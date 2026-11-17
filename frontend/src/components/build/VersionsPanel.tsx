'use client';

import { useState } from 'react';
import { endpoints } from '@/lib/api/endpoints';
import type { Artifact, ArtifactKind, ArtifactVersion, DiffChange } from '@/lib/api/build-types';
import {
  useRefreshBuild,
  useResolvedAgentVersion,
  useVersion,
  useVersions,
} from '@/lib/build-hooks';
import {
  ActionError,
  EmptyState,
  formatDate,
  Modal,
  QueryGate,
  SelectField,
  useAction,
} from '../admin/ui';
import { ConfirmDialog } from '../ConfirmDialog';
import { QueryError } from '../QueryError';
import { JsonView, VersionStateBadge } from './shared';

interface PanelProps {
  org: string;
  kind: ArtifactKind;
  artifact: Artifact;
  canUpdate: boolean;
  canPublish: boolean;
}

type Pending = { type: 'rollback' | 'deprecated' | 'retired'; version: ArtifactVersion } | null;

export function VersionsPanel({ org, kind, artifact, canUpdate, canPublish }: PanelProps) {
  const versions = useVersions(org, kind, artifact.id);
  const refresh = useRefreshBuild(org);
  const [viewing, setViewing] = useState<string | null>(null);
  const [pending, setPending] = useState<Pending>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const { busy, error, run, reset } = useAction();

  async function confirm() {
    if (!pending) return;
    const { type, version } = pending;
    const r = await run(() =>
      type === 'rollback'
        ? endpoints.rollback(org, kind, artifact.id, version.id)
        : endpoints.setVersionState(org, kind, artifact.id, version.id, type),
    );
    if (r.ok) {
      setNotice(
        type === 'rollback'
          ? `Rolled back: a new draft was created from v${version.version_no}. Publish it to apply.`
          : `Version ${version.version_no} is now ${type}.`,
      );
      setPending(null);
      await refresh();
    }
  }

  return (
    <div className="space-y-3">
      {notice && (
        <p role="status" className="rounded-md border border-green-300 bg-green-50 p-3 text-sm">
          {notice}
        </p>
      )}
      <QueryGate query={versions} noun="versions">
        {(items) =>
          items.length === 0 ? (
            <EmptyState>No versions yet.</EmptyState>
          ) : (
            <section aria-label="Versions" className="card p-0">
              <table className="w-full">
                <caption className="sr-only">Versions</caption>
                <thead className="border-b border-slate-200">
                  <tr>
                    {['Version', 'State', 'Spec hash', 'Published', 'Change note', 'Actions'].map(
                      (h) => (
                        <th key={h} scope="col" className="th">
                          {h}
                        </th>
                      ),
                    )}
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {[...items]
                    .sort((a, b) => b.version_no - a.version_no)
                    .map((v) => (
                      <tr key={v.id}>
                        <td className="td font-medium">v{v.version_no}</td>
                        <td className="td">
                          <VersionStateBadge state={v.state} />
                        </td>
                        <td className="td font-mono text-xs">{v.spec_hash?.slice(0, 12) ?? '-'}</td>
                        <td className="td">{formatDate(v.published_at)}</td>
                        <td className="td">{v.change_note ?? '-'}</td>
                        <td className="td">
                          <div className="flex flex-wrap gap-1">
                            <button
                              type="button"
                              className="btn"
                              aria-label={`View v${v.version_no}`}
                              onClick={() => setViewing(v.id)}
                            >
                              View
                            </button>
                            {canUpdate && v.state !== 'draft' && (
                              <button
                                type="button"
                                className="btn"
                                aria-label={`Roll back to v${v.version_no}`}
                                onClick={() => {
                                  reset();
                                  setPending({ type: 'rollback', version: v });
                                }}
                              >
                                Roll back
                              </button>
                            )}
                            {canPublish && v.state === 'published' && (
                              <button
                                type="button"
                                className="btn"
                                aria-label={`Deprecate v${v.version_no}`}
                                onClick={() => {
                                  reset();
                                  setPending({ type: 'deprecated', version: v });
                                }}
                              >
                                Deprecate
                              </button>
                            )}
                            {canPublish && v.state === 'deprecated' && (
                              <button
                                type="button"
                                className="btn btn-danger"
                                aria-label={`Retire v${v.version_no}`}
                                onClick={() => {
                                  reset();
                                  setPending({ type: 'retired', version: v });
                                }}
                              >
                                Retire
                              </button>
                            )}
                          </div>
                        </td>
                      </tr>
                    ))}
                </tbody>
              </table>
            </section>
          )
        }
      </QueryGate>
      <ConfirmDialog
        open={pending !== null}
        title={
          pending?.type === 'rollback'
            ? `Roll back to v${pending.version.version_no}?`
            : pending?.type === 'retired'
              ? `Retire v${pending?.version.version_no}?`
              : `Deprecate v${pending?.version.version_no}?`
        }
        confirmLabel={
          pending?.type === 'rollback'
            ? 'Create draft'
            : pending?.type === 'retired'
              ? 'Retire'
              : 'Deprecate'
        }
        busy={busy}
        onConfirm={() => void confirm()}
        onCancel={() => setPending(null)}
      >
        {pending?.type === 'rollback' ? (
          <p>
            This creates a new draft based on v{pending.version.version_no}. History stays linear;
            publish the draft to apply it.
          </p>
        ) : pending?.type === 'retired' ? (
          <p>
            Retired versions can no longer be selected or used. This cannot be undone: retired is
            terminal.
          </p>
        ) : (
          <p>
            Deprecated versions remain usable by existing references but should not be chosen for
            new ones. A deprecated version can later be retired.
          </p>
        )}
        <ActionError error={error} title="Action failed" />
      </ConfirmDialog>
      {viewing && (
        <VersionViewer
          org={org}
          kind={kind}
          artifactId={artifact.id}
          versionId={viewing}
          onClose={() => setViewing(null)}
        />
      )}
    </div>
  );
}

export function VersionViewer({
  org,
  kind,
  artifactId,
  versionId,
  onClose,
}: {
  org: string;
  kind: ArtifactKind;
  artifactId: string;
  versionId: string;
  onClose: () => void;
}) {
  const v = useVersion(org, kind, artifactId, versionId);
  const [showResolved, setShowResolved] = useState(false);
  return (
    <Modal title="Version details" onClose={onClose} wide>
      {v.isPending ? (
        <p role="status">Loading...</p>
      ) : v.isError ? (
        <QueryError error={v.error} />
      ) : (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <strong>v{v.data.version_no}</strong>
            <VersionStateBadge state={v.data.state} />
            <span>
              Spec hash:{' '}
              <code className="font-mono text-xs">{v.data.spec_hash ?? 'not published'}</code>
            </span>
          </div>
          {v.data.change_note && <p className="text-sm">Change note: {v.data.change_note}</p>}
          <JsonView value={v.data.spec} label="Version spec (read-only)" />
          {kind === 'agents' && v.data.state !== 'draft' && (
            <>
              <button type="button" className="btn" onClick={() => setShowResolved((s) => !s)}>
                {showResolved ? 'Hide resolved view' : 'Show resolved components'}
              </button>
              {showResolved && (
                <ResolvedView org={org} artifactId={artifactId} versionId={versionId} />
              )}
            </>
          )}
        </div>
      )}
      <div className="mt-4 flex justify-end">
        <button type="button" className="btn" onClick={onClose}>
          Close
        </button>
      </div>
    </Modal>
  );
}

function ResolvedView({
  org,
  artifactId,
  versionId,
}: {
  org: string;
  artifactId: string;
  versionId: string;
}) {
  const r = useResolvedAgentVersion(org, artifactId, versionId);
  return (
    <QueryGate query={r} noun="resolved components">
      {(data) => (
        <div className="space-y-2">
          <p className="text-sm">
            Resolved spec hash: <code className="font-mono text-xs">{data.spec_hash ?? '-'}</code>
          </p>
          <JsonView value={data} label="Resolved pinned components" />
        </div>
      )}
    </QueryGate>
  );
}

const OP_STYLE: Record<DiffChange['op'], string> = {
  add: 'bg-green-50 text-green-800',
  remove: 'bg-red-50 text-red-800',
  replace: 'bg-amber-50 text-amber-900',
};

const show = (v: unknown) => (v === undefined ? '' : typeof v === 'string' ? v : JSON.stringify(v));

export function DiffViewer({ changes }: { changes: DiffChange[] }) {
  if (changes.length === 0) return <EmptyState>No differences.</EmptyState>;
  return (
    <table className="w-full">
      <caption className="sr-only">Changes</caption>
      <thead className="border-b border-slate-200">
        <tr>
          {['Path', 'Change', 'From', 'To'].map((h) => (
            <th key={h} scope="col" className="th">
              {h}
            </th>
          ))}
        </tr>
      </thead>
      <tbody className="divide-y divide-slate-100">
        {changes.map((c, i) => (
          <tr key={`${c.path}-${i}`}>
            <td className="td font-mono text-xs">{c.path}</td>
            <td className="td">
              <span className={`badge border-transparent ${OP_STYLE[c.op]}`}>{c.op}</span>
            </td>
            <td className="td font-mono text-xs break-all">{show(c.from)}</td>
            <td className="td font-mono text-xs break-all">{show(c.to)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function DiffPanel({ org, kind, artifact }: Pick<PanelProps, 'org' | 'kind' | 'artifact'>) {
  const versions = useVersions(org, kind, artifact.id);
  const [from, setFrom] = useState('');
  const [to, setTo] = useState('');
  const [changes, setChanges] = useState<DiffChange[] | null>(null);
  const { busy, error, run } = useAction();
  const items = (versions.data ?? []).filter((v) => v.state !== 'draft');
  const label = (v: ArtifactVersion) => `v${v.version_no} (${v.state})`;

  async function compare() {
    const r = await run(() => endpoints.diff(org, kind, artifact.id, from, to));
    if (r.ok) setChanges(r.value);
  }

  return (
    <div className="space-y-3">
      <div className="grid gap-3 sm:grid-cols-3">
        <SelectField
          label="From version"
          value={from}
          onChange={setFrom}
          options={[
            { value: '', label: 'Select...' },
            ...items.map((v) => ({ value: v.id, label: label(v) })),
          ]}
        />
        <SelectField
          label="To version"
          value={to}
          onChange={setTo}
          options={[
            { value: '', label: 'Select...' },
            ...(artifact.has_draft ? [{ value: 'draft', label: 'Current draft' }] : []),
            ...items.map((v) => ({ value: v.id, label: label(v) })),
          ]}
        />
        <div className="flex items-end">
          <button
            type="button"
            className="btn btn-primary"
            disabled={!from || !to || busy}
            onClick={() => void compare()}
          >
            {busy ? 'Comparing...' : 'Compare'}
          </button>
        </div>
      </div>
      <ActionError error={error} title="Could not compute diff" />
      {changes && (
        <section aria-label="Diff" className="card p-0">
          <DiffViewer changes={changes} />
        </section>
      )}
    </div>
  );
}
