'use client';

import { useMemo, useState } from 'react';
import type { NodeTypeDef, PipelineNode, PipelineSpec } from '@/lib/api/pipeline-types';
import {
  configLayout,
  connectionTypesFor,
  mergeConfig,
  pruneConfig,
  valuesFromConfig,
  VERSION_KEYS,
} from '@/lib/pipeline-config';
import {
  bindInput,
  bindingOptions,
  OPERATIONS_KEY,
  parseOperators,
  PORT_NAME,
  serializeOperators,
  type Operator,
} from '@/lib/pipeline-spec';
import { buildConfig, validateValues, type FormValues } from '@/lib/schema-form';
import { SelectField, TextField } from '../admin/ui';
import { ConnectionSelect, ModelSelect, VersionPicker } from '../build/pickers';
import { JsonField, num } from '../build/shared';
import { SchemaForm } from '../integrate/SchemaForm';
import { TransformEditor } from './TransformEditor';

/**
 * Side panel for one node: typed input bindings, resource pickers, the schema-driven config form,
 * the transform operator editor and JSON editors for config properties the form cannot render.
 */
export function NodeConfigPanel({
  org,
  node,
  def,
  spec,
  registry,
  readOnly,
  onChange,
  onRemove,
  reportInvalid,
}: {
  org: string;
  node: PipelineNode;
  def: NodeTypeDef | undefined;
  spec: PipelineSpec;
  registry: readonly NodeTypeDef[];
  readOnly: boolean;
  onChange: (n: PipelineNode) => void;
  onRemove: () => void;
  reportInvalid: (field: string, message: string | null) => void;
}) {
  const layout = useMemo(() => configLayout(node, def?.config_schema), [node, def]);
  const [values, setValues] = useState<FormValues>(() =>
    valuesFromConfig(layout.fields, node.config),
  );
  const errors = useMemo(() => {
    const all = validateValues(layout.fields, values);
    // Empty required fields are normal while drafting; the server reports them on validate.
    return Object.fromEntries(
      Object.entries(all).filter(([k]) => String(values[k] ?? '').trim() !== ''),
    );
  }, [layout.fields, values]);

  const setConfig = (config: Record<string, unknown>) =>
    onChange({ ...node, config: pruneConfig(node, config) });
  const setKey = (key: string, value: unknown) => {
    const config = { ...node.config };
    if (value === undefined || value === '') delete config[key];
    else config[key] = value;
    setConfig(config);
  };

  const options = bindingOptions(spec, registry, node.id);

  return (
    <section aria-label={`Configure node ${node.id}`} className="card space-y-4">
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-sm font-semibold">
          Node {node.id}{' '}
          <span className="font-normal text-slate-600">({def?.label ?? node.type})</span>
        </h3>
        {!readOnly && (
          <button type="button" className="btn btn-danger" onClick={onRemove}>
            Remove node {node.id}
          </button>
        )}
      </div>
      {def === undefined && (
        <p role="alert" className="text-sm text-red-700">
          Type &quot;{node.type}&quot; is not in the node-type registry, so its configuration cannot
          be edited here.
        </p>
      )}

      {def && def.inputs.length > 0 && (
        <fieldset className="space-y-2">
          <legend className="label">Inputs</legend>
          <p className="text-xs text-slate-600">
            Bind each input to an output of an upstream node (connect an edge first) or to a
            pipeline parameter.
          </p>
          {def.inputs.map((p) => {
            const current = node.inputs[p.name] ?? '';
            const known = options.some((o) => o.value === current);
            return (
              <SelectField
                key={p.name}
                label={`Input ${p.name}`}
                value={current}
                disabled={readOnly}
                onChange={(v) => onChange(bindInput(node, p.name, v))}
                options={[
                  { value: '', label: 'Not bound' },
                  ...(current && !known
                    ? [{ value: current, label: `${current} (not upstream)` }]
                    : []),
                  ...options,
                ]}
              />
            );
          })}
        </fieldset>
      )}
      {def?.dynamic_inputs && (
        <DynamicInputs
          node={node}
          declared={def.inputs.map((p) => p.name)}
          options={options}
          readOnly={readOnly}
          onChange={onChange}
        />
      )}
      {def && def.outputs.length > 0 && (
        <p className="text-xs text-slate-600">
          Outputs: {def.outputs.map((o) => `${node.id}.${o.name}`).join(', ')}
        </p>
      )}

      {layout.resources.map((key) => {
        if (key === 'connection_id')
          return (
            <ConnectionSelect
              key={key}
              org={org}
              label="Connection"
              types={connectionTypesFor(node.type)}
              value={String(node.config[key] ?? '')}
              disabled={readOnly}
              onChange={(v) => setKey(key, v)}
            />
          );
        if (key === 'model_id')
          return (
            <ModelSelect
              key={key}
              org={org}
              label="Model"
              value={String(node.config[key] ?? '')}
              disabled={readOnly}
              onChange={(v) => setKey(key, v)}
            />
          );
        const info = VERSION_KEYS[key];
        return (
          <VersionPicker
            key={key}
            org={org}
            kind={info.kind}
            label={info.label}
            value={String(node.config[key] ?? '')}
            disabled={readOnly}
            onChange={(v) => setKey(key, v)}
          />
        );
      })}

      {layout.fields.length > 0 && (
        <fieldset disabled={readOnly} className="space-y-2">
          <legend className="label">Configuration</legend>
          <SchemaForm
            fields={layout.fields}
            values={values}
            errors={errors}
            emptyText=""
            onChange={(k, v) => {
              const next = { ...values, [k]: v };
              setValues(next);
              setConfig(mergeConfig(node.config, layout.fields, buildConfig(layout.fields, next)));
            }}
          />
        </fieldset>
      )}

      {layout.hasOperators && (
        <TransformEditor
          operators={parseOperators(node.config[OPERATIONS_KEY])}
          readOnly={readOnly}
          onChange={(ops: Operator[]) =>
            setKey(OPERATIONS_KEY, ops.length ? serializeOperators(ops) : undefined)
          }
        />
      )}

      {layout.jsonKeys.map((key) => (
        <JsonField
          key={key}
          label={`${key} (JSON)`}
          value={node.config[key]}
          readOnly={readOnly}
          rows={4}
          onInvalid={(m) => reportInvalid(`config:${node.id}:${key}`, m)}
          onChange={(v) => setKey(key, v)}
        />
      ))}

      <TextField
        label="Max attempts (retry)"
        type="number"
        min={1}
        max={10}
        disabled={readOnly}
        value={node.retry?.max_attempts === undefined ? '' : String(node.retry.max_attempts)}
        help="Retries honour side-effect rules on the server."
        onChange={(v) => {
          const n = num(v);
          const { retry: _drop, ...rest } = node;
          void _drop;
          onChange(
            n === undefined ? (rest as PipelineNode) : { ...rest, retry: { max_attempts: n } },
          );
        }}
      />
    </section>
  );
}

/**
 * Types with `dynamic_inputs` (condition, api_request) name their own input ports: the expression reads
 * `inputs.<port>`, a request path/query/body uses `{port}` placeholders. One row per bound port.
 */
function DynamicInputs({
  node,
  declared,
  options,
  readOnly,
  onChange,
}: {
  node: PipelineNode;
  declared: string[];
  options: { value: string; label: string }[];
  readOnly: boolean;
  onChange: (n: PipelineNode) => void;
}) {
  const [name, setName] = useState('');
  const ports = Object.keys(node.inputs).filter((k) => !declared.includes(k));
  const nameError =
    name === ''
      ? undefined
      : !PORT_NAME.test(name)
        ? 'Use letters, digits and underscores, starting with a letter.'
        : name in node.inputs
          ? 'This input already exists.'
          : undefined;
  return (
    <fieldset className="space-y-2">
      <legend className="label">Named inputs</legend>
      <p className="text-xs text-slate-600">
        This node type names its own inputs. Add one per value you reference (an expression reads
        inputs.name, a request uses {'{name}'} placeholders) and bind it to an upstream output or a
        pipeline parameter.
      </p>
      {ports.length === 0 && <p className="text-sm text-slate-600">No named inputs.</p>}
      {!readOnly && options.length === 0 && (
        <p className="text-xs text-slate-600">
          Connect an upstream node (or declare a parameter) before adding an input.
        </p>
      )}
      {ports.map((port) => {
        const current = node.inputs[port] ?? '';
        const known = options.some((o) => o.value === current);
        return (
          <div key={port} className="flex flex-wrap items-end gap-2">
            <SelectField
              label={`Input ${port}`}
              value={current}
              disabled={readOnly}
              onChange={(v) => onChange(bindInput(node, port, v))}
              options={[
                ...(current && !known
                  ? [{ value: current, label: `${current} (not upstream)` }]
                  : []),
                ...options,
              ]}
            />
            {!readOnly && (
              <button
                type="button"
                className="btn"
                aria-label={`Remove input ${port}`}
                onClick={() => {
                  const { [port]: _drop, ...rest } = node.inputs;
                  void _drop;
                  onChange({ ...node, inputs: rest });
                }}
              >
                Remove
              </button>
            )}
          </div>
        );
      })}
      {!readOnly && (
        <div className="flex flex-wrap items-end gap-2">
          <TextField
            label="New input name"
            value={name}
            error={nameError}
            monospace
            onChange={setName}
          />
          <button
            type="button"
            className="btn"
            disabled={name === '' || nameError !== undefined || options.length === 0}
            onClick={() => {
              // A port without a binding is invalid, so it starts on the first available output.
              onChange({ ...node, inputs: { ...node.inputs, [name]: options[0]?.value ?? '' } });
              setName('');
            }}
          >
            Add input
          </button>
        </div>
      )}
    </fieldset>
  );
}
