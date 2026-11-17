'use client';

import Link from 'next/link';
import { useParams, useSearchParams } from 'next/navigation';
import { useRef, useState, type FormEvent } from 'react';
import type { PromptVariable } from '@/lib/api/build-types';
import { endpoints } from '@/lib/api/endpoints';
import type { Run } from '@/lib/api/operate-types';
import { useArtifacts, useResolvedAgentVersion } from '@/lib/build-hooks';
import { useCan } from '@/lib/hooks';
import { useRun, useRunResult } from '@/lib/operate-hooks';
import { buildVariables, initialVariableValues, variableFields } from '@/lib/prompt-utils';
import { newIdempotencyKey, waitingText } from '@/lib/run-utils';
import { orgHref, projHref } from '@/lib/scope';
import type { FormValues } from '@/lib/schema-form';
import { ActionError, PageHeader, SelectField, useAction } from '../admin/ui';
import { SchemaForm } from '../integrate/SchemaForm';
import { NoAccess } from '../NoAccess';
import { useOrgParam } from '../security/useOrg';
import { Area } from '../build/shared';
import { CancelRunButton } from './CancelRunButton';
import { FailurePanel, ResultView, RunStatusChip, UsageSummary } from './parts';
import { Timeline } from './Timeline';

/**
 * Prompt variables the caller must supply, from the resolved agent version (shape tolerant, see
 * README). Variables the agent's `variables_map` already fills (e.g. `message <- {{input}}`) are
 * omitted: the server derives them, so asking for them again would be redundant.
 */
export function variablesOf(
  resolved: { prompt_version?: unknown; spec?: unknown } | undefined,
): PromptVariable[] {
  const pv = resolved?.prompt_version;
  if (typeof pv !== 'object' || pv === null) return [];
  const rec = pv as Record<string, unknown>;
  const spec = (typeof rec.spec === 'object' && rec.spec !== null ? rec.spec : rec) as Record<
    string,
    unknown
  >;
  const all = Array.isArray(spec.variables) ? (spec.variables as PromptVariable[]) : [];
  const agentSpec = resolved?.spec;
  const mapped =
    typeof agentSpec === 'object' && agentSpec !== null
      ? (agentSpec as Record<string, unknown>).variables_map
      : undefined;
  if (typeof mapped !== 'object' || mapped === null) return all;
  return all.filter((v) => !Object.hasOwn(mapped, v.name));
}

export function RunConsole() {
  const org = useOrgParam();
  const params = useParams<{ ws: string; proj: string }>();
  const ws = decodeURIComponent(params.ws);
  const proj = decodeURIComponent(params.proj);
  const { can, loaded } = useCan(org);
  const preselect = useSearchParams()?.get('agent') ?? '';
  const agents = useArtifacts(org, 'agents', { status: 'active' });
  const runnable = (agents.data?.items ?? []).filter((a) => a.latest_published_version);
  const [agentId, setAgentId] = useState(preselect);
  const [runId, setRunId] = useState<string | undefined>();

  if (loaded && !can('agent:execute')) return <NoAccess />;
  const agent = runnable.find((a) => a.id === agentId);

  return (
    <div className="space-y-6">
      <PageHeader
        title="Agent Execution Console"
        description={`Run a published agent in ${ws} / ${proj}. Runs execute asynchronously on the worker.`}
        actions={
          <Link className="btn" href={`${projHref(org, ws, proj)}/runs`}>
            Project run history
          </Link>
        }
      />
      <section aria-label="Start a run" className="card space-y-4">
        <SelectField
          label="Agent"
          value={agentId}
          onChange={(v) => {
            setAgentId(v);
            setRunId(undefined);
          }}
          options={[
            {
              value: '',
              label: agents.isPending ? 'Loading agents...' : 'Select an active agent',
            },
            ...runnable.map((a) => ({
              value: a.id,
              label: `${a.name} (v${a.latest_published_version?.version_no})`,
            })),
          ]}
        />
        {agents.isError && <ActionError error={agents.error} title="Could not load agents" />}
        {agent && agent.latest_published_version && (
          <StartForm
            key={agent.id}
            org={org}
            ws={ws}
            proj={proj}
            agentId={agent.id}
            versionId={agent.latest_published_version.id}
            onStarted={(r) => setRunId(r.id)}
          />
        )}
      </section>
      {runId && <LiveRun org={org} runId={runId} />}
    </div>
  );
}

function StartForm({
  org,
  ws,
  proj,
  agentId,
  versionId,
  onStarted,
}: {
  org: string;
  ws: string;
  proj: string;
  agentId: string;
  versionId: string;
  onStarted: (run: Run) => void;
}) {
  const resolved = useResolvedAgentVersion(org, agentId, versionId);
  const vars = variablesOf(resolved.data);
  const [input, setInput] = useState('');
  const [values, setValues] = useState<FormValues | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const { error, run, busy } = useAction();
  // One key per distinct submission payload: a retry of the same payload replays safely, an edited
  // payload gets a fresh key. `inFlight` is a synchronous guard so a double click cannot submit twice.
  const key = useRef<{ payload: string; id: string } | null>(null);
  const inFlight = useRef(false);

  const current = values ?? initialVariableValues(vars);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (inFlight.current) return;
    const built = buildVariables(vars, current);
    if (!built.ok) {
      setErrors(built.errors);
      return;
    }
    setErrors({});
    const body = {
      kind: 'agent' as const,
      agent_id: agentId,
      version_id: versionId,
      workspace: ws,
      project: proj,
      input,
      variables: built.values,
    };
    const payload = JSON.stringify(body);
    if (key.current?.payload !== payload) key.current = { payload, id: newIdempotencyKey() };
    const idempotencyKey = key.current.id;
    inFlight.current = true;
    try {
      const r = await run(() => endpoints.createRun(org, body, idempotencyKey));
      if (r.ok) {
        key.current = null;
        onStarted(r.value);
      }
    } finally {
      inFlight.current = false;
    }
  }

  if (resolved.isPending) return <p role="status">Loading agent...</p>;
  return (
    <form onSubmit={submit} className="space-y-4" aria-label="Run inputs">
      <Area label="Input" value={input} rows={4} onChange={setInput} />
      <fieldset className="space-y-2">
        <legend className="text-sm font-semibold">Variables</legend>
        {resolved.isError && (
          <p role="alert" className="text-sm text-amber-900">
            Could not read the agent&apos;s prompt variables; the run will start without any.
          </p>
        )}
        <SchemaForm
          fields={variableFields(vars)}
          values={current}
          errors={errors}
          onChange={(k, v) => setValues({ ...current, [k]: v })}
          emptyText="This agent's prompt declares no variables."
        />
      </fieldset>
      <ActionError error={error} title="Could not start the run" />
      <button type="submit" className="btn btn-primary" disabled={busy || !input.trim()}>
        {busy ? 'Starting...' : 'Start run'}
      </button>
    </form>
  );
}

function LiveRun({ org, runId }: { org: string; runId: string }) {
  const query = useRun(org, runId);
  const run = query.data;
  const result = useRunResult(org, runId, run?.status === 'completed');
  if (!run) return <p role="status">Loading run...</p>;
  return (
    <section aria-label="Run status" className="card space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <h2 className="text-base font-semibold">Run</h2>
        <code className="font-mono text-xs">{run.id}</code>
        <RunStatusChip status={run.status} outcomeUnknown={run.outcome_unknown} />
        <Link className="text-sm text-blue-800 underline" href={`${orgHref(org)}/runs/${run.id}`}>
          Open run detail
        </Link>
        <div className="ml-auto">
          <CancelRunButton org={org} run={run} />
        </div>
      </div>
      {run.status === 'waiting_for_approval' && (
        <p role="status" className="text-sm text-amber-900">
          {waitingText(run, 'Waiting for an approver to decide.')}
        </p>
      )}
      <FailurePanel run={run} />
      <UsageSummary run={run} />
      {result.data && <ResultView result={result.data} />}
      <div>
        <h3 className="mb-2 text-sm font-semibold">Timeline</h3>
        <Timeline org={org} run={run} />
      </div>
    </section>
  );
}
