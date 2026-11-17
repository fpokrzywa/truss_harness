'use client';

import Link from 'next/link';
import { useRef, useState, type FormEvent } from 'react';
import { ApiError } from '@/lib/api/errors';
import { endpoints } from '@/lib/api/endpoints';
import type { Artifact } from '@/lib/api/build-types';
import type { PipelineSpec } from '@/lib/api/pipeline-types';
import { useVersion, useVersions } from '@/lib/build-hooks';
import { useCan, useProjects, useWorkspaces } from '@/lib/hooks';
import { buildParams, paramsLayout } from '@/lib/pipeline-params';
import { useNodeTypes, useRunOverlay } from '@/lib/pipeline-hooks';
import { normalizePipelineSpec } from '@/lib/pipeline-spec';
import { newIdempotencyKey, waitingText } from '@/lib/run-utils';
import { initialValues, mapServerErrors, type FormValues } from '@/lib/schema-form';
import { orgHref } from '@/lib/scope';
import { ActionError, Badge, SelectField, useAction } from '../admin/ui';
import type { TestPanelProps } from '../build/DraftEditor';
import { Area } from '../build/shared';
import { SchemaForm } from '../integrate/SchemaForm';
import { CancelRunButton } from '../operate/CancelRunButton';
import { FailurePanel, RunStatusChip } from '../operate/parts';
import { LazyPipelineCanvas } from './LazyPipelineCanvas';
import { NodeStatusList } from './NodeStatusList';
import { usePipelineRun } from './PipelineRunContext';

const NO_IDS: ReadonlySet<string> = new Set();

/** Live run summary: status, per-node table with output preview, cancel. */
export function RunMonitor({ org, runId }: { org: string; runId: string }) {
  const { run, runError, nodes } = useRunOverlay(org, runId);
  if (runError) return <ActionError error={runError} title="Could not load the run" />;
  if (!run) return <p role="status">Loading run...</p>;
  return (
    <section aria-label="Pipeline run" className="card space-y-3">
      <div className="flex flex-wrap items-center gap-3">
        <h3 className="text-sm font-semibold">Run</h3>
        <code className="font-mono text-xs">{run.id}</code>
        <RunStatusChip status={run.status} outcomeUnknown={run.outcome_unknown} />
        {run.is_test && <Badge tone="warn">Test run</Badge>}
        <Link className="text-sm text-blue-800 underline" href={`${orgHref(org)}/runs/${run.id}`}>
          Open run detail
        </Link>
        <div className="ml-auto">
          <CancelRunButton org={org} run={run} />
        </div>
      </div>
      {run.status === 'waiting_for_approval' && (
        <p role="status" className="text-sm text-amber-900">
          {waitingText(run, 'A node is waiting for an approver.')}{' '}
          <Link className="text-blue-800 underline" href={`${orgHref(org)}/approvals`}>
            Open the approval queue
          </Link>
        </p>
      )}
      <FailurePanel run={run} />
      <NodeStatusList org={org} run={run} nodes={nodes ?? []} />
    </section>
  );
}

/** Workspace and project (slugs) a run executes in. */
function TargetPicker({
  org,
  ws,
  proj,
  onWs,
  onProj,
}: {
  org: string;
  ws: string;
  proj: string;
  onWs: (v: string) => void;
  onProj: (v: string) => void;
}) {
  const workspaces = useWorkspaces(org);
  const projects = useProjects(org, ws || undefined);
  return (
    <div className="flex flex-wrap items-end gap-3">
      <SelectField
        label="Run in workspace"
        value={ws}
        onChange={(v) => {
          onWs(v);
          onProj('');
        }}
        options={[
          { value: '', label: 'Select...' },
          ...(workspaces.data?.items ?? []).map((w) => ({ value: w.slug, label: w.name })),
        ]}
      />
      <SelectField
        label="Run in project"
        value={proj}
        disabled={!ws}
        onChange={onProj}
        options={[
          { value: '', label: 'Select...' },
          ...(projects.data?.items ?? []).map((p) => ({ value: p.slug, label: p.name })),
        ]}
      />
    </div>
  );
}

/**
 * Params form (from `spec.params`) plus workspace/project and the submit button. Test runs send
 * `version_id: "draft"` and `is_test: true`; executions send the published version id.
 */
export function PipelineRunForm({
  org,
  pipelineId,
  versionId,
  isTest,
  paramsSchema,
  disabledReason,
  submitLabel,
  onStarted,
}: {
  org: string;
  pipelineId: string;
  versionId: string;
  isTest: boolean;
  paramsSchema: Record<string, unknown> | undefined;
  disabledReason?: string;
  submitLabel: string;
  onStarted: (runId: string) => void;
}) {
  const layout = paramsLayout(paramsSchema);
  const [ws, setWs] = useState('');
  const [proj, setProj] = useState('');
  const [values, setValues] = useState<FormValues | null>(null);
  const [extra, setExtra] = useState('');
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [extraError, setExtraError] = useState<string | undefined>();
  const { busy, error, run } = useAction();
  // One key per distinct payload: retrying the same payload replays safely, an edited one gets a new key.
  const key = useRef<{ payload: string; id: string } | null>(null);
  const inFlight = useRef(false);
  const current = values ?? initialValues(layout.fields);

  const server =
    error instanceof ApiError && error.status === 422
      ? mapServerErrors(
          error.fieldErrors,
          layout.fields.map((f) => f.key),
        )
      : null;

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (inFlight.current) return;
    const built = buildParams(layout, current, extra);
    if (!built.ok) {
      setErrors(built.errors);
      setExtraError(built.extraError);
      return;
    }
    setErrors({});
    setExtraError(undefined);
    const body = {
      kind: 'pipeline' as const,
      pipeline_id: pipelineId,
      version_id: versionId,
      workspace: ws,
      project: proj,
      params: built.params,
      ...(isTest ? { is_test: true } : {}),
    };
    const payload = JSON.stringify(body);
    if (key.current?.payload !== payload) key.current = { payload, id: newIdempotencyKey() };
    const idempotencyKey = key.current.id;
    inFlight.current = true;
    try {
      const r = await run(() => endpoints.createPipelineRun(org, body, idempotencyKey));
      if (r.ok) {
        key.current = null;
        onStarted(r.value.id);
      }
    } finally {
      inFlight.current = false;
    }
  }

  return (
    <form
      onSubmit={submit}
      noValidate
      className="space-y-3"
      aria-label={isTest ? 'Test run inputs' : 'Run inputs'}
    >
      <TargetPicker org={org} ws={ws} proj={proj} onWs={setWs} onProj={setProj} />
      <fieldset className="space-y-2">
        <legend className="text-sm font-semibold">Parameters</legend>
        <SchemaForm
          fields={layout.fields}
          values={current}
          errors={{ ...(server?.byField ?? {}), ...errors }}
          onChange={(k, v) => setValues({ ...current, [k]: v })}
          emptyText={layout.jsonKeys.length ? '' : 'This pipeline declares no parameters.'}
        />
        {layout.jsonKeys.length > 0 && (
          <>
            <Area
              label={`Additional parameters (JSON object: ${layout.jsonKeys.join(', ')})`}
              value={extra}
              onChange={setExtra}
              rows={4}
              monospace
            />
            {extraError && (
              <p role="alert" className="text-sm text-red-700">
                {extraError}
              </p>
            )}
          </>
        )}
      </fieldset>
      {disabledReason && (
        <p role="note" className="text-sm text-slate-600">
          {disabledReason}
        </p>
      )}
      {server && server.other.length > 0 && (
        <ul role="alert" className="list-disc pl-5 text-sm text-red-700">
          {server.other.map((f, i) => (
            <li key={i}>
              {f.loc.join('.')}: {f.msg}
            </li>
          ))}
        </ul>
      )}
      <ActionError
        error={error}
        title={isTest ? 'Could not start the test run' : 'Could not start the run'}
      />
      <button
        type="submit"
        className="btn btn-primary"
        disabled={busy || !!disabledReason || !ws || !proj}
      >
        {busy ? 'Starting...' : submitLabel}
      </button>
    </form>
  );
}

/** Pipeline Test: runs the saved draft (`version_id: "draft"`, `is_test: true`). */
export function PipelineTestPanel({ org, artifact, spec, dirty }: TestPanelProps<PipelineSpec>) {
  const { can } = useCan(org);
  const { runId, setRunId } = usePipelineRun();
  if (!can('pipeline:test')) return null;
  return (
    <section aria-label="Test the draft" className="card space-y-3">
      <h3 className="text-sm font-semibold">Test run</h3>
      <p className="text-xs text-slate-600">
        Runs the saved draft as a labelled test run. Test runs use real connections and models, but
        side-effect nodes still wait for their approvals.
      </p>
      <PipelineRunForm
        org={org}
        pipelineId={artifact.id}
        versionId="draft"
        isTest
        paramsSchema={spec.params}
        submitLabel="Run test"
        disabledReason={
          dirty ? 'Save the draft to test it; tests run on the saved draft.' : undefined
        }
        onStarted={setRunId}
      />
      {runId && <RunMonitor org={org} runId={runId} />}
    </section>
  );
}

/** Pipeline Execution: run a published version with params, with the live overlay on a read-only canvas. */
export function PipelineRunPanel({ org, artifact }: { org: string; artifact: Artifact }) {
  const { can } = useCan(org);
  const { runId, setRunId } = usePipelineRun();
  const versions = useVersions(org, 'pipelines', artifact.id);
  const published = (versions.data ?? []).filter((v) => v.state === 'published');
  const [chosen, setChosen] = useState('');
  const versionId = chosen || artifact.latest_published_version?.id || '';
  const version = useVersion(org, 'pipelines', artifact.id, versionId || undefined);
  const spec = normalizePipelineSpec(version.data?.spec);
  const types = useNodeTypes();
  const overlay = useRunOverlay(org, runId);

  if (!can('pipeline:execute')) {
    return <p className="text-sm text-slate-600">You do not have permission to run pipelines.</p>;
  }
  if (!artifact.latest_published_version) {
    return (
      <p role="note" className="text-sm text-slate-600">
        Publish this pipeline to run it. Use the test run in the editor to try the draft.
      </p>
    );
  }
  return (
    <div className="space-y-4">
      <section aria-label="Run the pipeline" className="card space-y-3">
        <SelectField
          label="Version"
          value={versionId}
          onChange={(v) => {
            setChosen(v);
            setRunId(undefined);
          }}
          options={
            published.length
              ? published.map((v) => ({ value: v.id, label: `v${v.version_no}` }))
              : [{ value: versionId, label: `v${artifact.latest_published_version.version_no}` }]
          }
        />
        {artifact.status !== 'active' && (
          <p role="note" className="text-sm text-amber-900">
            This pipeline is {artifact.status}; the server may refuse to run it.
          </p>
        )}
        {version.isPending ? (
          <p role="status">Loading version...</p>
        ) : (
          <PipelineRunForm
            key={versionId}
            org={org}
            pipelineId={artifact.id}
            versionId={versionId}
            isTest={false}
            paramsSchema={spec.params}
            submitLabel="Run pipeline"
            onStarted={setRunId}
          />
        )}
      </section>
      {version.data && (
        <LazyPipelineCanvas
          spec={spec}
          registry={types.data ?? []}
          selectedId={null}
          invalidNodeIds={NO_IDS}
          invalidEdgeKeys={NO_IDS}
          statuses={overlay.statuses}
          readOnly
          onSelect={() => undefined}
          onConnect={() => undefined}
          onDeleteNodes={() => undefined}
          onDeleteEdges={() => undefined}
          onMoveNode={() => undefined}
        />
      )}
      {runId && <RunMonitor org={org} runId={runId} />}
    </div>
  );
}
