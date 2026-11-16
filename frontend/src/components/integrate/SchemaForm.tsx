'use client';

import { useId } from 'react';
import type { FieldDef, FormValues } from '@/lib/schema-form';
import { TextField } from '../admin/ui';

function FieldControl({
  field,
  value,
  error,
  onChange,
}: {
  field: FieldDef;
  value: string | boolean;
  error?: string;
  onChange: (v: string | boolean) => void;
}) {
  const id = useId();
  const label = field.required ? `${field.label} (required)` : field.label;
  if (field.kind === 'boolean') {
    return (
      <div>
        <label className="flex items-center gap-2 text-sm font-medium text-slate-800">
          <input
            type="checkbox"
            checked={value === true}
            onChange={(e) => onChange(e.target.checked)}
          />
          {label}
        </label>
        {field.description && <p className="mt-1 text-xs text-slate-600">{field.description}</p>}
      </div>
    );
  }
  if (field.kind === 'enum') {
    return (
      <div>
        <label htmlFor={id} className="label">
          {label}
        </label>
        <select
          id={id}
          className="input"
          value={String(value)}
          aria-invalid={error ? true : undefined}
          aria-describedby={error ? `${id}-err` : undefined}
          onChange={(e) => onChange(e.target.value)}
        >
          <option value="">{field.required ? 'Select...' : 'Not set'}</option>
          {field.options?.map((o) => (
            <option key={String(o)} value={String(o)}>
              {String(o)}
            </option>
          ))}
        </select>
        {field.description && <p className="mt-1 text-xs text-slate-600">{field.description}</p>}
        {error && (
          <p id={`${id}-err`} className="mt-1 text-sm text-red-700">
            {error}
          </p>
        )}
      </div>
    );
  }
  return (
    <TextField
      label={label}
      value={String(value)}
      onChange={onChange}
      type={field.kind === 'number' || field.kind === 'integer' ? 'number' : 'text'}
      multiline={field.kind === 'stringArray'}
      help={
        field.kind === 'stringArray'
          ? `${field.description ? `${field.description} ` : ''}One value per line.`
          : field.description
      }
      error={error}
      min={field.prop.minimum}
      max={field.prop.maximum}
      maxLength={field.prop.maxLength}
    />
  );
}

/** Renders the fields derived from a JSON Schema. Validation lives in `lib/schema-form`. */
export function SchemaForm({
  fields,
  values,
  errors,
  onChange,
  emptyText = 'This adapter has no configuration fields.',
}: {
  fields: FieldDef[];
  values: FormValues;
  errors: Record<string, string>;
  onChange: (key: string, value: string | boolean) => void;
  emptyText?: string;
}) {
  if (fields.length === 0) {
    return <p className="text-sm text-slate-600">{emptyText}</p>;
  }
  return (
    <div className="space-y-4">
      {fields.map((f) => (
        <FieldControl
          key={f.key}
          field={f}
          value={values[f.key] ?? ''}
          error={errors[f.key]}
          onChange={(v) => onChange(f.key, v)}
        />
      ))}
    </div>
  );
}
