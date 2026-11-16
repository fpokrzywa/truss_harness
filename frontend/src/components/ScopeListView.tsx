'use client';

import Link from 'next/link';
import { useState, type ReactNode } from 'react';
import type { Workspace } from '@/lib/api/types';
import { ConfirmDialog } from './ConfirmDialog';
import { ErrorMessage } from './ErrorMessage';

interface ScopeListViewProps<T extends Workspace> {
  title: string;
  /** Lowercase singular noun, e.g. "workspace". */
  noun: string;
  items: T[];
  showArchived: boolean;
  onShowArchivedChange: (show: boolean) => void;
  /** Link to the item's dashboard. */
  hrefFor: (item: T) => string;
  /** Extra per-row actions/links (e.g. "Projects" for a workspace). */
  extraActions?: (item: T) => ReactNode;
  archive: (item: T) => Promise<unknown>;
  createForm: ReactNode;
}

/** Shared list + archive pattern for workspaces and projects (NFR-USAB-003). */
export function ScopeListView<T extends Workspace>({
  title,
  noun,
  items,
  showArchived,
  onShowArchivedChange,
  hrefFor,
  extraActions,
  archive,
  createForm,
}: ScopeListViewProps<T>) {
  const [target, setTarget] = useState<T | null>(null);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<unknown>(null);

  const visible = showArchived ? items : items.filter((i) => i.status === 'active');

  async function confirmArchive() {
    if (!target) return;
    setBusy(true);
    setFailure(null);
    try {
      await archive(target);
      setTarget(null);
    } catch (e) {
      setTarget(null);
      setFailure(e);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold text-slate-900">{title}</h1>

      {failure !== null && <ErrorMessage error={failure} title={`Could not archive ${noun}`} />}

      <section aria-label={title} className="card p-0">
        <div className="flex items-center justify-between border-b border-slate-200 px-3 py-2">
          <p className="text-sm text-slate-600" aria-live="polite">
            {visible.length} {visible.length === 1 ? noun : `${noun}s`}
          </p>
          <label className="flex items-center gap-2 text-sm text-slate-700">
            <input
              type="checkbox"
              checked={showArchived}
              onChange={(e) => onShowArchivedChange(e.target.checked)}
            />
            Show archived
          </label>
        </div>
        {visible.length === 0 ? (
          <p className="p-4 text-sm text-slate-600">
            No {noun}s yet. Use the form below to create the first one.
          </p>
        ) : (
          <table className="w-full">
            <caption className="sr-only">{title}</caption>
            <thead className="border-b border-slate-200">
              <tr>
                <th scope="col" className="th">
                  Name
                </th>
                <th scope="col" className="th">
                  Slug
                </th>
                <th scope="col" className="th">
                  Status
                </th>
                <th scope="col" className="th">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {visible.map((item) => (
                <tr key={item.id}>
                  <td className="td">
                    <Link href={hrefFor(item)} className="font-medium text-blue-800 underline">
                      {item.name}
                    </Link>
                    {item.description && (
                      <p className="text-xs text-slate-600">{item.description}</p>
                    )}
                  </td>
                  <td className="td font-mono text-xs">{item.slug}</td>
                  <td className="td">
                    <span
                      className={`badge ${
                        item.status === 'active'
                          ? 'border-green-300 bg-green-50 text-green-800'
                          : 'border-slate-300 bg-slate-100 text-slate-700'
                      }`}
                    >
                      {item.status === 'active' ? 'Active' : 'Archived'}
                    </span>
                  </td>
                  <td className="td">
                    <div className="flex justify-end gap-3">
                      {extraActions?.(item)}
                      {item.status === 'active' && (
                        <button type="button" className="btn" onClick={() => setTarget(item)}>
                          Archive <span className="sr-only">{item.name}</span>
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      {createForm}

      <ConfirmDialog
        open={target !== null}
        title={`Archive ${noun}?`}
        confirmLabel={`Archive ${noun}`}
        busy={busy}
        onConfirm={() => void confirmArchive()}
        onCancel={() => setTarget(null)}
      >
        <p>
          <strong>{target?.name}</strong> will become read-only and disappear from selectors. Its
          data is kept.
        </p>
      </ConfirmDialog>
    </div>
  );
}
