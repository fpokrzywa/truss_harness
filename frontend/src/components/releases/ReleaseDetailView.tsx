'use client';

import Link from 'next/link';
import { useState, type FormEvent } from 'react';
import { endpoints } from '@/lib/api/endpoints';
import type { ReleaseOut } from '@/lib/api/packaging-types';
import { useCan } from '@/lib/hooks';
import { useRefreshReleases, useRelease, useReleaseEvaluations } from '@/lib/release-hooks';
import { evaluationsHref, solutionHref } from '@/lib/scope';
import {
  ActionError,
  EmptyState,
  formatDate,
  Modal,
  ModalActions,
  PageHeader,
  QueryGate,
  TextField,
  useAction,
} from '../admin/ui';
import { StatusBadge } from '../integrate/Maturity';
import { useOrgParam } from '../security/useOrg';
import { CompatibilityTable } from './CompatibilityTable';

type Json = Record<string, unknown>;

const isObject = (v: unknown): v is Json =>
  typeof v === 'object' && v !== null && !Array.isArray(v);
const obj = (v: unknown): Json => (isObject(v) ? v : {});
const arr = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);
/** A manifest scalar as plain text; `-` when absent. */
const text = (v: unknown): string =>
  typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean' ? String(v) : '-';

/** One sealed release: digest, manifest, compatibility and evaluations. Plain React text only. */
export function ReleaseDetailView({ id }: { id: string }) {
  const org = useOrgParam();
  const release = useRelease(org, id);
  return (
    <div className="space-y-6">
      <QueryGate query={release} noun="release">
        {(r) => <ReleaseBody org={org} release={r} />}
      </QueryGate>
    </div>
  );
}

function ReleaseBody({ org, release }: { org: string; release: ReleaseOut }) {
  const { can } = useCan(org);
  const refresh = useRefreshReleases(org);
  const evaluate = useAction();
  const [withdrawing, setWithdrawing] = useState(false);
  const sealed = release.status === 'sealed';
  const canEvaluate = sealed && can('evaluation:run:request');
  const canWithdraw = sealed && can('packaging:release:withdraw');

  async function runEvaluation() {
    const res = await evaluate.run(() => endpoints.requestEvaluation(org, release.id));
    if (res.ok) await refresh('evaluations');
  }

  return (
    <>
      <PageHeader
        title={`Release of v${release.version_no}`}
        description="Sealed release"
        actions={
          <>
            {canEvaluate && (
              <button
                type="button"
                className="btn btn-primary"
                disabled={evaluate.busy}
                onClick={() => void runEvaluation()}
              >
                Run evaluation
              </button>
            )}
            {canWithdraw && (
              <button type="button" className="btn" onClick={() => setWithdrawing(true)}>
                Withdraw
              </button>
            )}
          </>
        }
      />
      <ActionError error={evaluate.error} title="Could not request the evaluation" />
      <section aria-label="Release" className="card">
        <dl className="grid gap-x-4 gap-y-1 text-sm sm:grid-cols-[12rem_1fr]">
          <dt className="text-slate-600">Status</dt>
          <dd>
            <StatusBadge status={release.status} />
          </dd>
          <dt className="text-slate-600">Digest</dt>
          <dd className="font-mono text-xs break-all">{release.digest}</dd>
          <dt className="text-slate-600">Solution version</dt>
          <dd>
            {release.workspace && release.project ? (
              <Link
                className="text-blue-800 underline"
                href={solutionHref(org, release.solution_id, release.workspace, release.project)}
              >
                v{release.version_no}
              </Link>
            ) : (
              `v${release.version_no}`
            )}
          </dd>
          <dt className="text-slate-600">Sealed</dt>
          <dd>{formatDate(release.sealed_at)}</dd>
        </dl>
      </section>
      <ManifestSummary manifest={release.manifest} />
      <section aria-label="Compatibility" className="card overflow-x-auto p-0">
        {release.compatibility.length === 0 ? (
          <EmptyState>No compatibility diagnostics.</EmptyState>
        ) : (
          <CompatibilityTable title="Compatibility" diagnostics={release.compatibility} />
        )}
      </section>
      <Evaluations org={org} releaseId={release.id} />
      {withdrawing && (
        <WithdrawDialog org={org} releaseId={release.id} onClose={() => setWithdrawing(false)} />
      )}
    </>
  );
}

function ManifestSummary({ manifest }: { manifest: Json }) {
  const entrypoint = obj(manifest.entrypoint);
  const pins = arr(manifest.pins).map(obj);
  const reads = arr(manifest.data_reads).map(obj);
  const bindings = obj(manifest.bindings);
  const models = Object.entries(obj(bindings.model_bindings));
  const connections = Object.entries(obj(bindings.connection_bindings));
  const evaluation = obj(manifest.evaluation);
  const thresholds = obj(evaluation.thresholds);
  return (
    <section aria-label="Manifest" className="card space-y-4">
      <h2 className="text-base font-semibold">Manifest</h2>
      <dl className="grid gap-x-4 gap-y-1 text-sm sm:grid-cols-[12rem_1fr]">
        <dt className="text-slate-600">Target kind</dt>
        <dd className="font-mono text-xs">{text(manifest.target_kind)}</dd>
        <dt className="text-slate-600">Entrypoint</dt>
        <dd>
          {text(entrypoint.kind)}{' '}
          <span className="font-mono text-xs break-all">{text(entrypoint.version_id)}</span>
        </dd>
        <dt className="text-slate-600">Data reads</dt>
        <dd>
          {reads.length === 0 ? (
            '-'
          ) : (
            <ul>
              {reads.map((r, i) => (
                <li key={i}>
                  <span className="font-mono text-xs">{text(r.connection_id)}</span>:{' '}
                  {arr(r.tables).map(text).join(', ') || '-'}
                </li>
              ))}
            </ul>
          )}
        </dd>
        <dt className="text-slate-600">Model bindings</dt>
        <dd>
          {models.length === 0 ? (
            '-'
          ) : (
            <ul>
              {models.map(([pin, b]) => (
                <li key={pin}>
                  <span className="font-mono text-xs">{pin}</span>: {text(obj(b).model)} (provider{' '}
                  <span className="font-mono text-xs">{text(obj(b).provider_id)}</span>)
                </li>
              ))}
            </ul>
          )}
        </dd>
        <dt className="text-slate-600">Connection bindings</dt>
        <dd>
          {connections.length === 0 ? (
            '-'
          ) : (
            <ul>
              {connections.map(([pin, c]) => (
                <li key={pin}>
                  <span className="font-mono text-xs">{pin}</span>:{' '}
                  <span className="font-mono text-xs">{text(c)}</span>
                </li>
              ))}
            </ul>
          )}
        </dd>
        <dt className="text-slate-600">Evaluation dataset</dt>
        <dd className="font-mono text-xs break-all">{text(evaluation.dataset_id)}</dd>
        <dt className="text-slate-600">Min accuracy</dt>
        <dd>{text(thresholds.min_accuracy)}</dd>
        <dt className="text-slate-600">Max p95 latency (ms)</dt>
        <dd>{text(thresholds.max_p95_latency_ms)}</dd>
        <dt className="text-slate-600">Max total cost (USD)</dt>
        <dd>{text(thresholds.max_total_cost_usd)}</dd>
        <dt className="text-slate-600">Compiler version</dt>
        <dd>{text(manifest.compiler_version)}</dd>
      </dl>
      {pins.length === 0 ? (
        <p className="text-sm text-slate-600">No pins.</p>
      ) : (
        <table className="w-full">
          <caption className="text-left text-sm font-semibold">Pins</caption>
          <thead className="border-b border-slate-200">
            <tr>
              {['Role', 'Version id', 'Spec hash'].map((h) => (
                <th key={h} scope="col" className="th">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {pins.map((p, i) => (
              <tr key={i}>
                <td className="td">{text(p.role)}</td>
                <td className="td font-mono text-xs break-all">{text(p.version_id)}</td>
                <td className="td font-mono text-xs break-all">{text(p.spec_hash)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

function Evaluations({ org, releaseId }: { org: string; releaseId: string }) {
  const runs = useReleaseEvaluations(org, releaseId);
  return (
    <section aria-label="Evaluations" className="card overflow-x-auto p-0">
      <h2 className="px-4 pt-4 text-base font-semibold">Evaluations</h2>
      <QueryGate query={runs} noun="evaluations">
        {(data) =>
          data.items.length === 0 ? (
            <EmptyState>No evaluations of this release yet.</EmptyState>
          ) : (
            <table className="w-full">
              <caption className="sr-only">Evaluations</caption>
              <thead className="border-b border-slate-200">
                <tr>
                  {['Created', 'Status', 'Accuracy', 'p95 latency (ms)', 'Cost (USD)'].map((h) => (
                    <th key={h} scope="col" className="th">
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {data.items.map((r) => (
                  <tr key={r.id}>
                    <td className="td">
                      <Link
                        className="text-blue-800 underline"
                        href={evaluationsHref(org, encodeURIComponent(r.id))}
                      >
                        {formatDate(r.created_at)}
                      </Link>
                    </td>
                    <td className="td">
                      <StatusBadge status={r.status} />
                    </td>
                    <td className="td">{r.accuracy ?? '-'}</td>
                    <td className="td">{r.p95_latency_ms ?? '-'}</td>
                    <td className="td">{r.total_cost_usd ?? '-'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )
        }
      </QueryGate>
    </section>
  );
}

function WithdrawDialog({
  org,
  releaseId,
  onClose,
}: {
  org: string;
  releaseId: string;
  onClose: () => void;
}) {
  const refresh = useRefreshReleases(org);
  const action = useAction();
  const [note, setNote] = useState('');
  const [error, setError] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    const trimmed = note.trim();
    if (trimmed === '' || trimmed.length > 2000) {
      setError('A note of 1 to 2000 characters is required.');
      return;
    }
    setError(null);
    const res = await action.run(() => endpoints.withdrawRelease(org, releaseId, trimmed));
    if (res.ok) {
      await refresh('releases');
      onClose();
    }
  }

  return (
    <Modal title="Withdraw release" onClose={onClose} busy={action.busy}>
      <form onSubmit={(e) => void submit(e)} className="space-y-4" noValidate>
        <p className="text-sm text-slate-700">
          A withdrawn release can no longer be evaluated or planned. This cannot be undone.
        </p>
        <TextField
          label="Note"
          value={note}
          onChange={setNote}
          multiline
          maxLength={2000}
          error={error}
        />
        <ActionError error={action.error} title="Could not withdraw the release" />
        <ModalActions onCancel={onClose} busy={action.busy} submitLabel="Withdraw release" />
      </form>
    </Modal>
  );
}
