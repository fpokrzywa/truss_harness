'use client';

import { useState, type FormEvent } from 'react';
import {
  CLASSIFICATIONS,
  PROPERTY_TYPES,
  type Classification,
  type ObjectType,
  type OntologySpec,
  type OntProperty,
  type PropertyType,
} from '@/lib/api/ontology-types';
import { KEY_RE, newProperty, PROP_KEY_RE } from '@/lib/ontology-spec';
import { Modal, ModalActions, SelectField, TextField } from '../admin/ui';

/** Create or edit an object type: identity, properties (type, nullability, classification), keys. */
export function ObjectTypeDialog({
  spec,
  initial,
  defaultNamespace,
  onSave,
  onClose,
}: {
  spec: OntologySpec;
  initial?: ObjectType;
  defaultNamespace?: string;
  onSave: (ot: ObjectType) => void;
  onClose: () => void;
}) {
  const editing = !!initial;
  const [key, setKey] = useState(initial?.key ?? '');
  const [label, setLabel] = useState(initial?.label ?? '');
  const [namespace, setNamespace] = useState(
    initial?.namespace ?? defaultNamespace ?? spec.namespaces[0]?.key ?? '',
  );
  const [props, setProps] = useState<OntProperty[]>(initial?.properties ?? []);
  const [pk, setPk] = useState<string[]>(initial?.primary_key ?? []);
  const [display, setDisplay] = useState(initial?.display_property ?? '');

  const keyTaken = !editing && spec.object_types.some((o) => o.key === key);
  const keyError =
    key && !KEY_RE.test(key)
      ? 'Use lowercase letters, digits, ".", "-" or "_".'
      : keyTaken
        ? 'An object type with this key already exists.'
        : null;
  const propKeys = props.map((p) => p.key);
  const propErrors = props.map((p, i) =>
    !PROP_KEY_RE.test(p.key)
      ? 'Letters, digits and "_"; cannot start with a digit.'
      : propKeys.indexOf(p.key) !== i
        ? 'Duplicate property key.'
        : null,
  );
  const invalid =
    !key || !!keyError || !label.trim() || !namespace || propErrors.some((e) => e !== null);

  function patch(i: number, change: Partial<OntProperty>) {
    const old = props[i];
    if (!old) return;
    if (change.key !== undefined && change.key !== old.key) {
      setPk((cur) => cur.map((k) => (k === old.key ? (change.key as string) : k)));
      setDisplay((cur) => (cur && cur === old.key ? (change.key as string) : cur));
    }
    setProps((cur) => cur.map((p, j) => (j === i ? { ...p, ...change } : p)));
  }

  function removeProp(i: number) {
    const gone = props[i]?.key;
    setProps((cur) => cur.filter((_, j) => j !== i));
    setPk((cur) => cur.filter((k) => k !== gone));
    setDisplay((cur) => (cur === gone ? '' : cur));
  }

  function submit(e: FormEvent) {
    e.preventDefault();
    if (invalid) return;
    onSave({
      ...(initial ?? {}),
      key,
      label: label.trim(),
      namespace,
      properties: props,
      primary_key: pk.filter((k) => propKeys.includes(k)),
      ...(display ? { display_property: display } : {}),
    });
  }

  return (
    <Modal
      title={editing ? `Edit object type ${initial.key}` : 'Add object type'}
      onClose={onClose}
      wide
    >
      <form onSubmit={submit} className="space-y-3" noValidate>
        <div className="grid gap-3 sm:grid-cols-2">
          <TextField
            label="Key"
            value={key}
            onChange={setKey}
            required
            monospace
            disabled={editing}
            help={
              editing
                ? 'The key cannot change after creation.'
                : 'Unique within the ontology, for example crm.customer.'
            }
            error={keyError}
          />
          <TextField label="Label" value={label} onChange={setLabel} required />
        </div>
        {spec.namespaces.length === 0 ? (
          <p role="alert" className="text-sm text-red-700">
            Add a namespace first; every object type belongs to one.
          </p>
        ) : (
          <SelectField
            label="Namespace"
            value={namespace}
            onChange={setNamespace}
            options={spec.namespaces.map((n) => ({ value: n.key, label: `${n.label} (${n.key})` }))}
          />
        )}

        <fieldset className="space-y-2">
          <legend className="text-sm font-semibold">Properties</legend>
          {props.length === 0 && <p className="text-sm text-slate-600">No properties yet.</p>}
          {props.map((p, i) => (
            <div
              key={i}
              className="flex flex-wrap items-start gap-2 rounded border border-slate-200 p-2"
            >
              <div>
                <input
                  aria-label={`Property ${i + 1} key`}
                  className="input font-mono"
                  value={p.key}
                  aria-invalid={propErrors[i] ? true : undefined}
                  onChange={(e) => patch(i, { key: e.target.value })}
                />
                {propErrors[i] && <p className="mt-1 text-xs text-red-700">{propErrors[i]}</p>}
              </div>
              <select
                aria-label={`Property ${i + 1} type`}
                className="input w-auto"
                value={p.type}
                onChange={(e) => patch(i, { type: e.target.value as PropertyType })}
              >
                {PROPERTY_TYPES.map((t) => (
                  <option key={t} value={t}>
                    {t}
                  </option>
                ))}
              </select>
              <select
                aria-label={`Property ${i + 1} classification`}
                className="input w-auto"
                value={p.classification}
                onChange={(e) => patch(i, { classification: e.target.value as Classification })}
              >
                {CLASSIFICATIONS.map((c) => (
                  <option key={c} value={c}>
                    {c}
                  </option>
                ))}
              </select>
              <label className="flex items-center gap-1 pt-2 text-sm">
                <input
                  type="checkbox"
                  checked={p.nullable}
                  onChange={(e) => patch(i, { nullable: e.target.checked })}
                />
                Property {i + 1} nullable
              </label>
              <button
                type="button"
                className="btn"
                aria-label={`Remove property ${i + 1}`}
                onClick={() => removeProp(i)}
              >
                Remove
              </button>
            </div>
          ))}
          <button
            type="button"
            className="btn"
            onClick={() =>
              setProps((cur) => [...cur, newProperty({ properties: cur } as ObjectType)])
            }
          >
            Add property
          </button>
        </fieldset>

        {props.length > 0 && (
          <fieldset>
            <legend className="text-sm font-semibold">Primary key</legend>
            <p className="text-xs text-slate-600">
              Primary key properties must exist and must not be nullable.
            </p>
            <div className="mt-1 flex flex-wrap gap-3">
              {props.map((p) => (
                <label key={p.key} className="flex items-center gap-1 text-sm">
                  <input
                    type="checkbox"
                    checked={pk.includes(p.key)}
                    onChange={(e) =>
                      setPk((cur) =>
                        e.target.checked ? [...cur, p.key] : cur.filter((k) => k !== p.key),
                      )
                    }
                  />
                  Primary key: {p.key}
                </label>
              ))}
            </div>
          </fieldset>
        )}
        {props.length > 0 && (
          <SelectField
            label="Display property"
            value={display}
            onChange={setDisplay}
            options={[
              { value: '', label: 'None' },
              ...props.map((p) => ({ value: p.key, label: p.key })),
            ]}
          />
        )}
        <ModalActions
          onCancel={onClose}
          submitLabel="Save object type"
          busy={false}
          submitDisabled={invalid}
        />
      </form>
    </Modal>
  );
}
