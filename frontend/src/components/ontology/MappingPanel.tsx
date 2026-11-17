'use client';

import { useMemo, useState } from 'react';
import { endpoints } from '@/lib/api/endpoints';
import type {
  Mapping,
  MappingCheck,
  MappingTargetKind,
  ObjectType,
  OntologySpec,
  Relationship,
} from '@/lib/api/ontology-types';
import type { DiscoveredTable } from '@/lib/discovery';
import { useConnections } from '@/lib/integrate-hooks';
import {
  buildMapping,
  checkFilter,
  draftFromMapping,
  relationId,
  typeCompat,
  type MappingDraft,
} from '@/lib/ontology-spec';
import { useConnectionTables, useMappings, useRefreshOntology } from '@/lib/ontology-hooks';
import { ActionError, Badge, SelectField, TextField, useAction } from '../admin/ui';
import { QueryError } from '../QueryError';
import { Section } from '../build/shared';

const SOURCE_TYPES = ['database', 'data_platform'];

export interface MappingPanelProps {
  org: string;
  ontologyId: string;
  spec: OntologySpec;
  target: { kind: MappingTargetKind; key: string };
  readOnly: boolean;
  /** True when the working copy is unsaved or no draft exists: mappings attach to the saved draft. */
  needsSave: boolean;
}

/** Mapping panel for the selected object type or relationship (physical layer, contract section 2). */
export function MappingPanel(props: MappingPanelProps) {
  const { org, ontologyId, target, needsSave } = props;
  const mappings = useMappings(org, ontologyId, 'draft', !needsSave);
  const existing = mappings.data?.find(
    (m) => m.target_kind === target.kind && m.target_key === target.key,
  );
  return (
    <Section
      title={`Mapping: ${target.key}`}
      help="Maps this logical definition onto a table in a source connection. Mappings are stored with the draft and frozen at publish."
    >
      <p className="text-sm text-slate-700">
        Checking a mapping reads schema metadata only (relations and column names and types). No
        rows are read or copied into this platform; data is queried live from the source at run
        time.
      </p>
      {needsSave ? (
        <p role="status" className="text-sm text-amber-900">
          Save the draft before mapping: mappings attach to the saved draft and its object types.
        </p>
      ) : mappings.isPending ? (
        <p role="status" className="text-sm text-slate-600">
          Loading mapping...
        </p>
      ) : mappings.isError ? (
        <QueryError error={mappings.error} />
      ) : (
        <MappingForm
          // Re-initialise when switching targets (the form owns its edits otherwise).
          key={`${target.kind}:${target.key}`}
          {...props}
          existing={existing}
        />
      )}
    </Section>
  );
}

function MappingForm({
  org,
  ontologyId,
  spec,
  target,
  readOnly,
  existing,
}: MappingPanelProps & { existing: Mapping | undefined }) {
  const refresh = useRefreshOntology(org, ontologyId);
  const [d, setD] = useState<MappingDraft>(() => draftFromMapping(existing));
  const [check, setCheck] = useState<MappingCheck | null>(null);
  const save = useAction();
  const del = useAction();
  const test = useAction();
  const [saved, setSaved] = useState(false);

  const ot: ObjectType | undefined =
    target.kind === 'object_type' ? spec.object_types.find((o) => o.key === target.key) : undefined;
  const rel: Relationship | undefined =
    target.kind === 'relationship'
      ? spec.relationships.find((r) => r.key === target.key)
      : undefined;

  const connections = useConnections(org);
  const sources = (connections.data?.items ?? []).filter(
    (c) => SOURCE_TYPES.includes(c.connection_type) && c.status === 'active',
  );
  const disco = useConnectionTables(org, d.connectionId);
  const find = (r: { schema: string; name: string }): DiscoveredTable | undefined =>
    disco.tables.find((t) => t.name === r.name && (t.schema ?? '') === r.schema);
  const table = find(d.relation);
  const linkTable = find(d.linkRelation);

  const filterErrors = d.filters.map((f) => (f.trim() ? checkFilter(f) : { ok: true as const }));
  const filtersOk = filterErrors.every((r) => r.ok);
  const relationSet =
    d.keyMode === 'link' && target.kind === 'relationship'
      ? !!d.linkRelation.name
      : !!d.relation.name;
  const keysOk = target.kind === 'object_type' || (!!d.fromColumn && !!d.toColumn);
  const canSave = !!d.connectionId && relationSet && filtersOk && keysOk && !readOnly;

  const set = (patch: Partial<MappingDraft>) => {
    setD((cur) => ({ ...cur, ...patch }));
    setSaved(false);
  };

  function pickRelation(id: string, which: 'relation' | 'linkRelation') {
    const t = disco.tables.find((x) => relationId(x) === id);
    const r = t ? { schema: t.schema ?? '', name: t.name } : { schema: '', name: '' };
    if (which === 'relation' && ot && t) {
      // Prefill columns whose name matches a property; leave the rest for the user.
      const colMap: Record<string, string> = {};
      for (const p of ot.properties) {
        const c = t.columns.find((col) => col.name.toLowerCase() === p.key.toLowerCase());
        if (c) colMap[p.key] = c.name;
      }
      set({ relation: r, columnMap: colMap });
    } else set({ [which]: r });
  }

  async function doSave() {
    const body = buildMapping(target.kind, target.key, d);
    const r = await save.run(() => endpoints.putMapping(org, ontologyId, body));
    if (r.ok) {
      setSaved(true);
      await refresh();
    }
  }

  async function doDelete() {
    const r = await del.run(() =>
      endpoints.deleteMapping(org, ontologyId, target.kind, target.key),
    );
    if (r.ok) {
      setD(draftFromMapping(undefined));
      setCheck(null);
      await refresh();
    }
  }

  async function doCheck() {
    const r = await test.run(() =>
      endpoints.checkMapping(org, ontologyId, target.kind, target.key),
    );
    if (r.ok) setCheck(r.value);
  }

  const mismatches = useMemo(() => {
    if (!ot || !table) return [];
    return ot.properties.flatMap((p) => {
      const col = d.columnMap[p.key];
      const colType = table.columns.find((c) => c.name === col)?.type;
      return col && typeCompat(p.type, colType) === 'incompatible'
        ? [`${p.key} (${p.type}) is mapped to ${col} (${colType})`]
        : [];
    });
  }, [ot, table, d.columnMap]);

  const colOptions = (t: DiscoveredTable | undefined) => [
    { value: '', label: 'Select column...' },
    ...(t?.columns.map((c) => ({
      value: c.name,
      label: c.type ? `${c.name} (${c.type})` : c.name,
    })) ?? []),
  ];
  const relOptions = [
    { value: '', label: 'Select relation...' },
    ...disco.tables.map((t) => ({ value: relationId(t), label: relationId(t) })),
  ];

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        {existing ? (
          <Badge tone="good">Mapped{existing.status ? `: ${existing.status}` : ''}</Badge>
        ) : (
          <Badge tone="warn">Not mapped</Badge>
        )}
        {ot && <span>{ot.label}</span>}
        {rel && (
          <span>
            {rel.label}: {rel.from} to {rel.to}
          </span>
        )}
      </div>

      <SelectField
        label="Connection"
        value={d.connectionId}
        disabled={readOnly}
        onChange={(v) =>
          set({
            connectionId: v,
            relation: { schema: '', name: '' },
            linkRelation: { schema: '', name: '' },
            columnMap: {},
            fromColumn: '',
            toColumn: '',
          })
        }
        options={[
          { value: '', label: 'Select a database or data platform connection...' },
          ...sources.map((c) => ({ value: c.id, label: `${c.name} (${c.connection_type})` })),
        ]}
      />
      {connections.isSuccess && sources.length === 0 && (
        <p className="text-sm text-slate-600">
          No active database or data platform connections. Create one under Integrate, then run
          schema discovery.
        </p>
      )}

      {d.connectionId && disco.isPending && (
        <p role="status" className="text-sm text-slate-600">
          Loading discovered relations...
        </p>
      )}
      {d.connectionId && disco.isError && <QueryError error={disco.error} />}
      {d.connectionId && disco.noDiscovery && (
        <p role="status" className="text-sm text-amber-900">
          This connection has no schema discovery yet. Run discovery on the connection first so
          relations and columns can be picked.
        </p>
      )}

      {d.connectionId && disco.isSuccess && !disco.noDiscovery && (
        <>
          {target.kind === 'relationship' && (
            <SelectField
              label="Relationship key mapping"
              value={d.keyMode}
              disabled={readOnly}
              onChange={(v) => set({ keyMode: v as 'direct' | 'link' })}
              options={[
                { value: 'direct', label: 'Direct from/to columns' },
                { value: 'link', label: 'Link table' },
              ]}
            />
          )}

          {(target.kind === 'object_type' || d.keyMode === 'direct') && (
            <SelectField
              label={
                target.kind === 'object_type' ? 'Relation' : 'Relation holding the foreign key'
              }
              value={relationId(d.relation)}
              disabled={readOnly}
              onChange={(v) => pickRelation(v, 'relation')}
              options={relOptions}
            />
          )}

          {target.kind === 'object_type' && ot && table && (
            <div className="space-y-2">
              <h4 className="text-sm font-semibold">Columns to properties</h4>
              <table className="w-full text-sm">
                <caption className="sr-only">Property to column mapping</caption>
                <thead>
                  <tr>
                    <th scope="col" className="th">
                      Property
                    </th>
                    <th scope="col" className="th">
                      Column
                    </th>
                    <th scope="col" className="th">
                      Type check
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {ot.properties.map((p) => {
                    const col = d.columnMap[p.key] ?? '';
                    const colType = table.columns.find((c) => c.name === col)?.type;
                    const compat = col ? typeCompat(p.type, colType) : 'unknown';
                    return (
                      <tr key={p.key}>
                        <td className="td font-mono">
                          {p.key} <span className="text-xs text-slate-600">({p.type})</span>
                        </td>
                        <td className="td">
                          <select
                            aria-label={`Column for ${p.key}`}
                            className="input"
                            value={col}
                            disabled={readOnly}
                            onChange={(e) =>
                              set({ columnMap: { ...d.columnMap, [p.key]: e.target.value } })
                            }
                          >
                            {colOptions(table).map((o) => (
                              <option key={o.value} value={o.value}>
                                {o.label}
                              </option>
                            ))}
                          </select>
                        </td>
                        <td className="td">
                          {col && compat === 'compatible' && (
                            <span className="text-green-800">Compatible</span>
                          )}
                          {col && compat === 'incompatible' && (
                            <span role="alert" className="text-red-800">
                              Type mismatch: {p.type} property, {colType} column
                            </span>
                          )}
                          {col && compat === 'unknown' && (
                            <span className="text-slate-600">Cannot verify</span>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
              {ot.primary_key.some((k) => !d.columnMap[k]) && (
                <p className="text-sm text-amber-900">
                  Every primary key property needs a column before this object type can be
                  published.
                </p>
              )}
            </div>
          )}

          {target.kind === 'relationship' && d.keyMode === 'link' && (
            <>
              <SelectField
                label="Link table"
                value={relationId(d.linkRelation)}
                disabled={readOnly}
                onChange={(v) => pickRelation(v, 'linkRelation')}
                options={relOptions}
              />
              <div className="grid gap-3 sm:grid-cols-2">
                <SelectField
                  label={`Link column referencing ${rel?.from ?? 'from'}`}
                  value={d.fromColumn}
                  disabled={readOnly}
                  onChange={(v) => set({ fromColumn: v })}
                  options={colOptions(linkTable)}
                />
                <SelectField
                  label={`Link column referencing ${rel?.to ?? 'to'}`}
                  value={d.toColumn}
                  disabled={readOnly}
                  onChange={(v) => set({ toColumn: v })}
                  options={colOptions(linkTable)}
                />
              </div>
            </>
          )}
          {target.kind === 'relationship' && d.keyMode === 'direct' && table && (
            <div className="grid gap-3 sm:grid-cols-2">
              <SelectField
                label={`From column (${rel?.from ?? 'from'})`}
                value={d.fromColumn}
                disabled={readOnly}
                onChange={(v) => set({ fromColumn: v })}
                options={colOptions(table)}
              />
              <TextField
                label={`To column (${rel?.to ?? 'to'})`}
                value={d.toColumn}
                disabled={readOnly}
                onChange={(v) => set({ toColumn: v })}
                monospace
                help="Column in the target object type's relation that the foreign key points at."
              />
            </div>
          )}
        </>
      )}

      <fieldset className="space-y-2">
        <legend className="text-sm font-semibold">Static filters</legend>
        <p className="text-xs text-slate-600">
          Safe expressions applied to every query through this mapping, for example status !=
          &quot;deleted&quot;. They are compiled into parameters, never into SQL text.
        </p>
        {d.filters.map((f, i) => {
          const r = filterErrors[i];
          return (
            <div key={i} className="flex items-start gap-2">
              <div className="flex-1">
                <TextField
                  label={`Filter ${i + 1}`}
                  value={f}
                  disabled={readOnly}
                  monospace
                  error={r && !r.ok ? r.error : null}
                  onChange={(v) => set({ filters: d.filters.map((x, j) => (j === i ? v : x)) })}
                />
              </div>
              {!readOnly && (
                <button
                  type="button"
                  className="btn mt-6"
                  aria-label={`Remove filter ${i + 1}`}
                  onClick={() => set({ filters: d.filters.filter((_, j) => j !== i) })}
                >
                  Remove
                </button>
              )}
            </div>
          );
        })}
        {!readOnly && (
          <button
            type="button"
            className="btn"
            onClick={() => set({ filters: [...d.filters, ''] })}
          >
            Add filter
          </button>
        )}
      </fieldset>

      {mismatches.length > 0 && (
        <p role="alert" className="text-sm text-red-800">
          {mismatches.length} type mismatch(es) will fail validation at publish:{' '}
          {mismatches.join('; ')}.
        </p>
      )}

      <ActionError error={save.error} title="Could not save mapping" />
      <ActionError error={del.error} title="Could not delete mapping" />
      <ActionError error={test.error} title="Could not check mapping" />
      {saved && (
        <p role="status" className="text-sm text-green-800">
          Mapping saved to the draft.
        </p>
      )}
      <div className="flex flex-wrap gap-2">
        {!readOnly && (
          <button
            type="button"
            className="btn btn-primary"
            disabled={!canSave || save.busy}
            onClick={() => void doSave()}
          >
            {save.busy ? 'Saving...' : 'Save mapping'}
          </button>
        )}
        {!readOnly && existing && (
          <button type="button" className="btn" disabled={del.busy} onClick={() => void doDelete()}>
            Delete mapping
          </button>
        )}
        {!readOnly && (
          <button
            type="button"
            className="btn"
            disabled={!existing || test.busy}
            title={existing ? undefined : 'Save the mapping first'}
            onClick={() => void doCheck()}
          >
            {test.busy ? 'Checking...' : 'Check mapping'}
          </button>
        )}
      </div>

      {check && <CheckResult result={check} />}
    </div>
  );
}

function CheckResult({ result }: { result: MappingCheck }) {
  return (
    <section
      aria-label="Mapping check result"
      className="space-y-2 rounded-md border border-slate-300 p-3 text-sm"
    >
      <h4 className="font-semibold">
        {result.ok
          ? result.issues.length > 0
            ? `Mapping is valid with ${result.issues.length} warning(s)`
            : 'Mapping is valid'
          : `Mapping has ${result.issues.length} issue(s)`}
      </h4>
      {result.issues.length > 0 && (
        <ul className="list-disc space-y-1 pl-5 text-red-800">
          {result.issues.map((i, n) => (
            <li
              key={`${i.code ?? ''}-${n}`}
              className={i.severity === 'warning' ? 'text-amber-900' : ''}
            >
              <Badge tone={i.severity === 'warning' ? 'warn' : 'bad'}>
                {i.severity === 'warning' ? 'warning' : 'error'}
              </Badge>{' '}
              {i.message}
              {i.code && <span className="ml-1 font-mono text-xs">({i.code})</span>}
            </li>
          ))}
        </ul>
      )}
      {(result.sample_columns?.length ?? 0) > 0 && (
        <div>
          <p className="font-medium">Sample columns (metadata only, no rows)</p>
          <ul className="flex flex-wrap gap-2">
            {result.sample_columns?.map((c) => (
              <li key={c.name} className="rounded bg-slate-100 px-1 font-mono text-xs">
                {c.name}
                {c.type ? `: ${c.type}` : ''}
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}
