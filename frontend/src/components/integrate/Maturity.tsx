'use client';

import Link from 'next/link';
import type { Evidence, Maturity } from '@/lib/api/integrate-types';
import { Badge, formatDate } from '../admin/ui';

export const PLANNED_LABEL = 'Declared — not validated';

/**
 * Honest capability status (FR-VND-004/005). `planned` is never shown as working: it reads
 * "Declared — not validated". `validated` always carries its evidence when the server supplies it.
 */
export function MaturityBadge({ maturity }: { maturity: Maturity }) {
  if (maturity === 'validated') return <Badge tone="good">Validated</Badge>;
  if (maturity === 'experimental') return <Badge tone="warn">Experimental</Badge>;
  return <Badge tone="neutral">{PLANNED_LABEL}</Badge>;
}

export function EvidenceText({
  evidence,
  org,
  adapterKey,
}: {
  evidence: Evidence | null | undefined;
  org?: string;
  adapterKey?: string | null;
}) {
  if (!evidence) return <span className="text-slate-500">-</span>;
  const text = `Suite ${evidence.suite_version}, ${formatDate(evidence.finished_at)}`;
  if (org && adapterKey) {
    return (
      <Link
        className="text-blue-800 underline"
        href={`/o/${encodeURIComponent(org)}/integrate/adapters#${encodeURIComponent(adapterKey)}`}
      >
        {text}
      </Link>
    );
  }
  return <span>{text}</span>;
}

export interface CapabilityRow {
  capability: string;
  adapter_key?: string | null;
  maturity: Maturity;
  evidence?: Evidence | null;
}

export function CapabilityTable({
  rows,
  org,
  caption,
  showAdapter = false,
}: {
  rows: CapabilityRow[];
  org?: string;
  caption: string;
  showAdapter?: boolean;
}) {
  return (
    <table className="w-full">
      <caption className="sr-only">{caption}</caption>
      <thead className="border-b border-slate-200">
        <tr>
          <th scope="col" className="th">
            Capability
          </th>
          {showAdapter && (
            <th scope="col" className="th">
              Adapter
            </th>
          )}
          <th scope="col" className="th">
            Status
          </th>
          <th scope="col" className="th">
            Evidence
          </th>
        </tr>
      </thead>
      <tbody className="divide-y divide-slate-100">
        {rows.map((r, i) => (
          <tr key={`${r.capability}-${r.adapter_key ?? ''}-${i}`}>
            <td className="td font-medium">{r.capability}</td>
            {showAdapter && <td className="td">{r.adapter_key ?? 'No adapter'}</td>}
            <td className="td">
              <MaturityBadge maturity={r.maturity} />
            </td>
            <td className="td">
              {r.maturity === 'validated' ? (
                <EvidenceText evidence={r.evidence} org={org} adapterKey={r.adapter_key} />
              ) : (
                <span className="text-slate-500">
                  {r.maturity === 'planned' ? 'None: not implemented' : 'None recorded'}
                </span>
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function StatusBadge({ status }: { status: string | null | undefined }) {
  const tone =
    status === 'active' || status === 'healthy' || status === 'pass'
      ? 'good'
      : status === 'draft' || status === 'degraded' || status === 'preview'
        ? 'warn'
        : status === 'fail' || status === 'unavailable'
          ? 'bad'
          : 'neutral';
  return <Badge tone={tone}>{status ?? 'unknown'}</Badge>;
}
