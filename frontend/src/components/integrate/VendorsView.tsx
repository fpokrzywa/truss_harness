'use client';

import { useQuery } from '@tanstack/react-query';
import Link from 'next/link';
import { useState, type FormEvent } from 'react';
import { endpoints } from '@/lib/api/endpoints';
import type { Vendor, VendorCategory, VendorStatus } from '@/lib/api/integrate-types';
import { useCan } from '@/lib/hooks';
import { useRefreshInt } from '@/lib/integrate-hooks';
import { ConfirmDialog } from '../ConfirmDialog';
import {
  ActionError,
  Badge,
  EmptyState,
  Modal,
  ModalActions,
  PageHeader,
  QueryGate,
  SelectField,
  TextField,
  useAction,
} from '../admin/ui';
import { useOrgParam } from '../security/useOrg';
import { StatusBadge } from './Maturity';

export const CATEGORIES: { value: VendorCategory; label: string }[] = [
  { value: 'data_platform', label: 'Data platform' },
  { value: 'model_provider', label: 'Model provider' },
  { value: 'saas_api', label: 'SaaS API' },
  { value: 'mcp', label: 'MCP' },
  { value: 'other', label: 'Other' },
];
const categoryLabel = (c: string) => CATEGORIES.find((x) => x.value === c)?.label ?? c;

export const isGlobalVendor = (v: Vendor): boolean =>
  v.is_global === true || v.scope === 'global' || v.tenant_id === null;

const KEY_RE = /^[a-z][a-z0-9_-]*$/;

export function VendorsView() {
  return <VendorCatalog org={useOrgParam()} />;
}

/** Platform-admin catalog of global vendors (`/api/v1/platform/vendors`). */
export function PlatformVendorsView() {
  return <VendorCatalog org={null} />;
}

function VendorCatalog({ org }: { org: string | null }) {
  const platform = org === null;
  const { can } = useCan(org ?? undefined);
  const refresh = useRefreshInt(org ?? '');
  const [category, setCategory] = useState('');
  const [status, setStatus] = useState('');
  const [origin, setOrigin] = useState('');
  const [editing, setEditing] = useState<Vendor | 'new' | null>(null);
  const [transition, setTransition] = useState<{ vendor: Vendor; to: VendorStatus } | null>(null);
  const action = useAction();

  const vendors = useQuery({
    queryKey: ['int', org ?? '_platform', 'vendors', category, status],
    queryFn: () =>
      platform
        ? endpoints.listPlatformVendors()
        : endpoints.listVendors(org, {
            category: category || undefined,
            status: status || undefined,
          }),
  });
  const canManage = platform || can('vendor:manage');

  async function confirmTransition() {
    if (!transition) return;
    const { vendor, to } = transition;
    const res = await action.run(() =>
      platform
        ? endpoints.updatePlatformVendor(vendor.id, { row_version: vendor.row_version, status: to })
        : endpoints.setVendorStatus(org, vendor, to),
    );
    setTransition(null);
    if (res.ok) {
      if (platform) await vendors.refetch();
      else await refresh('vendors');
    }
  }

  return (
    <div className="space-y-6">
      <PageHeader
        title={platform ? 'Global vendor catalog' : 'Vendors'}
        description={
          platform
            ? 'Vendors visible to every organization. Entries without an adapter are declared only.'
            : 'Global catalog entries and your private vendors. Capability status comes from recorded evidence, never from configuration.'
        }
        actions={
          canManage && (
            <button type="button" className="btn btn-primary" onClick={() => setEditing('new')}>
              {platform ? 'Create global vendor' : 'Create private vendor'}
            </button>
          )
        }
      />
      <ActionError error={action.error} title="Could not change the vendor status" />
      <div className="flex flex-wrap items-end gap-3">
        <SelectField
          label="Category"
          value={category}
          onChange={setCategory}
          options={[{ value: '', label: 'All categories' }, ...CATEGORIES]}
        />
        <SelectField
          label="Status"
          value={status}
          onChange={setStatus}
          options={[
            { value: '', label: 'All statuses' },
            { value: 'active', label: 'Active' },
            { value: 'disabled', label: 'Disabled' },
            { value: 'retired', label: 'Retired' },
          ]}
        />
        {!platform && (
          <SelectField
            label="Source"
            value={origin}
            onChange={setOrigin}
            options={[
              { value: '', label: 'Global and private' },
              { value: 'global', label: 'Global catalog' },
              { value: 'private', label: 'Private' },
            ]}
          />
        )}
      </div>
      <QueryGate query={vendors} noun="vendors">
        {(data) => {
          const rows = data.items.filter(
            (v) => origin === '' || (origin === 'global') === isGlobalVendor(v),
          );
          return (
            <section aria-label="Vendors" className="card p-0">
              {rows.length === 0 ? (
                <EmptyState>No vendors match.</EmptyState>
              ) : (
                <table className="w-full">
                  <caption className="sr-only">Vendors</caption>
                  <thead className="border-b border-slate-200">
                    <tr>
                      {['Name', 'Category', 'Source', 'Version', 'Status'].map((h) => (
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
                    {rows.map((v) => {
                      const global = isGlobalVendor(v);
                      const editable = platform || (!global && can('vendor:manage'));
                      return (
                        <tr key={v.id}>
                          <td className="td font-medium">
                            {platform ? (
                              v.name
                            ) : (
                              <Link
                                className="text-blue-800 underline"
                                href={`/o/${encodeURIComponent(org)}/integrate/vendors/${encodeURIComponent(v.id)}`}
                              >
                                {v.name}
                              </Link>
                            )}
                          </td>
                          <td className="td">{categoryLabel(v.category)}</td>
                          <td className="td">
                            <Badge>{global ? 'Global' : 'Private'}</Badge>
                          </td>
                          <td className="td">{v.version ?? '-'}</td>
                          <td className="td">
                            <StatusBadge status={v.status} />
                          </td>
                          <td className="td">
                            {editable && (
                              <div className="flex justify-end gap-2">
                                <button type="button" className="btn" onClick={() => setEditing(v)}>
                                  Edit <span className="sr-only">{v.name}</span>
                                </button>
                                {v.status !== 'active' && v.status !== 'retired' && (
                                  <button
                                    type="button"
                                    className="btn"
                                    onClick={() => setTransition({ vendor: v, to: 'active' })}
                                  >
                                    Enable <span className="sr-only">{v.name}</span>
                                  </button>
                                )}
                                {v.status === 'active' && (
                                  <button
                                    type="button"
                                    className="btn"
                                    onClick={() => setTransition({ vendor: v, to: 'disabled' })}
                                  >
                                    Disable <span className="sr-only">{v.name}</span>
                                  </button>
                                )}
                                {v.status !== 'retired' && (
                                  <button
                                    type="button"
                                    className="btn"
                                    onClick={() => setTransition({ vendor: v, to: 'retired' })}
                                  >
                                    Retire <span className="sr-only">{v.name}</span>
                                  </button>
                                )}
                              </div>
                            )}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              )}
            </section>
          );
        }}
      </QueryGate>

      {editing && (
        <VendorDialog
          org={org}
          vendor={editing === 'new' ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={async () => {
            if (platform) await vendors.refetch();
            else await refresh('vendors');
          }}
        />
      )}
      <ConfirmDialog
        open={transition !== null}
        title={
          transition?.to === 'retired'
            ? 'Retire vendor?'
            : transition?.to === 'disabled'
              ? 'Disable vendor?'
              : 'Enable vendor?'
        }
        confirmLabel={
          transition?.to === 'retired'
            ? 'Retire vendor'
            : transition?.to === 'disabled'
              ? 'Disable vendor'
              : 'Enable vendor'
        }
        busy={action.busy}
        onConfirm={() => void confirmTransition()}
        onCancel={() => setTransition(null)}
      >
        <p>
          <strong>{transition?.vendor.name}</strong> will be marked {transition?.to}.
          {transition?.to === 'retired' && ' Retirement cannot be undone.'}
        </p>
      </ConfirmDialog>
    </div>
  );
}

function VendorDialog({
  org,
  vendor,
  onClose,
  onSaved,
}: {
  org: string | null;
  vendor: Vendor | null;
  onClose: () => void;
  onSaved: () => Promise<unknown>;
}) {
  const action = useAction();
  const [key, setKey] = useState(vendor?.key ?? '');
  const [name, setName] = useState(vendor?.name ?? '');
  const [category, setCategory] = useState<VendorCategory>(vendor?.category ?? 'saas_api');
  const [description, setDescription] = useState(vendor?.description ?? '');
  const [version, setVersion] = useState(vendor?.version ?? '');
  const [errors, setErrors] = useState<{ key?: string; name?: string }>({});

  async function submit(e: FormEvent) {
    e.preventDefault();
    const next = {
      key:
        !vendor && !KEY_RE.test(key)
          ? 'Use lowercase letters, digits, - and _ (start with a letter).'
          : undefined,
      name: name.trim() === '' ? 'Name is required.' : undefined,
    };
    setErrors(next);
    if (next.key || next.name) return;
    const body = {
      name: name.trim(),
      category,
      ...(description.trim() ? { description: description.trim() } : {}),
      ...(version.trim() ? { version: version.trim() } : {}),
    };
    const res = await action.run(() =>
      vendor
        ? org === null
          ? endpoints.updatePlatformVendor(vendor.id, { ...body, row_version: vendor.row_version })
          : endpoints.updateVendor(org, vendor.id, { ...body, row_version: vendor.row_version })
        : org === null
          ? endpoints.createPlatformVendor({ key, ...body })
          : endpoints.createVendor(org, { key, ...body }),
    );
    if (res.ok) {
      await onSaved();
      onClose();
    }
  }

  return (
    <Modal
      title={vendor ? `Edit ${vendor.name}` : 'Create vendor'}
      onClose={onClose}
      busy={action.busy}
    >
      <form onSubmit={(e) => void submit(e)} className="space-y-4" noValidate>
        {!vendor && (
          <TextField label="Key" value={key} onChange={setKey} error={errors.key} maxLength={80} />
        )}
        <TextField
          label="Name"
          value={name}
          onChange={setName}
          error={errors.name}
          maxLength={200}
        />
        <SelectField
          label="Category"
          value={category}
          onChange={(v) => setCategory(v as VendorCategory)}
          options={CATEGORIES}
        />
        <TextField label="Version" value={version} onChange={setVersion} maxLength={80} />
        <TextField label="Description" value={description} onChange={setDescription} multiline />
        <ActionError error={action.error} title="Could not save the vendor" />
        <ModalActions onCancel={onClose} busy={action.busy} submitLabel="Save vendor" />
      </form>
    </Modal>
  );
}
