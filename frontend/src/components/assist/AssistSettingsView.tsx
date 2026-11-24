'use client';

import { useState, type FormEvent } from 'react';
import type {
  AssistCatalogOut,
  AssistSettingsOut,
  CatalogEntryOut,
  SpecialistOut,
} from '@/lib/api/assist-types';
import { endpoints } from '@/lib/api/endpoints';
import { PROPOSAL_KIND, specialistTitle } from '@/lib/assist';
import {
  useAssistCatalog,
  useAssistSettings,
  useRefreshAssist,
  useSpecialists,
  useStoreAssistSettings,
} from '@/lib/assist-hooks';
import { useCan, usePermissions } from '@/lib/hooks';
import { ConfirmDialog } from '../ConfirmDialog';
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
import { ModelSelect } from '../build/pickers';
import { ProjectScopePicker, useProjectQuery } from '../releases/ProjectScope';
import { useOrgParam } from '../security/useOrg';
import type { ProjectScope } from './parts';

/** What installing needs on the project (`INSTALL_PERMISSIONS` in `assist/service.py`). */
const INSTALL_PERMISSIONS = [
  'assist:install',
  'prompt:create',
  'prompt:update',
  'prompt:publish',
  'tool:create',
  'tool:update',
  'tool:publish',
  'agent:create',
  'agent:update',
  'agent:publish',
  'agent:activate',
] as const;

/** A USD amount as the server stores it: at most 8 whole digits and 4 decimals. */
const AMOUNT = /^([0-9]{1,8}(\.[0-9]{1,4})?|\.[0-9]{1,4})$/;

/** Ask SETH's settings: the organization's switch and routing, and one project's specialists. */
export function AssistSettingsView() {
  const org = useOrgParam();
  const { can } = useCan(org);
  const { ws, proj } = useProjectQuery();
  return (
    <div className="space-y-8">
      <PageHeader
        title="Assist settings"
        description="Turn Ask SETH on for the organization and install its specialists in each project that uses it."
      />
      {can('assist:admin') && <OrganizationSection org={org} />}
      <section aria-label="Project specialists" className="space-y-4">
        <h2 className="text-lg font-semibold text-slate-900">Project specialists</h2>
        <ProjectScopePicker org={org} />
        {ws !== '' && proj !== '' ? (
          <ProjectSpecialists key={`${ws}/${proj}`} scope={{ org, ws, proj }} />
        ) : (
          <div className="card p-0">
            <EmptyState>Choose a workspace and a project to see its specialists.</EmptyState>
          </div>
        )}
        <p className="max-w-3xl text-sm text-slate-700">
          A governance approval rule can hold a specialist&apos;s proposal step. The proposal then
          waits on the normal approval of the run.
        </p>
      </section>
    </div>
  );
}

// ---- Organization --------------------------------------------------------------------------

function OrganizationSection({ org }: { org: string }) {
  const settings = useAssistSettings(org);
  // The form is remounted after each save, so its fields show what the server stored.
  const [saves, setSaves] = useState(0);
  return (
    <section aria-label="Organization" className="space-y-4">
      <h2 className="text-lg font-semibold text-slate-900">Organization</h2>
      <QueryGate query={settings} noun="assist settings">
        {(saved) => (
          <>
            <Switch org={org} saved={saved} />
            <RoutingForm
              key={`${saves}/${saved.model_routing_enabled}/${saved.router_model_id}/${saved.specialist_max_cost}`}
              org={org}
              saved={saved}
              onSaved={() => setSaves((n) => n + 1)}
            />
            <p role="status" className="text-sm text-slate-700">
              {saves > 0 && 'Settings saved.'}
            </p>
          </>
        )}
      </QueryGate>
    </section>
  );
}

function Switch({ org, saved }: { org: string; saved: AssistSettingsOut }) {
  const store = useStoreAssistSettings(org);
  const refresh = useRefreshAssist(org);
  const action = useAction();
  const [confirming, setConfirming] = useState(false);
  const on = saved.assist_enabled;
  const outdated = on && saved.catalog_version !== saved.current_catalog_version;

  async function change(call: typeof endpoints.enableAssist) {
    const res = await action.run(() => call(org));
    if (res.ok) {
      store(res.value);
      // The specialists' availability follows the switch and the pin.
      void refresh();
    }
    return res.ok;
  }

  async function confirm() {
    if (await change(on ? endpoints.disableAssist : endpoints.enableAssist)) setConfirming(false);
  }

  return (
    <>
      <div className="card flex flex-wrap items-center gap-3">
        <span className="text-sm text-slate-700">Ask SETH for this organization</span>
        <Badge tone={on ? 'good' : 'neutral'}>{on ? 'On' : 'Off'}</Badge>
        <button
          type="button"
          role="switch"
          aria-checked={on}
          aria-label="Ask SETH"
          className="btn"
          disabled={action.busy}
          onClick={() => {
            action.reset();
            setConfirming(true);
          }}
        >
          {on ? 'Turn off' : 'Turn on'}
        </button>
        {saved.updated_at && (
          <span className="text-xs text-slate-600">
            Last changed {formatDate(saved.updated_at)}
          </span>
        )}
      </div>
      {outdated && (
        <div className="card space-y-2 border-amber-300 bg-amber-50 text-sm text-amber-900">
          <p>
            Every specialist is off: this organization is pinned to catalog version{' '}
            {saved.catalog_version ?? 'none'}, and SETH now has {saved.current_catalog_version}.
            Move to it, then run install again in each project.
          </p>
          <button
            type="button"
            className="btn"
            disabled={action.busy}
            onClick={() => void change(endpoints.enableAssist)}
          >
            Move to {saved.current_catalog_version}
          </button>
          {!confirming && (
            <ActionError error={action.error} title="Could not change the catalog version" />
          )}
        </div>
      )}
      <ConfirmDialog
        open={confirming}
        title={on ? 'Turn Ask SETH off?' : 'Turn Ask SETH on?'}
        confirmLabel={on ? 'Turn off' : 'Turn on'}
        busy={action.busy}
        onConfirm={() => void confirm()}
        onCancel={() => {
          action.reset();
          setConfirming(false);
        }}
      >
        {on
          ? "Every project's specialists of this organization stop. They stay installed."
          : 'The specialists installed in this organization’s projects can be used.'}
        <ActionError error={action.error} title="Could not change Ask SETH" />
      </ConfirmDialog>
    </>
  );
}

function RoutingForm({
  org,
  saved,
  onSaved,
}: {
  org: string;
  saved: AssistSettingsOut;
  onSaved: () => void;
}) {
  const store = useStoreAssistSettings(org);
  const action = useAction();
  const [routing, setRouting] = useState(saved.model_routing_enabled);
  const [modelId, setModelId] = useState(saved.router_model_id ?? '');
  const [cost, setCost] = useState(String(saved.specialist_max_cost));
  const [errors, setErrors] = useState<{ model?: string; cost?: string }>({});

  async function submit(e: FormEvent) {
    e.preventDefault();
    const next: typeof errors = {};
    if (routing && modelId === '') next.model = 'Choose a router model to use model routing.';
    const amount = cost.trim();
    if (!AMOUNT.test(amount) || Number(amount) <= 0)
      next.cost = 'Enter an amount in USD above 0, with at most 4 decimals.';
    setErrors(next);
    if (Object.keys(next).length > 0) return;

    const res = await action.run(() =>
      endpoints.putAssistSettings(org, {
        model_routing_enabled: routing,
        router_model_id: modelId || null,
        specialist_max_cost: Number(amount),
      }),
    );
    if (res.ok) {
      store(res.value);
      onSaved();
    }
  }

  return (
    <form
      onSubmit={(e) => void submit(e)}
      aria-label="Routing and cost"
      className="card max-w-3xl space-y-4"
      noValidate
    >
      <label className="flex items-center gap-2 text-sm">
        <input type="checkbox" checked={routing} onChange={(e) => setRouting(e.target.checked)} />
        Let a model choose the specialist when the routing rules cannot
      </label>
      <div>
        <ModelSelect
          org={org}
          label="Router model"
          value={modelId}
          onChange={setModelId}
          emptyLabel="None"
        />
        {errors.model && (
          <p role="alert" className="mt-1 text-sm text-red-700">
            {errors.model}
          </p>
        )}
      </div>
      <TextField
        label="Cost cap of one request (USD)"
        type="number"
        min={0.0001}
        step="any"
        value={cost}
        onChange={setCost}
        help="The cap applies to new requests. Installed agents carry the cap of their last install, so run install again after changing it."
        error={errors.cost}
      />
      <ActionError error={action.error} title="Could not save the assist settings" />
      <button type="submit" className="btn btn-primary" disabled={action.busy}>
        {action.busy ? 'Saving...' : 'Save'}
      </button>
    </form>
  );
}

// ---- Project -------------------------------------------------------------------------------

function ProjectSpecialists({ scope }: { scope: ProjectScope }) {
  const { org, ws, proj } = scope;
  const permissions = usePermissions(org, ws, proj);
  const catalog = useAssistCatalog(org);
  const specialists = useSpecialists(org, ws, proj);
  return (
    <QueryGate query={permissions} noun="permissions">
      {(perms) => (
        <QueryGate query={catalog} noun="specialists">
          {(data) => (
            <QueryGate query={specialists} noun="specialists">
              {(rows) => (
                <>
                  <SpecialistsTable scope={scope} catalog={data} rows={rows} perms={perms} />
                  <Install scope={scope} catalog={data} rows={rows} perms={perms} />
                </>
              )}
            </QueryGate>
          )}
        </QueryGate>
      )}
    </QueryGate>
  );
}

interface ProjectData {
  scope: ProjectScope;
  catalog: AssistCatalogOut;
  rows: SpecialistOut[];
  perms: ReadonlySet<string>;
}

function SpecialistsTable({ scope, catalog, rows, perms }: ProjectData) {
  const canToggle = perms.has('assist:install') && perms.has('agent:activate');
  return (
    <div className="card overflow-x-auto p-0">
      <table className="w-full">
        <caption className="sr-only">Specialists</caption>
        <thead className="border-b border-slate-200">
          <tr>
            {['Specialist', 'What it does', 'Proposes', 'Status in this project'].map((h) => (
              <th key={h} scope="col" className="th">
                {h}
              </th>
            ))}
            <th scope="col" className="th">
              <span className="sr-only">Actions</span>
            </th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {catalog.entries.map((e) => (
            <SpecialistRow
              key={e.key}
              scope={scope}
              entry={e}
              row={rows.find((r) => r.key === e.key)}
              canToggle={canToggle}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function SpecialistRow({
  scope,
  entry,
  row,
  canToggle,
}: {
  scope: ProjectScope;
  entry: CatalogEntryOut;
  row: SpecialistOut | undefined;
  canToggle: boolean;
}) {
  const { org, ws, proj } = scope;
  const refresh = useRefreshAssist(org);
  const action = useAction();
  const enabled = row?.status === 'enabled';

  async function toggle() {
    const call = enabled ? endpoints.disableSpecialist : endpoints.enableSpecialist;
    await action.run(() => call(org, ws, proj, entry.key));
    await refresh();
  }

  return (
    <tr>
      <td className="td font-medium">{entry.title}</td>
      <td className="td">{entry.router_description}</td>
      <td className="td">
        {entry.proposal_kinds
          .map((k) => (k in PROPOSAL_KIND ? PROPOSAL_KIND[k as keyof typeof PROPOSAL_KIND] : k))
          .join(', ') || '-'}
      </td>
      <td className="td">
        <SpecialistStatusText row={row} />
        <ActionError error={action.error} title="Could not change the specialist" />
      </td>
      <td className="td">
        {row && row.status !== 'installing' && canToggle && (
          <button
            type="button"
            className="btn"
            disabled={action.busy}
            onClick={() => void toggle()}
          >
            {enabled ? 'Disable' : 'Enable'} <span className="sr-only">{entry.title}</span>
          </button>
        )}
      </td>
    </tr>
  );
}

function SpecialistStatusText({ row }: { row: SpecialistOut | undefined }) {
  if (row === undefined) return <Badge>Not installed</Badge>;
  if (row.status === 'installing')
    return (
      <>
        <Badge tone="warn">Installing</Badge>
        <p className="mt-1 text-xs text-slate-600">
          The install did not finish: run install again.
        </p>
      </>
    );
  if (row.status === 'disabled') return <Badge>Disabled</Badge>;
  if (row.available) return <Badge tone="good">Enabled</Badge>;
  return (
    <>
      <Badge tone="warn">Enabled, not available</Badge>
      <p className="mt-1 text-xs text-slate-600">Off for the organization, or outdated.</p>
    </>
  );
}

function Install({ scope, catalog, rows, perms }: ProjectData) {
  const { org, ws, proj } = scope;
  const refresh = useRefreshAssist(org);
  const action = useAction();
  const [modelId, setModelId] = useState('');
  const [installed, setInstalled] = useState(false);

  const missing = INSTALL_PERMISSIONS.filter((p) => !perms.has(p));
  if (missing.length > 0)
    return (
      <p className="text-sm text-slate-700">
        Installing specialists needs permissions you do not have on this project:{' '}
        {missing.join(', ')}.
      </p>
    );

  const disabled = rows.filter((r) => r.status === 'disabled').map((r) => r.key);
  const keys = catalog.entries.map((e) => e.key).filter((k) => !disabled.includes(k));
  const blocked = !catalog.assist_enabled
    ? 'Ask SETH is off for this organization, or pinned to an older catalog version, so nothing can be installed.'
    : keys.length === 0
      ? 'Every specialist is disabled in this project. Enable one to install it again.'
      : null;

  async function install(e: FormEvent) {
    e.preventDefault();
    setInstalled(false);
    const res = await action.run(() =>
      endpoints.installSpecialists(org, ws, proj, { model_id: modelId, keys }),
    );
    // A refused install can leave specialists installing: read them again either way.
    await refresh();
    setInstalled(res.ok);
  }

  return (
    <form
      onSubmit={(e) => void install(e)}
      aria-label="Install specialists"
      className="card max-w-3xl space-y-4"
    >
      <h3 className="text-base font-semibold">Install specialists</h3>
      <p className="text-sm text-slate-700">
        Publishes each specialist&apos;s tools, prompt and agent in this project with the model you
        choose. Installing again updates them.
      </p>
      {disabled.length > 0 && keys.length > 0 && (
        <p className="text-sm text-slate-700">
          Disabled specialists are left as they are:{' '}
          {disabled.map((k) => specialistTitle(catalog.entries, k)).join(', ')}.
        </p>
      )}
      {blocked !== null && <p className="text-sm text-slate-700">{blocked}</p>}
      <ModelSelect
        org={org}
        workspace={ws}
        project={proj}
        label="Specialist model"
        value={modelId}
        onChange={setModelId}
      />
      <ActionError error={action.error} title="Could not install the specialists" />
      <p role="status" className="text-sm text-slate-700">
        {installed && 'Specialists installed.'}
      </p>
      <button
        type="submit"
        className="btn btn-primary"
        disabled={action.busy || modelId === '' || blocked !== null}
      >
        {action.busy ? 'Installing...' : 'Install'}
      </button>
    </form>
  );
}
