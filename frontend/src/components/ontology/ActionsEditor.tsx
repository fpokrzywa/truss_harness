'use client';

import { useMemo, useState, type FormEvent } from 'react';
import type { OntAction, OntologySpec } from '@/lib/api/ontology-types';
import { useArtifacts, useVersion, useVersions } from '@/lib/build-hooks';
import { checkAction, OBJECT_REF, toolParams } from '@/lib/ontology-spec';
import { EmptyState, Modal, ModalActions, SelectField, TextField } from '../admin/ui';
import { Section } from '../build/shared';

interface Row {
  id: number;
  name: string;
  value: string;
}

/** Add, edit and remove `spec.actions[]` entries (contract P7 section 1). */
export function ActionsEditor({
  org,
  spec,
  readOnly,
  onUpsert,
  onRemove,
}: {
  org: string;
  spec: OntologySpec;
  readOnly: boolean;
  onUpsert: (a: OntAction) => void;
  onRemove: (key: string) => void;
}) {
  const [dialog, setDialog] = useState<{ initial?: OntAction } | null>(null);
  return (
    <Section
      title="Actions"
      help="Actions run a published tool version against one object. Parameters map to object properties ($object.property) or literal values. The server validates the tool and its side-effect rules on validate and publish."
    >
      {!readOnly && (
        <div>
          <button
            type="button"
            className="btn"
            disabled={spec.object_types.length === 0}
            onClick={() => setDialog({})}
          >
            Add action
          </button>
        </div>
      )}
      {spec.actions.length === 0 ? (
        <EmptyState>No actions declared.</EmptyState>
      ) : (
        <ul
          aria-label="Declared actions"
          className="divide-y divide-slate-100 rounded-md border border-slate-200"
        >
          {spec.actions.map((a) => (
            <li key={a.key} className="flex flex-wrap items-center gap-3 p-2 text-sm">
              <span className="font-mono">{a.key}</span>
              <span className="text-slate-600">on {a.object_type}</span>
              <span className="font-mono text-xs text-slate-600">tool {a.tool_version_id}</span>
              <span className="text-xs text-slate-600">
                {Object.entries(a.param_mapping)
                  .map(([k, v]) => `${k}=${v}`)
                  .join(', ') || 'no parameters'}
              </span>
              {!readOnly && (
                <>
                  <button
                    type="button"
                    className="btn"
                    aria-label={`Edit action ${a.key}`}
                    onClick={() => setDialog({ initial: a })}
                  >
                    Edit
                  </button>
                  <button
                    type="button"
                    className="btn"
                    aria-label={`Remove action ${a.key}`}
                    onClick={() => onRemove(a.key)}
                  >
                    Remove
                  </button>
                </>
              )}
            </li>
          ))}
        </ul>
      )}
      {dialog && (
        <ActionDialog
          org={org}
          spec={spec}
          initial={dialog.initial}
          onClose={() => setDialog(null)}
          onSave={(a) => {
            onUpsert(a);
            setDialog(null);
          }}
        />
      )}
    </Section>
  );
}

function ActionDialog({
  org,
  spec,
  initial,
  onSave,
  onClose,
}: {
  org: string;
  spec: OntologySpec;
  initial?: OntAction;
  onSave: (a: OntAction) => void;
  onClose: () => void;
}) {
  const editing = !!initial;
  const [key, setKey] = useState(initial?.key ?? '');
  const [objectType, setObjectType] = useState(initial?.object_type ?? '');
  const [toolId, setToolId] = useState('');
  const [versionId, setVersionId] = useState(initial?.tool_version_id ?? '');
  const [rows, setRows] = useState<Row[]>(
    Object.entries(initial?.param_mapping ?? {}).map(([name, value], i) => ({
      id: i,
      name,
      value,
    })),
  );
  const [nextId, setNextId] = useState(rows.length);

  const tools = useArtifacts(org, 'tools', { status: 'active' });
  const versions = useVersions(org, 'tools', toolId, !!toolId);
  const published = (versions.data ?? []).filter((v) => v.state === 'published');
  const toolVersion = useVersion(org, 'tools', toolId, toolId && versionId ? versionId : undefined);
  const params = useMemo(
    () => toolParams(toolVersion.data?.spec?.input_schema),
    [toolVersion.data],
  );
  const ot = spec.object_types.find((o) => o.key === objectType);

  const action: OntAction = {
    ...(initial ?? {}),
    key,
    object_type: objectType,
    tool_version_id: versionId,
    param_mapping: Object.fromEntries(rows.map((r) => [r.name.trim(), r.value])),
  };
  const duplicateNames = new Set(rows.map((r) => r.name.trim())).size !== rows.length;
  const check = checkAction(spec, action, { editing, params });
  const errors = duplicateNames
    ? [...check.errors, 'Parameter names must be unique.']
    : check.errors;

  function patch(id: number, change: Partial<Row>) {
    setRows((cur) => cur.map((r) => (r.id === id ? { ...r, ...change } : r)));
  }

  function addRow(name: string, value: string) {
    setRows((cur) => [...cur, { id: nextId, name, value }]);
    setNextId((n) => n + 1);
  }

  function submit(e: FormEvent) {
    e.preventDefault();
    if (errors.length === 0) onSave(action);
  }

  return (
    <Modal title={editing ? `Edit action ${initial?.key}` : 'Add action'} onClose={onClose} wide>
      <form onSubmit={submit} className="space-y-3" noValidate>
        <TextField
          label="Key"
          value={key}
          onChange={setKey}
          monospace
          disabled={editing}
          help="Immutable once created."
        />
        <SelectField
          label="Object type"
          value={objectType}
          onChange={setObjectType}
          options={[
            { value: '', label: 'Select...' },
            ...spec.object_types.map((o) => ({ value: o.key, label: `${o.label} (${o.key})` })),
          ]}
        />
        <div className="grid gap-2 sm:grid-cols-2">
          <SelectField
            label="Tool"
            value={toolId}
            onChange={(id) => {
              setToolId(id);
              setVersionId('');
            }}
            options={[
              {
                value: '',
                label: versionId && !toolId ? `Pinned version ${versionId}` : 'Select...',
              },
              ...(tools.data?.items ?? [])
                .filter((t) => t.latest_published_version)
                .map((t) => ({ value: t.id, label: `${t.name} (${t.key})` })),
            ]}
          />
          <SelectField
            label="Published tool version"
            value={versionId}
            disabled={!toolId}
            onChange={setVersionId}
            options={[
              { value: '', label: 'Select...' },
              ...published.map((v) => ({ value: v.id, label: `v${v.version_no}` })),
            ]}
          />
        </div>
        {params && (
          <p className="text-xs text-slate-600">
            Tool parameters: {params.names.join(', ') || 'none'}
            {params.required.length > 0 ? `; required: ${params.required.join(', ')}` : ''}.
          </p>
        )}
        <fieldset className="space-y-2">
          <legend className="text-sm font-semibold">Parameter mapping</legend>
          <p className="text-xs text-slate-600">
            Use {OBJECT_REF}&lt;property&gt; to take a value from the object, or type a literal.
          </p>
          {rows.map((r, i) => (
            <div key={r.id} className="flex flex-wrap items-end gap-2">
              <TextField
                label={`Parameter ${i + 1} name`}
                value={r.name}
                onChange={(name) => patch(r.id, { name })}
                monospace
              />
              <TextField
                label={`Parameter ${i + 1} value`}
                value={r.value}
                onChange={(value) => patch(r.id, { value })}
                monospace
              />
              <button
                type="button"
                className="btn"
                aria-label={`Remove parameter ${i + 1}`}
                onClick={() => setRows((cur) => cur.filter((x) => x.id !== r.id))}
              >
                Remove
              </button>
            </div>
          ))}
          <div className="flex flex-wrap gap-2">
            <button type="button" className="btn" onClick={() => addRow('', '')}>
              Add parameter
            </button>
            {ot?.properties.map((p) => (
              <button
                key={p.key}
                type="button"
                className="btn"
                aria-label={`Map ${p.key}`}
                onClick={() => addRow(p.key, `${OBJECT_REF}${p.key}`)}
              >
                Map {p.key}
              </button>
            ))}
          </div>
        </fieldset>
        {errors.length > 0 && (
          <ul
            role="alert"
            aria-label="Action problems"
            className="list-disc pl-5 text-sm text-red-700"
          >
            {errors.map((m) => (
              <li key={m}>{m}</li>
            ))}
          </ul>
        )}
        {check.warnings.length > 0 && (
          <ul aria-label="Action warnings" className="list-disc pl-5 text-sm text-amber-900">
            {check.warnings.map((m) => (
              <li key={m}>{m}</li>
            ))}
          </ul>
        )}
        <ModalActions
          onCancel={onClose}
          submitLabel="Save action"
          busy={false}
          submitDisabled={errors.length > 0}
        />
      </form>
    </Modal>
  );
}
