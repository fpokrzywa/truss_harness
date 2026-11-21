'use client';

import { useId, useState, type ChangeEvent, type FormEvent } from 'react';
import { endpoints } from '@/lib/api/endpoints';
import { useCan } from '@/lib/hooks';
import { useDatasets, useRefreshReleases } from '@/lib/release-hooks';
import { shortDigest, validateCases } from '@/lib/release-utils';
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
import { useOrgParam } from '../security/useOrg';
import { ProjectScopePicker, useProjectQuery } from './ProjectScope';

/** A project's evaluation datasets: list and new versions. Plain React text only. */
export function DatasetsView() {
  const org = useOrgParam();
  const { ws, proj } = useProjectQuery();
  const { can } = useCan(org);
  const canWrite = can('evaluation:dataset:write');
  // null: closed; a string: open, prefilled with that name ('' for a new dataset).
  const [creating, setCreating] = useState<string | null>(null);
  const chosen = ws !== '' && proj !== '';

  return (
    <div className="space-y-6">
      <PageHeader
        title="Evaluation datasets"
        description="Cases a release is evaluated against. Datasets are immutable: posting an existing name adds its next version."
        actions={
          canWrite &&
          chosen && (
            <button type="button" className="btn btn-primary" onClick={() => setCreating('')}>
              New dataset version
            </button>
          )
        }
      />
      <ProjectScopePicker org={org} />
      {chosen ? (
        <DatasetsTable
          org={org}
          ws={ws}
          proj={proj}
          canWrite={canWrite}
          onNewVersion={setCreating}
        />
      ) : (
        <div className="card p-0">
          <EmptyState>Choose a workspace and a project to see its evaluation datasets.</EmptyState>
        </div>
      )}
      {creating !== null && (
        <NewDatasetDialog
          org={org}
          ws={ws}
          proj={proj}
          initialName={creating}
          onClose={() => setCreating(null)}
        />
      )}
    </div>
  );
}

function DatasetsTable({
  org,
  ws,
  proj,
  canWrite,
  onNewVersion,
}: {
  org: string;
  ws: string;
  proj: string;
  canWrite: boolean;
  onNewVersion: (name: string) => void;
}) {
  const datasets = useDatasets(org, ws, proj);
  return (
    <QueryGate query={datasets} noun="datasets">
      {(data) => (
        <div className="card overflow-x-auto p-0">
          {data.items.length === 0 ? (
            <EmptyState>
              No evaluation datasets in this project yet.
              {canWrite && ' Use "New dataset version" to add the first one.'}
            </EmptyState>
          ) : (
            <table className="w-full">
              <caption className="sr-only">Evaluation datasets</caption>
              <thead className="border-b border-slate-200">
                <tr>
                  {['Name', 'Version', 'Cases', 'Digest', 'Created'].map((h) => (
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
                {data.items.map((d) => (
                  <tr key={d.id}>
                    <td className="td font-medium">{d.name}</td>
                    <td className="td">v{d.version_no}</td>
                    <td className="td">{d.cases.length}</td>
                    <td className="td font-mono text-xs" title={d.digest}>
                      {shortDigest(d.digest)}
                    </td>
                    <td className="td">{formatDate(d.created_at)}</td>
                    <td className="td">
                      {canWrite && (
                        <div className="flex justify-end">
                          <button
                            type="button"
                            className="btn"
                            onClick={() => onNewVersion(d.name)}
                          >
                            New version <span className="sr-only">of {d.name}</span>
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
  );
}

function NewDatasetDialog({
  org,
  ws,
  proj,
  initialName,
  onClose,
}: {
  org: string;
  ws: string;
  proj: string;
  initialName: string;
  onClose: () => void;
}) {
  const refresh = useRefreshReleases(org);
  const action = useAction();
  const fileId = useId();
  const [name, setName] = useState(initialName);
  const [cases, setCases] = useState('');
  const [errors, setErrors] = useState<{ name?: string; cases?: string }>({});

  async function upload(e: ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (file) setCases(await file.text());
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    const next: { name?: string; cases?: string } = {};
    const trimmed = name.trim();
    if (trimmed === '' || trimmed.length > 200) next.name = 'Name must be 1 to 200 characters.';
    const checked = validateCases(cases);
    if ('error' in checked) next.cases = checked.error;
    setErrors(next);
    if (next.name || 'error' in checked) return;

    const res = await action.run(() =>
      endpoints.createDataset(org, ws, proj, { name: trimmed, cases: checked.cases }),
    );
    if (res.ok) {
      await refresh('datasets');
      onClose();
    }
  }

  return (
    <Modal title="New dataset version" wide onClose={onClose} busy={action.busy}>
      <form onSubmit={(e) => void submit(e)} className="space-y-4" noValidate>
        <TextField
          label="Name"
          value={name}
          onChange={setName}
          maxLength={200}
          error={errors.name}
          help="An existing name adds that dataset's next version."
        />
        <TextField
          label="Cases (JSON)"
          value={cases}
          onChange={setCases}
          multiline
          monospace
          error={errors.cases}
          help='A JSON array of {"case_id", "input", "assertions"} objects, 1 to 200 cases.'
        />
        <div>
          <label htmlFor={fileId} className="label">
            Or upload a .json file
          </label>
          <input
            id={fileId}
            type="file"
            accept=".json,application/json"
            className="text-sm"
            onChange={(e) => void upload(e)}
          />
        </div>
        <ActionError error={action.error} title="Could not create the dataset" />
        <ModalActions onCancel={onClose} busy={action.busy} submitLabel="Create dataset version" />
      </form>
    </Modal>
  );
}
