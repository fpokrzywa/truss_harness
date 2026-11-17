'use client';

import Link from 'next/link';
import { useMemo, useState, type FormEvent } from 'react';
import { endpoints } from '@/lib/api/endpoints';
import type { Artifact, ArtifactScope } from '@/lib/api/build-types';
import type { OntAction } from '@/lib/api/ontology-types';
import type { Run } from '@/lib/api/operate-types';
import { useVersion } from '@/lib/build-hooks';
import { useProjects, useWorkspaces } from '@/lib/hooks';
import { normalizeOntologySpec } from '@/lib/ontology-spec';
import { orgHref } from '@/lib/scope';
import {
  ActionError,
  EmptyState,
  Modal,
  ModalActions,
  SelectField,
  TextField,
  useAction,
} from '../admin/ui';
import { QueryError } from '../QueryError';
import { RunStatusChip } from '../operate/parts';

/** Actions declared by the latest published version; invoking one creates a governed run. */
export function ActionsPanel({
  org,
  artifact,
  canInvoke,
}: {
  org: string;
  artifact: Artifact;
  canInvoke: boolean;
}) {
  const latestId = artifact.latest_published_version?.id;
  const version = useVersion(org, 'ontologies', artifact.id, latestId);
  const spec = useMemo(() => normalizeOntologySpec(version.data?.spec), [version.data]);
  const [invoking, setInvoking] = useState<OntAction | null>(null);

  if (!latestId) return <EmptyState>Publish a version to use its actions.</EmptyState>;
  if (version.isPending)
    return (
      <p role="status" className="text-sm text-slate-600">
        Loading actions...
      </p>
    );
  if (version.isError) return <QueryError error={version.error} />;

  return (
    <div className="space-y-3">
      <p className="text-sm text-slate-700">
        Actions run a governed tool against one object. Side-effecting tools follow the normal
        approval rules, so an invocation may wait for approval before it executes.
      </p>
      {spec.actions.length === 0 ? (
        <EmptyState>This version declares no actions.</EmptyState>
      ) : (
        <ul
          aria-label="Actions"
          className="divide-y divide-slate-100 rounded-md border border-slate-200"
        >
          {spec.actions.map((a) => (
            <li key={a.key} className="flex flex-wrap items-center gap-3 p-3 text-sm">
              <span className="font-mono">{a.key}</span>
              <span className="text-slate-600">on {a.object_type}</span>
              <span className="font-mono text-xs text-slate-600">
                tool version {a.tool_version_id}
              </span>
              <span className="text-xs text-slate-600">
                params:{' '}
                {Object.entries(a.param_mapping)
                  .map(([k, v]) => `${k}=${v}`)
                  .join(', ') || 'none'}
              </span>
              {canInvoke && (
                <button
                  type="button"
                  className="btn"
                  aria-label={`Invoke ${a.key}`}
                  onClick={() => setInvoking(a)}
                >
                  Invoke
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
      {invoking && (
        <InvokeDialog
          org={org}
          ontologyId={artifact.id}
          action={invoking}
          scope={artifact.scope}
          onClose={() => setInvoking(null)}
        />
      )}
    </div>
  );
}

function InvokeDialog({
  org,
  ontologyId,
  action,
  scope,
  onClose,
}: {
  org: string;
  ontologyId: string;
  action: OntAction;
  scope: ArtifactScope;
  onClose: () => void;
}) {
  // A run needs a workspace and a project. A project-scoped ontology supplies both; otherwise the
  // server requires the slugs in the body (422 when missing).
  const needsTarget = scope.type !== 'project';
  const workspaces = useWorkspaces(needsTarget ? org : undefined);
  const wsItems = workspaces.data?.items ?? [];
  const scopedWs = scope.type === 'workspace' ? wsItems.find((w) => w.id === scope.id) : undefined;
  const [pickedWs, setPickedWs] = useState('');
  const [project, setProject] = useState('');
  const workspace = scopedWs?.slug ?? pickedWs;
  const projects = useProjects(needsTarget ? org : undefined, workspace || undefined);
  const projectItems = workspace ? (projects.data?.items ?? []) : [];
  const targetOk = !needsTarget || (!!workspace && !!project);
  const [pk, setPk] = useState('');
  const [extra, setExtra] = useState('');
  const [run, setRun] = useState<Run | null>(null);
  const call = useAction();

  let extraParams: Record<string, unknown> | undefined;
  let extraError: string | null = null;
  if (extra.trim()) {
    try {
      const parsed: unknown = JSON.parse(extra);
      if (typeof parsed === 'object' && parsed !== null && !Array.isArray(parsed))
        extraParams = parsed as Record<string, unknown>;
      else extraError = 'Extra parameters must be a JSON object.';
    } catch {
      extraError = 'Extra parameters are not valid JSON.';
    }
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!pk.trim() || extraError || !targetOk) return;
    const r = await call.run(() =>
      endpoints.invokeAction(org, ontologyId, action.key, {
        object_pk: pk.trim(),
        ...(extraParams ? { extra_params: extraParams } : {}),
        ...(needsTarget ? { workspace, project } : {}),
      }),
    );
    if (r.ok) setRun(r.value);
  }

  return (
    <Modal title={`Invoke ${action.key}`} onClose={onClose} busy={call.busy}>
      {run ? (
        <div className="space-y-3">
          <p role="status" className="text-sm">
            Run created:{' '}
            <Link className="text-blue-800 underline" href={`${orgHref(org)}/runs/${run.id}`}>
              {run.id}
            </Link>{' '}
            <RunStatusChip status={run.status} outcomeUnknown={run.outcome_unknown} />.
          </p>
          {run.status === 'waiting_for_approval' ? (
            <p
              role="alert"
              className="rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900"
            >
              This action is waiting for approval. Nothing has changed yet; an approver must approve
              the run in{' '}
              <Link className="underline" href={`${orgHref(org)}/approvals`}>
                Approvals
              </Link>{' '}
              before it executes.
            </p>
          ) : (
            <p className="text-sm text-slate-700">
              The run executes through the tool gate. Open it to follow progress; it may still pause
              for approval if the tool has side effects.
            </p>
          )}
          <div className="flex justify-end">
            <button type="button" className="btn btn-primary" onClick={onClose}>
              Close
            </button>
          </div>
        </div>
      ) : (
        <form onSubmit={(e) => void submit(e)} className="space-y-3" noValidate>
          <p className="text-sm text-slate-700">
            Runs the action on one {action.object_type} object. Side-effecting actions may require
            approval.
          </p>
          <TextField
            label="Object primary key"
            value={pk}
            onChange={setPk}
            required
            monospace
            help="For composite keys, join the values with a comma."
          />
          {needsTarget && (
            <fieldset className="space-y-3">
              <legend className="text-sm font-semibold">Run in</legend>
              <p className="text-xs text-slate-600">
                This ontology is {scope.type === 'tenant' ? 'organization' : 'workspace'}-scoped, so
                the run needs a workspace and a project to belong to.
              </p>
              <div className="grid gap-3 sm:grid-cols-2">
                <SelectField
                  label="Workspace"
                  value={workspace}
                  disabled={!!scopedWs}
                  onChange={(v) => {
                    setPickedWs(v);
                    setProject('');
                  }}
                  options={[
                    { value: '', label: workspaces.isPending ? 'Loading...' : 'Select...' },
                    ...wsItems.map((w) => ({ value: w.slug, label: `${w.name} (${w.slug})` })),
                  ]}
                />
                <SelectField
                  label="Project"
                  value={project}
                  disabled={!workspace}
                  onChange={setProject}
                  options={[
                    {
                      value: '',
                      label: projects.isPending && workspace ? 'Loading...' : 'Select...',
                    },
                    ...projectItems.map((p) => ({ value: p.slug, label: `${p.name} (${p.slug})` })),
                  ]}
                />
              </div>
            </fieldset>
          )}
          <TextField
            label="Extra parameters (JSON object, optional)"
            value={extra}
            onChange={setExtra}
            multiline
            monospace
            error={extraError}
          />
          <ActionError error={call.error} title="Could not invoke the action" />
          <ModalActions
            onCancel={onClose}
            submitLabel="Invoke"
            busy={call.busy}
            submitDisabled={!pk.trim() || !!extraError || !targetOk}
          />
        </form>
      )}
    </Modal>
  );
}
