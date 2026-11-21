'use client';

import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useState, type FormEvent } from 'react';
import type { ConnectionType } from '@/lib/api/integrate-types';
import { endpoints } from '@/lib/api/endpoints';
import type {
  EntrypointKind,
  EnvironmentBindings,
  SolutionOut,
  SolutionVersionOut,
} from '@/lib/api/packaging-types';
import { useCan } from '@/lib/hooks';
import { useProviders } from '@/lib/integrate-hooks';
import {
  useDatasets,
  useRefreshReleases,
  useSolution,
  useSolutionVersions,
  useVersionReleases,
} from '@/lib/release-hooks';
import { compatibilityReport, shortDigest } from '@/lib/release-utils';
import { datasetsHref, releaseHref, solutionsHref } from '@/lib/scope';
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
import { ConnectionSelect, VersionPicker } from '../build/pickers';
import { StatusBadge } from '../integrate/Maturity';
import { useOrgParam } from '../security/useOrg';
import { CompatibilityTable } from './CompatibilityTable';
import { useProjectQuery } from './ProjectScope';

const DECIMAL = /^[0-9]{1,12}(\.[0-9]{1,12})?$/;
const POSITIVE_INT = /^[1-9][0-9]*$/;
const DATA_CONNECTIONS: ConnectionType[] = ['database', 'data_platform'];
const ALL_CONNECTIONS: ConnectionType[] = ['rest', 'mcp', 'database', 'data_platform', 'model_api'];

/** One solution: its versions, their releases, new versions and sealing. Plain React text only. */
export function SolutionDetailView({ id }: { id: string }) {
  const org = useOrgParam();
  const { ws, proj } = useProjectQuery();
  if (!ws || !proj) {
    return (
      <div className="card p-0">
        <EmptyState>
          This link does not name a project. Open the solution from the{' '}
          <Link className="text-blue-800 underline" href={solutionsHref(org)}>
            Solutions
          </Link>{' '}
          list.
        </EmptyState>
      </div>
    );
  }
  return <SolutionDetail org={org} ws={ws} proj={proj} id={id} />;
}

interface Scope {
  org: string;
  ws: string;
  proj: string;
}

function SolutionDetail({ id, ...scope }: Scope & { id: string }) {
  const solution = useSolution(scope.org, scope.ws, scope.proj, id);
  return (
    <div className="space-y-6">
      <QueryGate query={solution} noun="solution">
        {(s) => <SolutionBody scope={scope} solution={s} />}
      </QueryGate>
    </div>
  );
}

function SolutionBody({ scope, solution }: { scope: Scope; solution: SolutionOut }) {
  const { org, ws, proj } = scope;
  const { can } = useCan(org);
  const versions = useSolutionVersions(org, ws, proj, solution.id);
  const [creating, setCreating] = useState(false);
  const [sealing, setSealing] = useState<SolutionVersionOut | null>(null);
  const canWrite = can('packaging:solution:write') && solution.status === 'active';
  const canSeal = can('packaging:release:seal') && solution.status === 'active';

  return (
    <>
      <PageHeader
        title={solution.name}
        description="Solution"
        actions={
          canWrite && (
            <button type="button" className="btn btn-primary" onClick={() => setCreating(true)}>
              New version
            </button>
          )
        }
      />
      <section aria-label="Solution" className="card">
        <dl className="grid gap-x-4 gap-y-1 text-sm sm:grid-cols-[12rem_1fr]">
          <dt className="text-slate-600">Status</dt>
          <dd>
            <StatusBadge status={solution.status} />
          </dd>
          <dt className="text-slate-600">Target kind</dt>
          <dd className="font-mono text-xs">{solution.target_kind}</dd>
          <dt className="text-slate-600">Workspace / project</dt>
          <dd>
            {solution.workspace ?? '-'} / {solution.project ?? '-'}
          </dd>
          <dt className="text-slate-600">Created</dt>
          <dd>{formatDate(solution.created_at)}</dd>
        </dl>
      </section>

      <QueryGate query={versions} noun="versions">
        {(data) =>
          data.items.length === 0 ? (
            <div className="card p-0">
              <EmptyState>No versions yet.</EmptyState>
            </div>
          ) : (
            <>
              <section aria-label="Versions" className="card overflow-x-auto p-0">
                <table className="w-full">
                  <caption className="px-4 pt-4 text-left text-base font-semibold">
                    Versions
                  </caption>
                  <thead className="border-b border-slate-200">
                    <tr>
                      {['Version', 'Entrypoint', 'Pins', 'Evaluation contract', 'Created'].map(
                        (h) => (
                          <th key={h} scope="col" className="th">
                            {h}
                          </th>
                        ),
                      )}
                      <th scope="col" className="th">
                        <span className="sr-only">Actions</span>
                      </th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100">
                    {data.items.map((v) => (
                      <tr key={v.id}>
                        <td className="td">v{v.version_no}</td>
                        <td className="td">{v.entrypoint.kind}</td>
                        <td className="td">{v.pins.length}</td>
                        <td className="td">{v.spec.evaluation ? 'Set' : 'Not set'}</td>
                        <td className="td">{formatDate(v.created_at)}</td>
                        <td className="td">
                          {canSeal && (
                            <div className="flex justify-end">
                              <button type="button" className="btn" onClick={() => setSealing(v)}>
                                Seal release <span className="sr-only">of v{v.version_no}</span>
                              </button>
                            </div>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </section>
              {data.items.map((v) => (
                <VersionReleases key={v.id} scope={scope} solutionId={solution.id} version={v} />
              ))}
            </>
          )
        }
      </QueryGate>

      {creating && (
        <NewVersionDialog
          scope={scope}
          solutionId={solution.id}
          onClose={() => setCreating(false)}
        />
      )}
      {sealing && (
        <SealDialog
          scope={scope}
          solutionId={solution.id}
          version={sealing}
          onClose={() => setSealing(null)}
        />
      )}
    </>
  );
}

function VersionReleases({
  scope,
  solutionId,
  version,
}: {
  scope: Scope;
  solutionId: string;
  version: SolutionVersionOut;
}) {
  const { org, ws, proj } = scope;
  const releases = useVersionReleases(org, ws, proj, solutionId, version.id);
  const title = `Releases of v${version.version_no}`;
  return (
    <section aria-label={title} className="card overflow-x-auto p-0">
      <h2 className="px-4 pt-4 text-base font-semibold">{title}</h2>
      <QueryGate query={releases} noun="releases">
        {(data) =>
          data.items.length === 0 ? (
            <EmptyState>Not released yet.</EmptyState>
          ) : (
            <table className="w-full">
              <caption className="sr-only">{title}</caption>
              <thead className="border-b border-slate-200">
                <tr>
                  {['Digest', 'Status', 'Sealed'].map((h) => (
                    <th key={h} scope="col" className="th">
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {data.items.map((r) => (
                  <tr key={r.id}>
                    <td className="td font-mono text-xs">
                      <Link
                        className="text-blue-800 underline"
                        href={releaseHref(org, r.id)}
                        title={r.digest}
                      >
                        {shortDigest(r.digest)}
                      </Link>
                    </td>
                    <td className="td">
                      <StatusBadge status={r.status} />
                    </td>
                    <td className="td">{formatDate(r.sealed_at)}</td>
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

// ---- New version -------------------------------------------------------------------------

interface DataReadRow {
  connection_id: string;
  tables: string;
}

const splitLines = (text: string) =>
  text
    .split(/\r?\n/)
    .map((t) => t.trim())
    .filter((t) => t !== '');

function NewVersionDialog({
  scope,
  solutionId,
  onClose,
}: {
  scope: Scope;
  solutionId: string;
  onClose: () => void;
}) {
  const { org, ws, proj } = scope;
  const refresh = useRefreshReleases(org);
  const datasets = useDatasets(org, ws, proj);
  const action = useAction();
  const [kind, setKind] = useState<EntrypointKind>('agent');
  const [entrypoint, setEntrypoint] = useState('');
  const [reads, setReads] = useState<DataReadRow[]>([]);
  const [withEvaluation, setWithEvaluation] = useState(false);
  const [datasetId, setDatasetId] = useState('');
  const [minAccuracy, setMinAccuracy] = useState('');
  const [maxLatency, setMaxLatency] = useState('');
  const [maxCost, setMaxCost] = useState('');
  const [errors, setErrors] = useState<Record<string, string>>({});

  async function submit(e: FormEvent) {
    e.preventDefault();
    const next: Record<string, string> = {};
    if (entrypoint === '') next.entrypoint = 'Choose a published entrypoint version.';
    reads.forEach((r, i) => {
      if (r.connection_id === '') next[`read-${i}-connection`] = 'Choose a connection.';
      if (splitLines(r.tables).length === 0) next[`read-${i}-tables`] = 'List at least one table.';
    });
    if (withEvaluation) {
      if (datasetId === '') next.dataset = 'Choose a dataset.';
      const acc = minAccuracy.trim();
      if (!DECIMAL.test(acc) || Number(acc) > 1)
        next.minAccuracy = 'Enter a decimal between 0 and 1.';
      if (!POSITIVE_INT.test(maxLatency.trim()))
        next.maxLatency = 'Enter a positive whole number of milliseconds.';
      if (!DECIMAL.test(maxCost.trim())) next.maxCost = 'Enter a decimal amount in USD.';
    }
    setErrors(next);
    if (Object.keys(next).length > 0) return;

    const res = await action.run(() =>
      endpoints.createSolutionVersion(org, ws, proj, solutionId, {
        entrypoint_kind: kind,
        entrypoint_version_id: entrypoint,
        data_reads: reads.map((r) => ({
          connection_id: r.connection_id,
          tables: splitLines(r.tables),
        })),
        evaluation: withEvaluation
          ? {
              dataset_id: datasetId,
              min_accuracy: minAccuracy.trim(),
              max_p95_latency_ms: Number(maxLatency.trim()),
              max_total_cost_usd: maxCost.trim(),
            }
          : null,
      }),
    );
    if (res.ok) {
      await refresh('versions');
      onClose();
    }
  }

  const setRead = (i: number, patch: Partial<DataReadRow>) =>
    setReads((rows) => rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));

  return (
    <Modal title="New version" wide onClose={onClose} busy={action.busy}>
      <form onSubmit={(e) => void submit(e)} className="space-y-4" noValidate>
        <SelectField
          label="Entrypoint kind"
          value={kind}
          onChange={(v) => {
            setKind(v as EntrypointKind);
            setEntrypoint('');
          }}
          options={[
            { value: 'agent', label: 'Agent' },
            { value: 'pipeline', label: 'Pipeline' },
          ]}
        />
        <div>
          <VersionPicker
            key={kind}
            org={org}
            kind={kind === 'agent' ? 'agents' : 'pipelines'}
            label="Entrypoint"
            value={entrypoint}
            onChange={setEntrypoint}
          />
          {errors.entrypoint && <p className="mt-1 text-sm text-red-700">{errors.entrypoint}</p>}
        </div>

        <fieldset className="space-y-3">
          <legend className="label">Data reads</legend>
          {reads.map((r, i) => (
            <div key={i} className="space-y-2 rounded-md border border-slate-200 p-2">
              <div>
                <ConnectionSelect
                  org={org}
                  label={`Data read ${i + 1}: connection`}
                  types={DATA_CONNECTIONS}
                  value={r.connection_id}
                  onChange={(v) => setRead(i, { connection_id: v })}
                />
                {errors[`read-${i}-connection`] && (
                  <p className="mt-1 text-sm text-red-700">{errors[`read-${i}-connection`]}</p>
                )}
              </div>
              <TextField
                label={`Data read ${i + 1}: tables`}
                value={r.tables}
                onChange={(v) => setRead(i, { tables: v })}
                multiline
                monospace
                help="One table per line."
                error={errors[`read-${i}-tables`]}
              />
              <button
                type="button"
                className="btn"
                onClick={() => setReads((rows) => rows.filter((_, j) => j !== i))}
              >
                Remove data read {i + 1}
              </button>
            </div>
          ))}
          <button
            type="button"
            className="btn"
            onClick={() => setReads((rows) => [...rows, { connection_id: '', tables: '' }])}
          >
            Add data read
          </button>
        </fieldset>

        <fieldset className="space-y-3">
          <legend className="label">Evaluation contract</legend>
          <label className="flex items-center gap-2 text-sm font-medium text-slate-800">
            <input
              type="checkbox"
              checked={withEvaluation}
              onChange={(e) => setWithEvaluation(e.target.checked)}
            />
            Set an evaluation contract
          </label>
          {withEvaluation && (
            <>
              <div>
                <SelectField
                  label="Dataset"
                  value={datasetId}
                  onChange={setDatasetId}
                  options={[
                    { value: '', label: 'Select...' },
                    ...(datasets.data?.items ?? []).map((d) => ({
                      value: d.id,
                      label: `${d.name} v${d.version_no}`,
                    })),
                  ]}
                />
                {datasets.isSuccess && datasets.data.items.length === 0 && (
                  <p className="mt-1 text-sm text-slate-600">
                    This project has no datasets yet.{' '}
                    <Link className="text-blue-800 underline" href={datasetsHref(org, ws, proj)}>
                      Add a dataset
                    </Link>
                  </p>
                )}
                {errors.dataset && <p className="mt-1 text-sm text-red-700">{errors.dataset}</p>}
              </div>
              <div className="grid gap-3 sm:grid-cols-3">
                <TextField
                  label="Min accuracy"
                  value={minAccuracy}
                  onChange={setMinAccuracy}
                  help="Between 0 and 1."
                  error={errors.minAccuracy}
                />
                <TextField
                  label="Max p95 latency (ms)"
                  value={maxLatency}
                  onChange={setMaxLatency}
                  error={errors.maxLatency}
                />
                <TextField
                  label="Max total cost (USD)"
                  value={maxCost}
                  onChange={setMaxCost}
                  error={errors.maxCost}
                />
              </div>
            </>
          )}
        </fieldset>
        <ActionError error={action.error} title="Could not create the version" />
        <ModalActions onCancel={onClose} busy={action.busy} submitLabel="Create version" />
      </form>
    </Modal>
  );
}

// ---- Seal release ------------------------------------------------------------------------

interface ModelRow {
  pin: string;
  provider_id: string;
  model: string;
}

interface ConnectionRow {
  pin: string;
  connection_id: string;
}

function SealDialog({
  scope,
  solutionId,
  version,
  onClose,
}: {
  scope: Scope;
  solutionId: string;
  version: SolutionVersionOut;
  onClose: () => void;
}) {
  const { org, ws, proj } = scope;
  const router = useRouter();
  const providers = useProviders(org);
  const action = useAction();
  const [models, setModels] = useState<ModelRow[]>([]);
  const [connections, setConnections] = useState<ConnectionRow[]>([]);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const report = compatibilityReport(action.error);

  /** Adds an empty binding row for every unbound model or connection pin the report names. */
  function addMissing(error: unknown) {
    const missing = (compatibilityReport(error) ?? []).filter((d) => d.status === 'unsupported');
    const pins = (prefix: string) =>
      missing.filter((d) => d.path.startsWith(prefix)).map((d) => d.path.slice(prefix.length));
    const modelPins = pins('models.');
    const connectionPins = pins('connections.');
    setModels((rows) => [
      ...rows,
      ...modelPins
        .filter((p) => !rows.some((r) => r.pin === p))
        .map((pin) => ({ pin, provider_id: '', model: '' })),
    ]);
    setConnections((rows) => [
      ...rows,
      ...connectionPins
        .filter((p) => !rows.some((r) => r.pin === p))
        .map((pin) => ({ pin, connection_id: '' })),
    ]);
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    const next: Record<string, string> = {};
    models.forEach((m, i) => {
      if (m.pin.trim() === '') next[`model-${i}-pin`] = 'Pin name is required.';
      if (m.provider_id === '') next[`model-${i}-provider`] = 'Choose a provider.';
      if (m.model.trim() === '') next[`model-${i}-model`] = 'Model is required.';
    });
    connections.forEach((c, i) => {
      if (c.pin.trim() === '') next[`conn-${i}-pin`] = 'Pin name is required.';
      if (c.connection_id === '') next[`conn-${i}-connection`] = 'Choose a connection.';
    });
    // Bindings are keyed by the trimmed pin, so duplicate rows would silently collapse into one.
    const flagDuplicates = (pins: string[], prefix: string, message: string) =>
      pins.forEach((pin, i) => {
        if (pin !== '' && pins.indexOf(pin) !== pins.lastIndexOf(pin))
          next[`${prefix}-${i}-pin`] = message;
      });
    flagDuplicates(
      models.map((m) => m.pin.trim()),
      'model',
      'Another model binding uses this pin name.',
    );
    flagDuplicates(
      connections.map((c) => c.pin.trim()),
      'conn',
      'Another connection binding uses this pin name.',
    );
    setErrors(next);
    if (Object.keys(next).length > 0) return;

    const bindings: EnvironmentBindings = {
      model_bindings: Object.fromEntries(
        models.map((m) => [m.pin.trim(), { provider_id: m.provider_id, model: m.model.trim() }]),
      ),
      connection_bindings: Object.fromEntries(
        connections.map((c) => [c.pin.trim(), c.connection_id]),
      ),
    };
    const res = await action.run(async () => {
      try {
        return await endpoints.sealRelease(org, ws, proj, solutionId, version.id, {
          environment_bindings: bindings,
        });
      } catch (err) {
        addMissing(err);
        throw err;
      }
    });
    if (res.ok) router.push(releaseHref(org, res.value.id));
  }

  const providerOptions = [
    { value: '', label: 'Select a provider...' },
    ...(providers.data?.items ?? []).map((p) => ({ value: p.id, label: p.name })),
  ];
  const setModel = (i: number, patch: Partial<ModelRow>) =>
    setModels((rows) => rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  const setConnection = (i: number, patch: Partial<ConnectionRow>) =>
    setConnections((rows) => rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));

  return (
    <Modal
      title={`Seal release of v${version.version_no}`}
      wide
      onClose={onClose}
      busy={action.busy}
    >
      <form onSubmit={(e) => void submit(e)} className="space-y-4" noValidate>
        <p className="text-sm text-slate-700">
          Bind each model and connection pin of this version to this environment. Pin names are
          model ids (or <code className="font-mono">route-&lt;id&gt;</code>) and connection ids; a
          refused seal lists the missing ones.
        </p>
        <fieldset className="space-y-3">
          <legend className="label">Model bindings</legend>
          {models.map((m, i) => (
            <div
              key={i}
              className="grid gap-2 rounded-md border border-slate-200 p-2 sm:grid-cols-3"
            >
              <TextField
                label={`Model pin ${i + 1}`}
                value={m.pin}
                onChange={(v) => setModel(i, { pin: v })}
                monospace
                maxLength={200}
                error={errors[`model-${i}-pin`]}
              />
              <div>
                <SelectField
                  label={`Provider ${i + 1}`}
                  value={m.provider_id}
                  onChange={(v) => setModel(i, { provider_id: v })}
                  options={providerOptions}
                />
                {errors[`model-${i}-provider`] && (
                  <p className="mt-1 text-sm text-red-700">{errors[`model-${i}-provider`]}</p>
                )}
              </div>
              <TextField
                label={`Model ${i + 1}`}
                value={m.model}
                onChange={(v) => setModel(i, { model: v })}
                maxLength={300}
                error={errors[`model-${i}-model`]}
              />
              <button
                type="button"
                className="btn sm:col-span-3 sm:justify-self-start"
                onClick={() => setModels((rows) => rows.filter((_, j) => j !== i))}
              >
                Remove model binding {i + 1}
              </button>
            </div>
          ))}
          <button
            type="button"
            className="btn"
            onClick={() => setModels((rows) => [...rows, { pin: '', provider_id: '', model: '' }])}
          >
            Add model binding
          </button>
        </fieldset>
        <fieldset className="space-y-3">
          <legend className="label">Connection bindings</legend>
          {connections.map((c, i) => (
            <div
              key={i}
              className="grid gap-2 rounded-md border border-slate-200 p-2 sm:grid-cols-2"
            >
              <TextField
                label={`Connection pin ${i + 1}`}
                value={c.pin}
                onChange={(v) => setConnection(i, { pin: v })}
                monospace
                maxLength={200}
                error={errors[`conn-${i}-pin`]}
              />
              <div>
                <ConnectionSelect
                  org={org}
                  label={`Connection ${i + 1}`}
                  types={ALL_CONNECTIONS}
                  value={c.connection_id}
                  onChange={(v) => setConnection(i, { connection_id: v })}
                />
                {errors[`conn-${i}-connection`] && (
                  <p className="mt-1 text-sm text-red-700">{errors[`conn-${i}-connection`]}</p>
                )}
              </div>
              <button
                type="button"
                className="btn sm:col-span-2 sm:justify-self-start"
                onClick={() => setConnections((rows) => rows.filter((_, j) => j !== i))}
              >
                Remove connection binding {i + 1}
              </button>
            </div>
          ))}
          <button
            type="button"
            className="btn"
            onClick={() => setConnections((rows) => [...rows, { pin: '', connection_id: '' }])}
          >
            Add connection binding
          </button>
        </fieldset>
        {report ? (
          <div className="rounded-md border border-red-300 overflow-x-auto">
            <p className="px-4 pt-3 text-sm text-red-900">
              The release is not compatible with its target. Bind the missing pins and seal again.
            </p>
            <CompatibilityTable title="Compatibility report" diagnostics={report} />
          </div>
        ) : (
          <ActionError error={action.error} title="Could not seal the release" />
        )}
        <ModalActions onCancel={onClose} busy={action.busy} submitLabel="Seal release" />
      </form>
    </Modal>
  );
}
