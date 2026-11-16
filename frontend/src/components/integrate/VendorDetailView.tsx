'use client';

import Link from 'next/link';
import { useVendor, useVendorCapabilities } from '@/lib/integrate-hooks';
import { PageHeader, QueryGate } from '../admin/ui';
import { useOrgParam } from '../security/useOrg';
import { CapabilityTable, PLANNED_LABEL, StatusBadge } from './Maturity';
import { isGlobalVendor } from './VendorsView';

export function VendorDetailView({ id }: { id: string }) {
  const org = useOrgParam();
  const vendor = useVendor(org, id);
  const caps = useVendorCapabilities(org, id);

  return (
    <div className="space-y-6">
      <p className="text-sm">
        <Link
          className="text-blue-800 underline"
          href={`/o/${encodeURIComponent(org)}/integrate/vendors`}
        >
          Back to vendors
        </Link>
      </p>
      <QueryGate query={vendor} noun="vendor">
        {(v) => (
          <>
            <PageHeader title={v.name} description={v.description ?? undefined} />
            <dl className="card grid gap-3 text-sm sm:grid-cols-2">
              <div>
                <dt className="font-medium text-slate-600">Key</dt>
                <dd className="font-mono">{v.key}</dd>
              </div>
              <div>
                <dt className="font-medium text-slate-600">Status</dt>
                <dd>
                  <StatusBadge status={v.status} />
                </dd>
              </div>
              <div>
                <dt className="font-medium text-slate-600">Source</dt>
                <dd>
                  {isGlobalVendor(v)
                    ? 'Global catalog (read-only)'
                    : 'Private to this organization'}
                </dd>
              </div>
              <div>
                <dt className="font-medium text-slate-600">Version</dt>
                <dd>{v.version ?? '-'}</dd>
              </div>
              {v.configuration_requirements && v.configuration_requirements.length > 0 && (
                <div className="sm:col-span-2">
                  <dt className="font-medium text-slate-600">Configuration requirements</dt>
                  <dd>
                    <ul className="list-disc pl-5">
                      {v.configuration_requirements.map((r) => (
                        <li key={r}>{r}</li>
                      ))}
                    </ul>
                  </dd>
                </div>
              )}
            </dl>
          </>
        )}
      </QueryGate>
      <section aria-labelledby="cap-heading" className="card p-0">
        <h2 id="cap-heading" className="px-4 pt-4 text-lg font-semibold">
          Capability matrix
        </h2>
        <p className="px-4 pb-2 text-sm text-slate-600">
          Derived from the adapter registry and recorded conformance evidence. &quot;{PLANNED_LABEL}
          &quot; means the capability is declared only and does not work today.
        </p>
        <QueryGate query={caps} noun="capabilities">
          {(rows) =>
            rows.length === 0 ? (
              <p className="p-4 text-sm text-slate-600">
                No capabilities are declared for this vendor.
              </p>
            ) : (
              <CapabilityTable rows={rows} org={org} caption="Capability matrix" showAdapter />
            )
          }
        </QueryGate>
      </section>
    </div>
  );
}
