'use client';

import Link from 'next/link';
import { useMemo, useState } from 'react';
import { endpoints } from '@/lib/api/endpoints';
import { ApiError } from '@/lib/api/errors';
import type { Artifact } from '@/lib/api/build-types';
import type {
  ObjectType,
  OntologyQuery,
  OntologySpec,
  OntRow,
  TraverseHop,
} from '@/lib/api/ontology-types';
import { useVersion, useVersions } from '@/lib/build-hooks';
import { useObjectView } from '@/lib/ontology-hooks';
import {
  checkFilter,
  formatCell,
  hopOptions,
  MAX_LIMIT,
  MAX_TRAVERSE_DEPTH,
  normalizeOntologySpec,
  pkOfRow,
  relatedRows,
  typeAtHop,
} from '@/lib/ontology-spec';
import { buildHref } from '@/lib/scope';
import { ActionError, Badge, SelectField, TextField, useAction } from '../admin/ui';
import { QueryError } from '../QueryError';
import { Section } from '../build/shared';

interface Hop {
  relationship: string;
  select: string[];
  filter: string;
}

interface Opened {
  type: string;
  pk: string;
  row: OntRow;
}

/** Explorer: pick a published version and object type, build a query, browse results and objects. */
export function ExplorerPanel({ org, artifact }: { org: string; artifact: Artifact }) {
  const versions = useVersions(org, 'ontologies', artifact.id);
  const published = (versions.data ?? []).filter((v) => v.state === 'published');
  const [picked, setPicked] = useState('');
  const versionId = picked || published[0]?.id || artifact.latest_published_version?.id || '';
  const version = useVersion(org, 'ontologies', artifact.id, versionId || undefined);
  const spec = useMemo(() => normalizeOntologySpec(version.data?.spec), [version.data]);

  return (
    <div className="space-y-4">
      <p className="rounded-md border border-slate-200 bg-slate-50 p-3 text-sm text-slate-700">
        Queries read live from the mapped source connections under your own access policies.
        Materialization is not enabled: no data is copied into this platform.
      </p>
      {versions.isError && <QueryError error={versions.error} />}
      {versions.isSuccess && published.length === 0 ? (
        <p className="text-sm text-slate-600">
          Publish a version first; the Explorer queries published versions only.
        </p>
      ) : (
        <SelectField
          label="Published version"
          value={versionId}
          onChange={setPicked}
          options={published.map((v) => ({ value: v.id, label: `v${v.version_no}` }))}
        />
      )}
      {version.isError && <QueryError error={version.error} />}
      {version.isSuccess && (
        // Reset the whole builder when the version changes.
        <Builder key={versionId} org={org} artifact={artifact} spec={spec} versionId={versionId} />
      )}
    </div>
  );
}

function Builder({
  org,
  artifact,
  spec,
  versionId,
}: {
  org: string;
  artifact: Artifact;
  spec: OntologySpec;
  versionId: string;
}) {
  const [objectType, setObjectType] = useState('');
  const [select, setSelect] = useState<string[]>([]);
  const [filter, setFilter] = useState('');
  const [limit, setLimit] = useState('50');
  const [hops, setHops] = useState<Hop[]>([]);
  const [rows, setRows] = useState<OntRow[] | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const [lineageId, setLineageId] = useState<string | null>(null);
  const [truncated, setTruncated] = useState(false);
  const [stack, setStack] = useState<Opened[]>([]);
  const run = useAction();

  const ot = spec.object_types.find((o) => o.key === objectType);
  const limitN = Math.min(MAX_LIMIT, Math.max(1, Number(limit) || 50));
  const filterCheck = filter.trim() ? checkFilter(filter) : null;

  function pickType(key: string) {
    const t = spec.object_types.find((o) => o.key === key);
    setObjectType(key);
    setSelect(t?.primary_key ?? []);
    setHops([]);
    setRows(null);
    setStack([]);
  }

  function body(cur?: string): OntologyQuery {
    const traverse: TraverseHop[] = [];
    let at = objectType;
    for (const h of hops) {
      if (!h.relationship) continue;
      const opt = hopOptions(spec, at).find((o) => o.relationship.key === h.relationship);
      at = opt?.target ?? at;
      traverse.push({
        relationship: h.relationship,
        direction: opt?.reverse ? 'reverse' : 'forward',
        select: h.select,
        ...(h.filter.trim() ? { filter: h.filter.trim() } : {}),
      });
    }
    return {
      version_id: versionId,
      object_type: objectType,
      select,
      ...(filter.trim() ? { filter: filter.trim() } : {}),
      ...(traverse.length > 0 ? { traverse } : {}),
      limit: limitN,
      ...(cur ? { cursor: cur } : {}),
    };
  }

  async function execute(more: boolean) {
    const r = await run.run(() =>
      endpoints.queryOntology(org, artifact.id, body(more ? (cursor ?? undefined) : undefined)),
    );
    if (r.ok) {
      setRows((cur) => (more ? [...(cur ?? []), ...r.value.rows] : r.value.rows));
      setCursor(r.value.next_cursor ?? null);
      setLineageId(r.value.lineage_id);
      setTruncated((t) => r.value.truncated === true || (more && t));
      if (!more) setStack([]);
    }
  }

  const hopValid = hops.every(
    (h) =>
      !h.relationship || (h.select.length > 0 && (!h.filter.trim() || checkFilter(h.filter).ok)),
  );
  const canRun =
    !!ot && select.length > 0 && (filterCheck === null || filterCheck.ok) && hopValid && !run.busy;

  const columns = useMemo(() => {
    const set = new Set<string>();
    for (const r of rows ?? []) for (const k of Object.keys(r)) if (k !== '_links') set.add(k);
    return [...set];
  }, [rows]);

  const opened = stack[stack.length - 1];
  const denied = run.error instanceof ApiError && run.error.code === 'property_denied';

  return (
    <div className="space-y-4">
      <Section
        title="Query"
        help="Select properties, optionally filter and traverse up to three relationships."
      >
        <SelectField
          label="Object type"
          value={objectType}
          onChange={pickType}
          options={[
            { value: '', label: 'Select...' },
            ...spec.object_types.map((o) => ({ value: o.key, label: `${o.label} (${o.key})` })),
          ]}
        />
        {ot && (
          <>
            <PropChecks label="Select properties" type={ot} value={select} onChange={setSelect} />
            <TextField
              label="Filter"
              value={filter}
              onChange={setFilter}
              monospace
              help='Safe expression over property keys, for example status == "open". Not applied until you run the query.'
              error={filterCheck && !filterCheck.ok ? filterCheck.error : null}
            />
            <TextField
              label="Row limit"
              type="number"
              min={1}
              max={MAX_LIMIT}
              value={limit}
              onChange={setLimit}
              help={`1 to ${MAX_LIMIT}.`}
            />

            <fieldset className="space-y-2">
              <legend className="text-sm font-semibold">Traverse relationships</legend>
              {hops.map((h, i) => {
                const from = typeAtHop(spec, objectType, hops.slice(0, i));
                const opts = from ? hopOptions(spec, from) : [];
                const target = typeAtHop(spec, objectType, hops.slice(0, i + 1));
                const targetType = spec.object_types.find((o) => o.key === target);
                return (
                  <div key={i} className="space-y-2 rounded border border-slate-200 p-2">
                    <SelectField
                      label={`Hop ${i + 1} relationship`}
                      value={h.relationship}
                      onChange={(v) => {
                        const next = typeAtHop(spec, objectType, [
                          ...hops.slice(0, i),
                          { relationship: v },
                        ]);
                        const t = spec.object_types.find((o) => o.key === next);
                        setHops((cur) => [
                          ...cur.slice(0, i),
                          { relationship: v, select: t?.primary_key ?? [], filter: '' },
                        ]);
                      }}
                      options={[
                        { value: '', label: 'Select...' },
                        ...opts.map((o) => ({
                          value: o.relationship.key,
                          label: `${o.relationship.label}${o.reverse ? ' (reverse)' : ''} to ${o.target}`,
                        })),
                      ]}
                    />
                    {targetType && (
                      <>
                        <PropChecks
                          label={`Hop ${i + 1} properties`}
                          type={targetType}
                          value={h.select}
                          onChange={(s) =>
                            setHops((cur) => cur.map((x, j) => (j === i ? { ...x, select: s } : x)))
                          }
                        />
                        <TextField
                          label={`Hop ${i + 1} filter`}
                          value={h.filter}
                          monospace
                          error={
                            h.filter.trim() && !checkFilter(h.filter).ok
                              ? (checkFilter(h.filter).error ?? null)
                              : null
                          }
                          onChange={(f) =>
                            setHops((cur) => cur.map((x, j) => (j === i ? { ...x, filter: f } : x)))
                          }
                        />
                      </>
                    )}
                  </div>
                );
              })}
              <div className="flex gap-2">
                <button
                  type="button"
                  className="btn"
                  disabled={
                    hops.length >= MAX_TRAVERSE_DEPTH ||
                    (hops.length > 0 && !hops[hops.length - 1]?.relationship)
                  }
                  onClick={() =>
                    setHops((cur) => [...cur, { relationship: '', select: [], filter: '' }])
                  }
                >
                  Add hop
                </button>
                {hops.length > 0 && (
                  <button
                    type="button"
                    className="btn"
                    onClick={() => setHops((cur) => cur.slice(0, -1))}
                  >
                    Remove last hop
                  </button>
                )}
                <span className="self-center text-xs text-slate-600">
                  {hops.length} of {MAX_TRAVERSE_DEPTH} hops
                </span>
              </div>
            </fieldset>

            <button
              type="button"
              className="btn btn-primary"
              disabled={!canRun}
              onClick={() => void execute(false)}
            >
              {run.busy && !cursor ? 'Running...' : 'Run query'}
            </button>
          </>
        )}
        {denied ? (
          <div
            role="alert"
            className="rounded-md border border-red-300 bg-red-50 p-3 text-sm text-red-900"
          >
            <p className="font-semibold">Property access denied</p>
            <p className="mt-1">
              {(run.error as ApiError).detail ??
                'One of the selected properties is not permitted for your account.'}{' '}
              Denied properties are never dropped silently; remove them from the selection or ask a
              policy administrator for access.
            </p>
            <p className="mt-1 font-mono text-xs">property_denied</p>
          </div>
        ) : (
          <ActionError error={run.error} title="Query failed" />
        )}
      </Section>

      {rows && (
        <Section title="Results">
          <div className="flex flex-wrap items-center gap-3 text-sm">
            <span role="status">
              {rows.length} row{rows.length === 1 ? '' : 's'} loaded
            </span>
            {lineageId && (
              <Link
                className="text-blue-800 underline"
                href={`${buildHref(org, 'ontologies')}/${encodeURIComponent(artifact.id)}/lineage?lineage_id=${encodeURIComponent(lineageId)}`}
              >
                Lineage {lineageId}
              </Link>
            )}
          </div>
          {truncated && (
            <p role="status" className="text-sm text-amber-900">
              Some related rows were truncated by a server cap (100 related rows per parent, 10,000
              rows per query). Narrow the traversal filters to see the rest.
            </p>
          )}
          {rows.length === 0 ? (
            <p className="text-sm text-slate-600">No rows matched.</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <caption className="sr-only">Query results</caption>
                <thead>
                  <tr>
                    <th scope="col" className="th">
                      <span className="sr-only">Open</span>
                    </th>
                    {columns.map((c) => (
                      <th key={c} scope="col" className="th">
                        {c}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {rows.map((r, i) => {
                    const pk = ot ? pkOfRow(ot, r) : null;
                    return (
                      <tr key={i}>
                        <td className="td">
                          {pk ? (
                            <button
                              type="button"
                              className="btn"
                              aria-label={`Open ${objectType} ${pk}`}
                              onClick={() => setStack([{ type: objectType, pk, row: r }])}
                            >
                              Open
                            </button>
                          ) : null}
                        </td>
                        {columns.map((c) => (
                          <td key={c} className="td font-mono text-xs">
                            {formatCell(r[c])}
                          </td>
                        ))}
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
          {rows.length > 0 && ot && !rows.some((r) => pkOfRow(ot, r)) && (
            <p className="text-xs text-slate-600">
              Select all primary key properties to open objects from the results.
            </p>
          )}
          {cursor && (
            <button
              type="button"
              className="btn"
              disabled={run.busy}
              onClick={() => void execute(true)}
            >
              {run.busy ? 'Loading...' : 'Load more'}
            </button>
          )}
        </Section>
      )}

      {opened && (
        <ObjectView
          org={org}
          artifact={artifact}
          spec={spec}
          versionId={versionId}
          opened={opened}
          canGoBack={stack.length > 1}
          onBack={() => setStack((s) => s.slice(0, -1))}
          onClose={() => setStack([])}
          onOpen={(o) => setStack((s) => [...s, o])}
        />
      )}
    </div>
  );
}

function PropChecks({
  label,
  type,
  value,
  onChange,
}: {
  label: string;
  type: ObjectType;
  value: string[];
  onChange: (v: string[]) => void;
}) {
  return (
    <fieldset>
      <legend className="text-sm font-medium">{label}</legend>
      <div className="mt-1 flex flex-wrap gap-3">
        {type.properties.map((p) => (
          <label key={p.key} className="flex items-center gap-1 text-sm">
            <input
              type="checkbox"
              checked={value.includes(p.key)}
              onChange={(e) =>
                onChange(e.target.checked ? [...value, p.key] : value.filter((k) => k !== p.key))
              }
            />
            {p.key}
          </label>
        ))}
      </div>
    </fieldset>
  );
}

const literal = (v: unknown): string =>
  typeof v === 'number' || typeof v === 'boolean' ? String(v) : JSON.stringify(String(v));

/** `pk1 == v1 and pk2 == v2` for the opened row. */
function pkFilter(ot: ObjectType, row: OntRow): string {
  return ot.primary_key.map((k) => `${k} == ${literal(row[k])}`).join(' and ');
}

function ObjectView({
  org,
  artifact,
  spec,
  versionId,
  opened,
  canGoBack,
  onBack,
  onClose,
  onOpen,
}: {
  org: string;
  artifact: Artifact;
  spec: OntologySpec;
  versionId: string;
  opened: Opened;
  canGoBack: boolean;
  onBack: () => void;
  onClose: () => void;
  onOpen: (o: Opened) => void;
}) {
  const view = useObjectView(org, artifact.id, opened.type, opened.pk, versionId);
  const ot = spec.object_types.find((o) => o.key === opened.type);
  const [related, setRelated] = useState<{
    rel: string;
    target: string;
    rows: OntRow[];
    truncated: boolean;
  } | null>(null);
  const nav = useAction();

  async function showRelated(rel: string, direction?: 'forward' | 'reverse') {
    if (!ot) return;
    const opt = hopOptions(spec, ot.key).find(
      (o) =>
        o.relationship.key === rel &&
        (direction === undefined || o.reverse === (direction === 'reverse')),
    );
    const targetType = spec.object_types.find((o) => o.key === opt?.target);
    if (!opt || !targetType) return;
    const r = await nav.run(() =>
      endpoints.queryOntology(org, artifact.id, {
        version_id: versionId,
        object_type: ot.key,
        select: ot.primary_key,
        filter: pkFilter(ot, opened.row),
        traverse: [
          {
            relationship: rel,
            direction: opt.reverse ? 'reverse' : 'forward',
            select: targetType.primary_key,
          },
        ],
        limit: 1,
      }),
    );
    if (r.ok) {
      const first = r.value.rows[0];
      setRelated({
        rel,
        target: targetType.key,
        rows: first ? relatedRows(first, rel) : [],
        truncated: r.value.truncated === true,
      });
    }
  }

  return (
    <Section title={`Object ${opened.type} ${opened.pk}`}>
      <div className="flex gap-2">
        {canGoBack && (
          <button type="button" className="btn" onClick={onBack}>
            Back
          </button>
        )}
        <button type="button" className="btn" onClick={onClose}>
          Close object
        </button>
      </div>
      {view.isPending && (
        <p role="status" className="text-sm text-slate-600">
          Loading object...
        </p>
      )}
      {view.isError && <QueryError error={view.error} />}
      {view.data && (
        <>
          <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-sm">
            {Object.entries(view.data.properties).map(([k, v]) => (
              <div key={k} className="contents">
                <dt className="font-mono text-slate-600">{k}</dt>
                <dd className="font-mono">{formatCell(v)}</dd>
              </div>
            ))}
          </dl>
          <h4 className="text-sm font-semibold">Relationships</h4>
          {view.data.relationships.length === 0 ? (
            <p className="text-sm text-slate-600">No relationships.</p>
          ) : (
            <ul aria-label="Relationship summaries" className="space-y-1 text-sm">
              {view.data.relationships.map((s) => (
                <li
                  key={`${s.relationship}:${s.direction ?? ''}`}
                  className="flex items-center gap-2"
                >
                  <span className="font-mono">{s.relationship}</span>
                  {s.label && s.label !== s.relationship && (
                    <span className="text-slate-700">{s.label}</span>
                  )}
                  {s.direction && <Badge tone="neutral">{s.direction}</Badge>}
                  <span>
                    {s.count}
                    {s.capped ? '+' : ''} related
                  </span>
                  {s.capped && <Badge tone="warn">capped</Badge>}
                  <button
                    type="button"
                    className="btn"
                    aria-label={`View related via ${s.relationship}`}
                    disabled={nav.busy || s.count === 0}
                    onClick={() => void showRelated(s.relationship, s.direction)}
                  >
                    View related
                  </button>
                </li>
              ))}
            </ul>
          )}
          <ActionError error={nav.error} title="Could not load related objects" />
          {related && (
            <div
              aria-label={`Related via ${related.rel}`}
              role="group"
              className="space-y-1 text-sm"
            >
              <p className="font-medium">
                Related {related.target} objects via {related.rel}
              </p>
              {related.truncated && (
                <p className="text-amber-900">
                  The list was truncated by a server cap; not all related objects are shown.
                </p>
              )}
              {related.rows.length === 0 ? (
                <p className="text-slate-600">No related objects were returned.</p>
              ) : (
                <ul className="space-y-1">
                  {related.rows.map((r, i) => {
                    const targetType = spec.object_types.find((o) => o.key === related.target);
                    const pk = targetType ? pkOfRow(targetType, r) : null;
                    return (
                      <li key={i} className="flex items-center gap-2">
                        <span className="font-mono text-xs">{formatCell(r)}</span>
                        {pk && (
                          <button
                            type="button"
                            className="btn"
                            aria-label={`Open ${related.target} ${pk}`}
                            onClick={() => onOpen({ type: related.target, pk, row: r })}
                          >
                            Open
                          </button>
                        )}
                      </li>
                    );
                  })}
                </ul>
              )}
            </div>
          )}
        </>
      )}
    </Section>
  );
}
