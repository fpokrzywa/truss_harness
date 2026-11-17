'use client';

import { useState, type FormEvent } from 'react';
import {
  CARDINALITIES,
  type Cardinality,
  type OntologySpec,
  type Relationship,
} from '@/lib/api/ontology-types';
import { cardinalityText, KEY_RE } from '@/lib/ontology-spec';
import { Modal, ModalActions, SelectField, TextField } from '../admin/ui';

/** Create or edit a typed relationship between two object types. */
export function RelationshipDialog({
  spec,
  initial,
  from: fromProp,
  to: toProp,
  onSave,
  onClose,
}: {
  spec: OntologySpec;
  initial?: Relationship;
  from?: string;
  to?: string;
  onSave: (r: Relationship) => void;
  onClose: () => void;
}) {
  const editing = !!initial;
  const [key, setKey] = useState(initial?.key ?? '');
  const [label, setLabel] = useState(initial?.label ?? '');
  const [from, setFrom] = useState(initial?.from ?? fromProp ?? spec.object_types[0]?.key ?? '');
  const [to, setTo] = useState(initial?.to ?? toProp ?? spec.object_types[0]?.key ?? '');
  const [cardinality, setCardinality] = useState<Cardinality>(
    initial?.cardinality ?? 'one_to_many',
  );
  const [required, setRequired] = useState(initial?.constraints.required ?? false);

  const taken =
    !editing &&
    (spec.relationships.some((r) => r.key === key) || spec.object_types.some((o) => o.key === key));
  const keyError =
    key && !KEY_RE.test(key)
      ? 'Use lowercase letters, digits, ".", "-" or "_".'
      : taken
        ? 'This key is already used.'
        : null;
  const invalid = !key || !!keyError || !label.trim() || !from || !to;
  const opts = spec.object_types.map((o) => ({ value: o.key, label: `${o.label} (${o.key})` }));

  function submit(e: FormEvent) {
    e.preventDefault();
    if (invalid) return;
    onSave({
      ...(initial ?? {}),
      key,
      label: label.trim(),
      from,
      to,
      cardinality,
      constraints: { ...(initial?.constraints ?? {}), required },
    });
  }

  return (
    <Modal
      title={editing ? `Edit relationship ${initial.key}` : 'Add relationship'}
      onClose={onClose}
    >
      <form onSubmit={submit} className="space-y-3" noValidate>
        <TextField
          label="Key"
          value={key}
          onChange={setKey}
          required
          monospace
          disabled={editing}
          help="Unique within the ontology, for example crm.customer_accounts."
          error={keyError}
        />
        <TextField label="Label" value={label} onChange={setLabel} required />
        <div className="grid gap-3 sm:grid-cols-2">
          <SelectField label="From" value={from} onChange={setFrom} options={opts} />
          <SelectField label="To" value={to} onChange={setTo} options={opts} />
        </div>
        <SelectField
          label="Cardinality"
          value={cardinality}
          onChange={(v) => setCardinality(v as Cardinality)}
          options={CARDINALITIES.map((c) => ({ value: c, label: cardinalityText(c) }))}
        />
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={required}
            onChange={(e) => setRequired(e.target.checked)}
          />
          Required (every source object must have a related object)
        </label>
        <ModalActions
          onCancel={onClose}
          submitLabel="Save relationship"
          busy={false}
          submitDisabled={invalid}
        />
      </form>
    </Modal>
  );
}
