'use client';

import Link from 'next/link';
import { useState, type FormEvent } from 'react';
import { endpoints } from '@/lib/api/endpoints';
import { TARGET_KIND, type SolutionOut } from '@/lib/api/packaging-types';
import { useCan } from '@/lib/hooks';
import { useRefreshReleases, useSolutions } from '@/lib/release-hooks';
import { solutionHref } from '@/lib/scope';
import { ConfirmDialog } from '../ConfirmDialog';
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
import { ProjectScopePicker, useProjectQuery } from './ProjectScope';

/** A project's solutions: list, create, archive. Server text renders as plain React text only. */
export function SolutionsView() {
  const org = useOrgParam();
  const { ws, proj } = useProjectQuery();
  const { can } = useCan(org);
  const canWrite = can('packaging:solution:write');
  const [creating, setCreating] = useState(false);
  const chosen = ws !== '' && proj !== '';

  return (
    <div className="space-y-6">
      <PageHeader
        title="Solutions"
        description="Deployable solutions of a project: each version pins an entrypoint, and each release seals a version with its environment bindings."
        actions={
          canWrite &&
          chosen && (
            <button type="button" className="btn btn-primary" onClick={() => setCreating(true)}>
              New solution
            </button>
          )
        }
      />
      <ProjectScopePicker org={org} />
      {chosen ? (
        <SolutionsTable org={org} ws={ws} proj={proj} canWrite={canWrite} />
      ) : (
        <div className="card p-0">
          <EmptyState>Choose a workspace and a project to see its solutions.</EmptyState>
        </div>
      )}
      {creating && (
        <NewSolutionDialog org={org} ws={ws} proj={proj} onClose={() => setCreating(false)} />
      )}
    </div>
  );
}

function SolutionsTable({
  org,
  ws,
  proj,
  canWrite,
}: {
  org: string;
  ws: string;
  proj: string;
  canWrite: boolean;
}) {
  const solutions = useSolutions(org, ws, proj);
  const refresh = useRefreshReleases(org);
  const archive = useAction();
  const [archiving, setArchiving] = useState<SolutionOut | null>(null);

  async function confirmArchive() {
    if (!archiving) return;
    const res = await archive.run(() => endpoints.archiveSolution(org, ws, proj, archiving.id));
    setArchiving(null);
    if (res.ok) await refresh('solutions');
  }

  return (
    <>
      <ActionError error={archive.error} title="Could not archive the solution" />
      <QueryGate query={solutions} noun="solutions">
        {(data) => (
          <div className="card overflow-x-auto p-0">
            {data.items.length === 0 ? (
              <EmptyState>
                No solutions in this project yet.
                {canWrite && ' Use "New solution" to add the first one.'}
              </EmptyState>
            ) : (
              <table className="w-full">
                <caption className="sr-only">Solutions</caption>
                <thead className="border-b border-slate-200">
                  <tr>
                    {['Name', 'Target kind', 'Status', 'Created'].map((h) => (
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
                  {data.items.map((s) => (
                    <tr key={s.id}>
                      <td className="td font-medium">
                        <Link
                          className="text-blue-800 underline"
                          href={solutionHref(org, s.id, ws, proj)}
                        >
                          {s.name}
                        </Link>
                      </td>
                      <td className="td font-mono text-xs">{s.target_kind}</td>
                      <td className="td">
                        <StatusBadge status={s.status} />
                      </td>
                      <td className="td">{formatDate(s.created_at)}</td>
                      <td className="td">
                        {canWrite && s.status === 'active' && (
                          <div className="flex justify-end">
                            <button type="button" className="btn" onClick={() => setArchiving(s)}>
                              Archive <span className="sr-only">{s.name}</span>
                            </button>
                          </div>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        )}
      </QueryGate>
      <ConfirmDialog
        open={archiving !== null}
        title="Archive solution?"
        confirmLabel="Archive solution"
        busy={archive.busy}
        onConfirm={() => void confirmArchive()}
        onCancel={() => setArchiving(null)}
      >
        <p>
          <strong>{archiving?.name}</strong> can no longer get new versions or releases. Existing
          releases stay as they are. This cannot be undone.
        </p>
      </ConfirmDialog>
    </>
  );
}

function NewSolutionDialog({
  org,
  ws,
  proj,
  onClose,
}: {
  org: string;
  ws: string;
  proj: string;
  onClose: () => void;
}) {
  const refresh = useRefreshReleases(org);
  const action = useAction();
  const [name, setName] = useState('');
  const [error, setError] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    const trimmed = name.trim();
    if (trimmed === '' || trimmed.length > 200) {
      setError('Name must be 1 to 200 characters.');
      return;
    }
    setError(null);
    const res = await action.run(() =>
      endpoints.createSolution(org, ws, proj, { name: trimmed, target_kind: TARGET_KIND }),
    );
    if (res.ok) {
      await refresh('solutions');
      onClose();
    }
  }

  return (
    <Modal title="New solution" onClose={onClose} busy={action.busy}>
      <form onSubmit={(e) => void submit(e)} className="space-y-4" noValidate>
        <TextField label="Name" value={name} onChange={setName} error={error} maxLength={200} />
        <p className="text-sm text-slate-700">
          Target kind: <code className="font-mono">{TARGET_KIND}</code>
        </p>
        <ActionError error={action.error} title="Could not create the solution" />
        <ModalActions onCancel={onClose} busy={action.busy} submitLabel="Create solution" />
      </form>
    </Modal>
  );
}
