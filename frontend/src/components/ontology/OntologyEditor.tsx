'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import type { ObjectType, OntologySpec, Position, Relationship } from '@/lib/api/ontology-types';
import {
  addNamespace,
  cardinalityText,
  namespaceInUse,
  ontologyBlockers,
  ontologyIssues,
  removeNamespace,
  removeAction,
  removeObjectType,
  removeRelationship,
  setPosition,
  upsertAction,
  upsertObjectType,
  upsertRelationship,
  validationHighlights,
  KEY_RE,
} from '@/lib/ontology-spec';
import { SelectField, TextField } from '../admin/ui';
import { ActionsEditor } from './ActionsEditor';
import type { EditorProps } from '../build/DraftEditor';
import { Section } from '../build/shared';
import { LazyOntologyCanvas as OntologyCanvas } from './LazyOntologyCanvas';
import { MappingPanel } from './MappingPanel';
import type { Selection } from './OntologyCanvas';
import { ObjectTypeDialog } from './ObjectTypeDialog';
import { RelationshipDialog } from './RelationshipDialog';

export { normalizeOntologySpec } from '@/lib/ontology-spec';
export { ontologyBlockers };

type View = 'canvas' | 'list';
type Dialog =
  | { kind: 'object'; initial?: ObjectType }
  | { kind: 'relationship'; initial?: Relationship; from?: string; to?: string }
  | null;

export function OntologyEditor({
  org,
  artifact,
  spec,
  onChange,
  readOnly,
  validation,
  dirty,
  hasDraft,
}: EditorProps<OntologySpec>) {
  const [view, setView] = useState<View>('canvas');
  const [selected, setSelected] = useState<Selection>(null);
  const [dialog, setDialog] = useState<Dialog>(null);
  const [filter, setFilter] = useState('');
  const [nsKey, setNsKey] = useState('');
  const [nsLabel, setNsLabel] = useState('');

  // Canvas callbacks can fire several times per event; chain them on the latest spec.
  const latest = useRef(spec);
  useEffect(() => {
    latest.current = spec;
  }, [spec]);
  const update = (fn: (s: OntologySpec) => OntologySpec) => {
    latest.current = fn(latest.current);
    onChange(latest.current);
  };

  const issues = useMemo(() => ontologyIssues(spec), [spec]);
  const server = useMemo(
    () => validationHighlights(validation?.errors ?? [], spec),
    [validation, spec],
  );
  const invalidObjects = useMemo(() => {
    const s = new Set(server.objects);
    for (const i of issues) if (i.severity === 'error' && i.objectKey) s.add(i.objectKey);
    return s;
  }, [issues, server]);
  const invalidRels = useMemo(() => {
    const s = new Set(server.relationships);
    for (const i of issues)
      if (i.severity === 'error' && i.relationshipKey) s.add(i.relationshipKey);
    return s;
  }, [issues, server]);
  const advisory = issues.filter((i) => !i.blocking);

  const visible = useMemo<OntologySpec>(() => {
    if (!filter) return spec;
    const objects = spec.object_types.filter((o) => o.namespace === filter);
    const keys = new Set(objects.map((o) => o.key));
    return {
      ...spec,
      object_types: objects,
      relationships: spec.relationships.filter((r) => keys.has(r.from) && keys.has(r.to)),
    };
  }, [spec, filter]);

  const selObject =
    selected?.kind === 'object_type'
      ? spec.object_types.find((o) => o.key === selected.key)
      : undefined;
  const selRel =
    selected?.kind === 'relationship'
      ? spec.relationships.find((r) => r.key === selected.key)
      : undefined;

  function deleteObjects(keys: string[]) {
    update((s) => keys.reduce(removeObjectType, s));
    setSelected((cur) => (cur?.kind === 'object_type' && keys.includes(cur.key) ? null : cur));
  }
  function deleteRels(keys: string[]) {
    update((s) => keys.reduce(removeRelationship, s));
    setSelected((cur) => (cur?.kind === 'relationship' && keys.includes(cur.key) ? null : cur));
  }

  const nsError =
    nsKey && !KEY_RE.test(nsKey)
      ? 'Use lowercase letters, digits, ".", "-" or "_".'
      : spec.namespaces.some((n) => n.key === nsKey)
        ? 'This namespace already exists.'
        : null;

  return (
    <div className="space-y-4">
      <Section
        title="Namespaces"
        help="Namespaces group object types. Every object type belongs to one; filter the graph by namespace."
      >
        {spec.namespaces.length === 0 && (
          <p className="text-sm text-slate-600">No namespaces yet. Add one to start modelling.</p>
        )}
        <ul aria-label="Namespaces" className="flex flex-wrap gap-2">
          {spec.namespaces.map((n) => (
            <li
              key={n.key}
              className="flex items-center gap-1 rounded border border-slate-300 px-2 py-1 text-sm"
            >
              <span className="font-mono">{n.key}</span>
              <span className="text-slate-600">{n.label}</span>
              {!readOnly && (
                <button
                  type="button"
                  className="btn"
                  aria-label={`Remove namespace ${n.key}`}
                  disabled={namespaceInUse(spec, n.key)}
                  title={
                    namespaceInUse(spec, n.key) ? 'Object types use this namespace' : undefined
                  }
                  onClick={() => update((s) => removeNamespace(s, n.key))}
                >
                  Remove
                </button>
              )}
            </li>
          ))}
        </ul>
        {!readOnly && (
          <div className="flex flex-wrap items-start gap-2">
            <TextField
              label="Namespace key"
              value={nsKey}
              onChange={setNsKey}
              monospace
              error={nsError}
            />
            <TextField label="Namespace label" value={nsLabel} onChange={setNsLabel} />
            <button
              type="button"
              className="btn mt-6"
              disabled={!nsKey || !!nsError}
              onClick={() => {
                update((s) => addNamespace(s, nsKey, nsLabel.trim()));
                setNsKey('');
                setNsLabel('');
              }}
            >
              Add namespace
            </button>
          </div>
        )}
      </Section>

      <Section
        title="Ontology graph"
        help="Object types are nodes, relationships are labelled edges with cardinality. Drag from one node's handle to another to connect them, or use the list view. The server validates again on validate and publish."
      >
        <div className="flex flex-wrap items-end justify-between gap-2">
          <div className="flex flex-wrap items-end gap-2">
            {!readOnly && (
              <button
                type="button"
                className="btn btn-primary"
                onClick={() => setDialog({ kind: 'object' })}
              >
                Add object type
              </button>
            )}
            {!readOnly && (
              <button
                type="button"
                className="btn"
                disabled={spec.object_types.length === 0}
                onClick={() => setDialog({ kind: 'relationship' })}
              >
                Add relationship
              </button>
            )}
            <SelectField
              label="Namespace filter"
              value={filter}
              onChange={setFilter}
              options={[
                { value: '', label: 'All namespaces' },
                ...spec.namespaces.map((n) => ({ value: n.key, label: n.label })),
              ]}
            />
          </div>
          <div role="group" aria-label="Graph view" className="flex gap-1">
            <button
              type="button"
              className="btn"
              aria-pressed={view === 'canvas'}
              onClick={() => setView('canvas')}
            >
              Canvas view
            </button>
            <button
              type="button"
              className="btn"
              aria-pressed={view === 'list'}
              onClick={() => setView('list')}
            >
              List view
            </button>
          </div>
        </div>

        {view === 'canvas' ? (
          <OntologyCanvas
            spec={visible}
            selected={selected}
            invalidObjects={invalidObjects}
            invalidRelationships={invalidRels}
            readOnly={readOnly}
            onSelect={setSelected}
            onConnect={(from, to) => setDialog({ kind: 'relationship', from, to })}
            onDeleteObjects={deleteObjects}
            onDeleteRelationships={deleteRels}
            onMoveNode={(key, pos: Position) => update((s) => setPosition(s, key, pos))}
          />
        ) : (
          <GraphList
            spec={visible}
            selected={selected}
            readOnly={readOnly}
            invalidObjects={invalidObjects}
            invalidRelationships={invalidRels}
            onSelect={setSelected}
            onEditObject={(o) => setDialog({ kind: 'object', initial: o })}
            onEditRelationship={(r) => setDialog({ kind: 'relationship', initial: r })}
            onRemoveObject={(k) => deleteObjects([k])}
            onRemoveRelationship={(k) => deleteRels([k])}
          />
        )}
        {spec.object_types.length === 0 && (
          <p className="text-sm text-slate-600">No object types yet. Add one above.</p>
        )}
      </Section>

      {selObject && (
        <SelectedObject
          ot={selObject}
          readOnly={readOnly}
          onEdit={() => setDialog({ kind: 'object', initial: selObject })}
          onRemove={() => deleteObjects([selObject.key])}
        />
      )}
      {selRel && (
        <SelectedRelationship
          rel={selRel}
          readOnly={readOnly}
          onEdit={() => setDialog({ kind: 'relationship', initial: selRel })}
          onRemove={() => deleteRels([selRel.key])}
        />
      )}
      {selected && (selObject || selRel) && (
        <MappingPanel
          org={org}
          ontologyId={artifact.id}
          spec={spec}
          target={selected}
          readOnly={readOnly}
          needsSave={!!dirty || !hasDraft}
        />
      )}

      <ActionsEditor
        org={org}
        spec={spec}
        readOnly={readOnly}
        onUpsert={(a) => update((s) => upsertAction(s, a))}
        onRemove={(k) => update((s) => removeAction(s, k))}
      />

      {advisory.length > 0 && (
        <section aria-label="Advisory checks" className="card space-y-1 text-sm">
          <h3 className="text-sm font-semibold">Checks</h3>
          <ul className="list-disc pl-5">
            {advisory.map((i, n) => (
              <li key={`${i.code}-${n}`} className="text-amber-900">
                {i.message} <span className="font-mono text-xs">({i.code})</span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {dialog?.kind === 'object' && (
        <ObjectTypeDialog
          spec={spec}
          initial={dialog.initial}
          defaultNamespace={filter || undefined}
          onClose={() => setDialog(null)}
          onSave={(ot) => {
            update((s) => upsertObjectType(s, ot));
            setSelected({ kind: 'object_type', key: ot.key });
            setDialog(null);
          }}
        />
      )}
      {dialog?.kind === 'relationship' && (
        <RelationshipDialog
          spec={spec}
          initial={dialog.initial}
          from={dialog.from}
          to={dialog.to}
          onClose={() => setDialog(null)}
          onSave={(r) => {
            update((s) => upsertRelationship(s, r));
            setSelected({ kind: 'relationship', key: r.key });
            setDialog(null);
          }}
        />
      )}
    </div>
  );
}

function SelectedObject({
  ot,
  readOnly,
  onEdit,
  onRemove,
}: {
  ot: ObjectType;
  readOnly: boolean;
  onEdit: () => void;
  onRemove: () => void;
}) {
  return (
    <section aria-label={`Object type ${ot.key}`} className="card space-y-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-sm font-semibold">
          {ot.label} <span className="font-mono text-xs text-slate-600">{ot.key}</span>
        </h3>
        {!readOnly && (
          <div className="flex gap-2">
            <button type="button" className="btn" onClick={onEdit}>
              Edit object type
            </button>
            <button type="button" className="btn btn-danger" onClick={onRemove}>
              Remove object type
            </button>
          </div>
        )}
      </div>
      {ot.properties.length === 0 ? (
        <p className="text-sm text-slate-600">No properties.</p>
      ) : (
        <table className="w-full text-sm">
          <caption className="sr-only">Properties of {ot.key}</caption>
          <thead>
            <tr>
              {['Property', 'Type', 'Nullable', 'Classification', 'Key'].map((h) => (
                <th key={h} scope="col" className="th">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {ot.properties.map((p) => (
              <tr key={p.key}>
                <td className="td font-mono">{p.key}</td>
                <td className="td">{p.type}</td>
                <td className="td">{p.nullable ? 'yes' : 'no'}</td>
                <td className="td">{p.classification}</td>
                <td className="td">
                  {ot.primary_key.includes(p.key) ? 'Primary key' : ''}
                  {ot.display_property === p.key ? ' Display' : ''}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

function SelectedRelationship({
  rel,
  readOnly,
  onEdit,
  onRemove,
}: {
  rel: Relationship;
  readOnly: boolean;
  onEdit: () => void;
  onRemove: () => void;
}) {
  return (
    <section aria-label={`Relationship ${rel.key}`} className="card space-y-1 text-sm">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="font-semibold">
          {rel.label} <span className="font-mono text-xs text-slate-600">{rel.key}</span>
        </h3>
        {!readOnly && (
          <div className="flex gap-2">
            <button type="button" className="btn" onClick={onEdit}>
              Edit relationship
            </button>
            <button type="button" className="btn btn-danger" onClick={onRemove}>
              Remove relationship
            </button>
          </div>
        )}
      </div>
      <p>
        {rel.from} to {rel.to}: {cardinalityText(rel.cardinality)}
        {rel.constraints.required ? ', required' : ''}
      </p>
    </section>
  );
}

/** Keyboard-accessible alternative to the canvas. */
function GraphList({
  spec,
  selected,
  readOnly,
  invalidObjects,
  invalidRelationships,
  onSelect,
  onEditObject,
  onEditRelationship,
  onRemoveObject,
  onRemoveRelationship,
}: {
  spec: OntologySpec;
  selected: Selection;
  readOnly: boolean;
  invalidObjects: ReadonlySet<string>;
  invalidRelationships: ReadonlySet<string>;
  onSelect: (s: Selection) => void;
  onEditObject: (o: ObjectType) => void;
  onEditRelationship: (r: Relationship) => void;
  onRemoveObject: (key: string) => void;
  onRemoveRelationship: (key: string) => void;
}) {
  return (
    <div className="space-y-3">
      {spec.object_types.length > 0 && (
        <ul
          aria-label="Object types"
          className="divide-y divide-slate-100 rounded-md border border-slate-200"
        >
          {spec.object_types.map((o) => (
            <li key={o.key} className="flex flex-wrap items-center gap-2 p-2 text-sm">
              <button
                type="button"
                className="btn"
                aria-pressed={selected?.kind === 'object_type' && selected.key === o.key}
                aria-label={`Select object type ${o.key}`}
                onClick={() => onSelect({ kind: 'object_type', key: o.key })}
              >
                {o.label}
              </button>
              <span className="font-mono text-xs text-slate-600">{o.key}</span>
              <span className="text-slate-600">
                {o.namespace}: {o.properties.length} propert
                {o.properties.length === 1 ? 'y' : 'ies'}
              </span>
              {invalidObjects.has(o.key) && <span className="text-red-700">Has problems</span>}
              {!readOnly && (
                <>
                  <button
                    type="button"
                    className="btn"
                    aria-label={`Edit object type ${o.key}`}
                    onClick={() => onEditObject(o)}
                  >
                    Edit
                  </button>
                  <button
                    type="button"
                    className="btn"
                    aria-label={`Remove object type ${o.key}`}
                    onClick={() => onRemoveObject(o.key)}
                  >
                    Remove
                  </button>
                </>
              )}
            </li>
          ))}
        </ul>
      )}
      {spec.relationships.length > 0 && (
        <ul
          aria-label="Relationships"
          className="divide-y divide-slate-100 rounded-md border border-slate-200"
        >
          {spec.relationships.map((r) => (
            <li key={r.key} className="flex flex-wrap items-center gap-2 p-2 text-sm">
              <button
                type="button"
                className="btn"
                aria-pressed={selected?.kind === 'relationship' && selected.key === r.key}
                aria-label={`Select relationship ${r.key}`}
                onClick={() => onSelect({ kind: 'relationship', key: r.key })}
              >
                {r.label}
              </button>
              <span className="font-mono text-xs text-slate-600">{r.key}</span>
              <span className="text-slate-600">
                {r.from} to {r.to}, {cardinalityText(r.cardinality)}
              </span>
              {invalidRelationships.has(r.key) && (
                <span className="text-red-700">Has problems</span>
              )}
              {!readOnly && (
                <>
                  <button
                    type="button"
                    className="btn"
                    aria-label={`Edit relationship ${r.key}`}
                    onClick={() => onEditRelationship(r)}
                  >
                    Edit
                  </button>
                  <button
                    type="button"
                    className="btn"
                    aria-label={`Remove relationship ${r.key}`}
                    onClick={() => onRemoveRelationship(r.key)}
                  >
                    Remove
                  </button>
                </>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
