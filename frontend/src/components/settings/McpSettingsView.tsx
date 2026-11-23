'use client';

import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';
import { endpoints } from '@/lib/api/endpoints';
import {
  MCP_DEFAULT_TOOLS,
  MCP_TOOLS,
  type McpSettings,
  type McpSettingsInput,
} from '@/lib/api/mcp-endpoints';
import { useCan } from '@/lib/hooks';
import { ConfirmDialog } from '../ConfirmDialog';
import { QueryError } from '../QueryError';
import {
  ActionError,
  Badge,
  EmptyState,
  formatDate,
  PageHeader,
  QueryGate,
  TextField,
  useAction,
} from '../admin/ui';
import { useOrgParam } from '../security/useOrg';

/** The client configuration an MCP client needs; the key stays a placeholder. */
export function clientSnippet(url: string): string {
  return JSON.stringify(
    {
      mcpServers: {
        seth: {
          type: 'http',
          url,
          headers: { Authorization: 'Bearer shf_<your API key with mcp:access>' },
        },
      },
    },
    null,
    2,
  );
}

function EndpointBox({ url }: { url: string | null }) {
  return (
    <section aria-label="Endpoint" className="card space-y-2">
      <h2 className="text-lg font-semibold text-slate-900">Endpoint</h2>
      {url ? (
        <>
          <p className="font-mono text-sm break-all">{url}</p>
          <p className="text-sm text-slate-600">
            Client configuration (create an API key with the <code>mcp:access</code> scope on the
            API keys screen and put it in place of the placeholder). Tools that take a project also
            need <code>workspace:read</code>, and <code>start_run</code> needs{' '}
            <code>prompt:read</code>:
          </p>
          <pre className="overflow-x-auto rounded-md bg-slate-50 p-3 text-xs">
            {clientSnippet(url)}
          </pre>
        </>
      ) : (
        <p className="text-sm text-slate-600">
          The MCP endpoint is not configured on this installation.
        </p>
      )}
    </section>
  );
}

function McpAudit({ org }: { org: string }) {
  const events = useQuery({
    queryKey: ['audit', org, 'mcp'],
    queryFn: () => endpoints.listAuditEvents(org, { category: 'mcp' }),
  });
  return (
    <section aria-label="MCP audit events" className="card overflow-x-auto p-0">
      <h2 className="p-4 text-lg font-semibold text-slate-900">Last 50 MCP events</h2>
      {events.isPending && (
        <p role="status" className="p-4 text-sm text-slate-600">
          Loading events...
        </p>
      )}
      {events.isError && <QueryError error={events.error} />}
      {events.data &&
        (events.data.items.length === 0 ? (
          <EmptyState>No MCP events yet.</EmptyState>
        ) : (
          <table className="w-full">
            <caption className="sr-only">MCP audit events</caption>
            <thead className="border-b border-slate-200">
              <tr>
                {['When', 'Actor', 'Action', 'Outcome'].map((h) => (
                  <th key={h} scope="col" className="th">
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {events.data.items.map((e) => (
                <tr key={e.id}>
                  <td className="td whitespace-nowrap">
                    {formatDate(e.occurred_at ?? e.at ?? e.created_at)}
                  </td>
                  <td className="td">{e.actor ?? '-'}</td>
                  <td className="td font-mono text-xs">{e.action}</td>
                  <td className="td">{e.outcome ?? '-'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ))}
    </section>
  );
}

/** The audit list needs audit:read, which the developer role does not hold. */
function AuditSection({ org }: { org: string }) {
  const { loaded, can } = useCan(org);
  if (!loaded) return null;
  if (can('audit:read')) return <McpAudit org={org} />;
  return (
    <section aria-label="MCP audit events" className="card">
      <p className="text-sm text-slate-600">You need audit:read to see MCP events.</p>
    </section>
  );
}

function SettingsForm({ org, saved }: { org: string; saved: McpSettings }) {
  const { can } = useCan(org);
  const writable = can('mcp:settings:write');
  const queryClient = useQueryClient();
  const save = useAction();
  const toggle = useAction();
  const [confirming, setConfirming] = useState(false);
  const [rate, setRate] = useState(String(saved.rate_per_minute));
  const [maxCost, setMaxCost] = useState(String(saved.max_cost_per_call));
  const [tools, setTools] = useState<readonly string[]>(saved.allowed_tools ?? MCP_DEFAULT_TOOLS);
  const [usesDefault, setUsesDefault] = useState(saved.allowed_tools === null);

  const current = (): McpSettingsInput => ({
    enabled: saved.enabled,
    rate_per_minute: saved.rate_per_minute,
    max_cost_per_call: saved.max_cost_per_call,
    allowed_tools: saved.allowed_tools,
  });

  async function put(input: McpSettingsInput, action: ReturnType<typeof useAction>) {
    const res = await action.run(() => endpoints.putMcpSettings(org, input));
    if (res.ok) queryClient.setQueryData(['mcp-settings', org], res.value);
    return res.ok;
  }

  async function confirmToggle() {
    if (await put({ ...current(), enabled: !saved.enabled }, toggle)) setConfirming(false);
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    await put(
      {
        enabled: saved.enabled,
        rate_per_minute: Number(rate),
        max_cost_per_call: Number(maxCost),
        allowed_tools: usesDefault ? null : [...tools],
      },
      save,
    );
  }

  function flip(tool: string, on: boolean) {
    setUsesDefault(false);
    setTools((prev) => (on ? [...prev, tool] : prev.filter((t) => t !== tool)));
  }

  return (
    <>
      <section aria-label="MCP switch" className="card flex flex-wrap items-center gap-3">
        <span className="text-sm text-slate-700">MCP access for this organization</span>
        <Badge tone={saved.enabled ? 'good' : 'neutral'}>{saved.enabled ? 'On' : 'Off'}</Badge>
        <button
          type="button"
          role="switch"
          aria-checked={saved.enabled}
          aria-label="MCP access"
          className="btn"
          disabled={!writable || toggle.busy}
          onClick={() => setConfirming(true)}
        >
          {saved.enabled ? 'Turn off' : 'Turn on'}
        </button>
        {!writable && (
          <span className="text-xs text-slate-600">
            Only organization administrators can change this.
          </span>
        )}
        {saved.updated_at && (
          <span className="text-xs text-slate-600">
            Last changed {formatDate(saved.updated_at)}
          </span>
        )}
      </section>
      <ConfirmDialog
        open={confirming}
        title={saved.enabled ? 'Turn MCP access off?' : 'Turn MCP access on?'}
        confirmLabel={saved.enabled ? 'Turn off' : 'Turn on'}
        busy={toggle.busy}
        onConfirm={() => void confirmToggle()}
        onCancel={() => setConfirming(false)}
      >
        {saved.enabled
          ? 'Every MCP call from this organization’s API keys will be refused.'
          : 'API keys of this organization holding mcp:access can call the allowed tools, within the rate and cost limits below.'}
        <ActionError error={toggle.error} title="Could not change MCP access" />
      </ConfirmDialog>

      <form onSubmit={submit} aria-label="MCP limits" className="card space-y-4">
        <div className="grid gap-3 sm:grid-cols-2">
          <TextField
            label="Rate per minute (per API key)"
            type="number"
            min={1}
            max={600}
            value={rate}
            onChange={setRate}
            readOnly={!writable}
            help="1 to 600 tool calls a minute; the organization as a whole gets 4x this."
          />
          <TextField
            label="Max cost per call (USD)"
            type="number"
            min={0.0001}
            max={25}
            step="any"
            value={maxCost}
            onChange={setMaxCost}
            readOnly={!writable}
            help="Caps the cost of one start_run; at most 25."
          />
        </div>
        <fieldset>
          <legend className="label">Allowed tools</legend>
          {usesDefault && (
            <p className="mb-2 text-xs text-slate-600">
              The default set: every tool except start_run.
            </p>
          )}
          <ul className="grid gap-1 sm:grid-cols-3">
            {MCP_TOOLS.map((tool) => (
              <li key={tool}>
                <label className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={tools.includes(tool)}
                    disabled={!writable}
                    onChange={(e) => flip(tool, e.target.checked)}
                  />
                  <span className="font-mono">{tool}</span>
                </label>
              </li>
            ))}
          </ul>
        </fieldset>
        <ActionError error={save.error} title="Could not save the MCP settings" />
        {writable && (
          <button type="submit" className="btn btn-primary" disabled={save.busy}>
            {save.busy ? 'Saving...' : 'Save'}
          </button>
        )}
      </form>
    </>
  );
}

/** The organization's MCP settings (SP8 design 3.6): switch, limits, allowed tools, audit. */
export function McpSettingsView() {
  const org = useOrgParam();
  const settings = useQuery({
    queryKey: ['mcp-settings', org],
    queryFn: () => endpoints.getMcpSettings(org),
  });
  return (
    <div className="space-y-6">
      <PageHeader
        title="MCP access"
        description="Lets MCP clients call SETH's tools with this organization's API keys. Off by default; every call is audited."
      />
      <QueryGate query={settings} noun="MCP settings">
        {(saved) => (
          <>
            {/* remounted after each save, so the fields show what the server stored */}
            <SettingsForm key={saved.updated_at ?? 'defaults'} org={org} saved={saved} />
            <EndpointBox url={saved.endpoint_url} />
          </>
        )}
      </QueryGate>
      {/* only once the settings load, so a user without mcp:settings:read sees one refusal */}
      {settings.isSuccess && <AuditSection org={org} />}
    </div>
  );
}
