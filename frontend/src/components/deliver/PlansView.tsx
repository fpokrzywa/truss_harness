'use client';

import type { UseQueryResult } from '@tanstack/react-query';
import Link from 'next/link';
import { useState, type FormEvent } from 'react';
import { endpoints } from '@/lib/api/endpoints';
import { ApiError } from '@/lib/api/errors';
import { usePlans, useRefreshDelivery, useTargets } from '@/lib/delivery-hooks';
import { useCan, useProjects, useWorkspaces } from '@/lib/hooks';
import { useSolutions, useSolutionVersions, useVersionReleases } from '@/lib/release-hooks';
import { deliverHref, releaseHref } from '@/lib/scope';
import {
  ActionError,
  EmptyState,
  formatDate,
  Modal,
  ModalActions,
  PageHeader,
  QueryGate,
  SelectField,
  TextField,
  useAction,
} from '../admin/ui';
import { StatusBadge } from '../integrate/Maturity';
import { useOrgParam } from '../security/useOrg';
import { refusalHint } from './refusalHints';
import { targetName } from './targetName';

const COLUMNS = ['Display name', 'Application key', 'Target', 'Status', 'Expires', 'Created'];
const APPLICATION_KEY = /^[a-z][a-z0-9-]{0,62}$/;
const SELECT = { value: '', label: 'Select...' };

/** Deployment plans: list and request. Server text renders as plain React text only. */
export function PlansView() {
  const org = useOrgParam();
  const { can } = useCan(org);
  const plans = usePlans(org);
  const targets = useTargets(org);
  const [requesting, setRequesting] = useState(false);
  const canRequest = can('delivery:plan:request');

  return (
    <div className="space-y-6">
      <PageHeader
        title="Deployment plans"
        description="A plan is sealed against a target, then approved by a second person before it deploys."
        actions={
          canRequest && (
            <button type="button" className="btn btn-primary" onClick={() => setRequesting(true)}>
              Request plan
            </button>
          )
        }
      />
      <QueryGate query={plans} noun="plans">
        {(data) => (
          <div className="card overflow-x-auto p-0">
            {data.items.length === 0 ? (
              <EmptyState>
                No deployment plans yet.
                {canRequest && ' Use "Request plan" to create the first one.'}
              </EmptyState>
            ) : (
              <table className="w-full">
                <caption className="sr-only">Deployment plans</caption>
                <thead className="border-b border-slate-200">
                  <tr>
                    {COLUMNS.map((h) => (
                      <th key={h} scope="col" className="th">
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {data.items.map((p) => (
                    <tr key={p.id}>
                      <td className="td font-medium">
                        <Link
                          className="text-blue-800 underline"
                          href={deliverHref(org, `plans/${encodeURIComponent(p.id)}`)}
                        >
                          {p.display_name}
                        </Link>
                      </td>
                      <td className="td font-mono text-xs">{p.application_key}</td>
                      <td className="td">{targetName(targets.data?.items, p.target_id)}</td>
                      <td className="td">
                        <StatusBadge status={p.status} />
                      </td>
                      <td className="td">{formatDate(p.expires_at)}</td>
                      <td className="td">{formatDate(p.created_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        )}
      </QueryGate>

      {requesting && <RequestPlanDialog org={org} onClose={() => setRequesting(false)} />}
    </div>
  );
}

function RequestPlanDialog({ org, onClose }: { org: string; onClose: () => void }) {
  const targets = useTargets(org);
  const refresh = useRefreshDelivery(org);
  const action = useAction();
  const [targetId, setTargetId] = useState('');
  const [ws, setWs] = useState('');
  const [proj, setProj] = useState('');
  const [solutionId, setSolutionId] = useState('');
  const [versionId, setVersionId] = useState('');
  const [releaseId, setReleaseId] = useState('');
  // The release of the last request, so the evaluation link survives later select changes.
  const [submittedRelease, setSubmittedRelease] = useState('');
  const workspaces = useWorkspaces(org);
  const projects = useProjects(org, ws || undefined);
  // An empty workspace keeps a hook disabled until the select before it has a value.
  const solutions = useSolutions(org, proj ? ws : '', proj);
  const versions = useSolutionVersions(org, solutionId ? ws : '', proj, solutionId);
  const releases = useVersionReleases(org, versionId ? ws : '', proj, solutionId, versionId);
  const [applicationKey, setApplicationKey] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [errors, setErrors] = useState<Record<string, string>>({});

  async function submit(e: FormEvent) {
    e.preventDefault();
    const next: Record<string, string> = {};
    const name = displayName.trim();
    if (targetId === '') next.target = 'Choose a target.';
    if (releaseId === '') next.release = 'Choose a release.';
    if (!APPLICATION_KEY.test(applicationKey)) {
      next.applicationKey =
        'Use 1-63 lowercase letters, digits or hyphens, starting with a letter.';
    }
    if (name === '' || name.length > 200) next.displayName = 'Display name is 1-200 characters.';
    setErrors(next);
    if (Object.keys(next).length > 0) return;

    setSubmittedRelease(releaseId);
    const res = await action.run(() =>
      endpoints.requestPlan(org, {
        target_id: targetId,
        release_id: releaseId,
        application_key: applicationKey,
        display_name: name,
      }),
    );
    if (res.ok) {
      await refresh('plans');
      onClose();
    }
  }

  const targetOptions = (targets.data?.items ?? [])
    .filter((t) => t.status === 'active')
    .map((t) => ({ value: t.id, label: t.name }));
  const wsOptions = (workspaces.data?.items ?? [])
    .filter((w) => w.status === 'active')
    .map((w) => ({ value: w.slug, label: w.name }));
  const projOptions = ws
    ? (projects.data?.items ?? [])
        .filter((p) => p.status === 'active')
        .map((p) => ({ value: p.slug, label: p.name }))
    : [];
  const solutionOptions = proj
    ? (solutions.data?.items ?? []).map((s) => ({ value: s.id, label: s.name }))
    : [];
  const versionOptions = solutionId
    ? (versions.data?.items ?? []).map((v) => ({ value: v.id, label: `Version ${v.version_no}` }))
    : [];
  const releaseOptions = versionId
    ? (releases.data?.items ?? [])
        .filter((r) => r.status === 'sealed')
        .map((r) => ({
          value: r.id,
          label: `${r.digest.slice(0, 12)}, sealed ${formatDate(r.sealed_at)}`,
        }))
    : [];
  const evaluationRequired =
    action.error instanceof ApiError && action.error.code === 'evaluation_required';
  const hint = action.error instanceof ApiError ? refusalHint(action.error.code) : undefined;

  return (
    <Modal title="Request plan" onClose={onClose} busy={action.busy}>
      <form onSubmit={(e) => void submit(e)} className="space-y-4" noValidate>
        <div>
          <SelectField
            label="Target"
            value={targetId}
            onChange={setTargetId}
            options={[SELECT, ...targetOptions]}
          />
          {errors.target && <p className="mt-1 text-sm text-red-700">{errors.target}</p>}
        </div>
        <div>
          <SelectField
            label="Workspace"
            value={ws}
            onChange={(v) => {
              setWs(v);
              setProj('');
              setSolutionId('');
              setVersionId('');
              setReleaseId('');
            }}
            options={[SELECT, ...wsOptions]}
          />
          <StepHint
            query={workspaces}
            count={wsOptions.length}
            noun="workspaces"
            empty="No active workspaces."
          />
        </div>
        <div>
          <SelectField
            label="Project"
            value={proj}
            disabled={ws === ''}
            onChange={(v) => {
              setProj(v);
              setSolutionId('');
              setVersionId('');
              setReleaseId('');
            }}
            options={[SELECT, ...projOptions]}
          />
          {ws && (
            <StepHint
              query={projects}
              count={projOptions.length}
              noun="projects"
              empty="No active projects in this workspace."
            />
          )}
        </div>
        <div>
          <SelectField
            label="Solution"
            value={solutionId}
            disabled={proj === ''}
            onChange={(v) => {
              setSolutionId(v);
              setVersionId('');
              setReleaseId('');
            }}
            options={[SELECT, ...solutionOptions]}
          />
          {proj && (
            <StepHint
              query={solutions}
              count={solutionOptions.length}
              noun="solutions"
              empty="No solutions in this project."
            />
          )}
        </div>
        <div>
          <SelectField
            label="Version"
            value={versionId}
            disabled={solutionId === ''}
            onChange={(v) => {
              setVersionId(v);
              setReleaseId('');
            }}
            options={[SELECT, ...versionOptions]}
          />
          {solutionId && (
            <StepHint
              query={versions}
              count={versionOptions.length}
              noun="versions"
              empty="No versions of this solution."
            />
          )}
        </div>
        <div>
          <SelectField
            label="Release"
            value={releaseId}
            disabled={versionId === ''}
            onChange={setReleaseId}
            options={[SELECT, ...releaseOptions]}
          />
          <p className="mt-1 text-sm text-slate-600">
            Releases are sealed on the Solutions screen and need a passing evaluation before a plan
            can be requested.
          </p>
          {versionId && (
            <StepHint
              query={releases}
              count={releaseOptions.length}
              noun="releases"
              empty="No sealed releases for this version."
            />
          )}
          {errors.release && <p className="mt-1 text-sm text-red-700">{errors.release}</p>}
        </div>
        <TextField
          label="Application key"
          value={applicationKey}
          onChange={setApplicationKey}
          error={errors.applicationKey}
          maxLength={63}
          monospace
        />
        <TextField
          label="Display name"
          value={displayName}
          onChange={setDisplayName}
          error={errors.displayName}
          maxLength={200}
        />
        <ActionError error={action.error} title="Could not request the plan" />
        {hint && <p className="text-sm">{hint}</p>}
        {evaluationRequired && (
          <p className="text-sm">
            <Link className="text-blue-800 underline" href={releaseHref(org, submittedRelease)}>
              Open the release
            </Link>
          </p>
        )}
        <ModalActions onCancel={onClose} busy={action.busy} submitLabel="Request plan" />
      </form>
    </Modal>
  );
}

/** Under a cascade select whose step before it is chosen: loading, load failure or no options. */
function StepHint({
  query,
  count,
  noun,
  empty,
}: {
  query: UseQueryResult<unknown>;
  count: number;
  noun: string;
  empty: string;
}) {
  if (query.isPending)
    return (
      <p role="status" className="mt-1 text-sm text-slate-600">
        Loading {noun}...
      </p>
    );
  if (query.isError) {
    return (
      <div className="mt-1">
        <ActionError error={query.error} title={`Could not load the ${noun}`} />
      </div>
    );
  }
  if (count === 0) return <p className="mt-1 text-sm text-slate-600">{empty}</p>;
  return null;
}
