'use client';

import { useState } from 'react';
import { ApiError } from '@/lib/api/errors';
import { endpoints } from '@/lib/api/endpoints';
import type {
  AccessMode,
  Adapter,
  ConnectionInput,
  ConnectionType,
} from '@/lib/api/integrate-types';
import { useCan } from '@/lib/hooks';
import { useAdapters, useEgressPolicies, useRefreshInt, useVendors } from '@/lib/integrate-hooks';
import { isPlannedAdapter } from '@/lib/maturity';
import {
  buildConfig,
  fieldsFromSchema,
  initialValues,
  mapServerErrors,
  validateValues,
  type FormValues,
} from '@/lib/schema-form';
import { useSecrets } from '@/lib/security-hooks';
import { ErrorMessage } from '../ErrorMessage';
import { Modal, SelectField, TextField, useAction } from '../admin/ui';
import { SchemaForm } from './SchemaForm';
import { SlugScopeSelect } from './SlugScopeSelect';

export const CONNECTION_TYPES: { value: ConnectionType; label: string }[] = [
  { value: 'rest', label: 'REST API' },
  { value: 'mcp', label: 'MCP server' },
  { value: 'database', label: 'Database' },
  { value: 'data_platform', label: 'Data platform' },
  { value: 'model_api', label: 'Model API' },
];

const TYPE_VALUES = CONNECTION_TYPES.map((t) => t.value as string);
const KEY_TYPE: Record<string, ConnectionType> = {
  'rest.http': 'rest',
  mcp_http: 'mcp',
  postgres: 'database',
  openai_compatible: 'model_api',
  anthropic: 'model_api',
  fake: 'model_api',
};

/** Best guess of the connection type an adapter serves: its `kind` if it is a type, else a known key. */
export function inferType(a: Adapter): ConnectionType | null {
  if (TYPE_VALUES.includes(a.kind)) return a.kind as ConnectionType;
  return KEY_TYPE[a.key] ?? null;
}

const STEPS = ['Basics', 'Configuration', 'Access'] as const;
const BASE_KEYS = ['name', 'auth_method', 'secret_id', 'egress_policy_id', 'access_mode'];
const STEP_OF: Record<string, number> = {
  name: 0,
  adapter_key: 0,
  connection_type: 0,
  scope: 0,
  auth_method: 2,
  secret_id: 2,
  egress_policy_id: 2,
  access_mode: 2,
};

export function ConnectionWizard({
  org,
  onClose,
  onCreated,
}: {
  org: string;
  onClose: () => void;
  onCreated: (id: string) => void;
}) {
  const { can } = useCan(org);
  const refresh = useRefreshInt(org);
  const adapters = useAdapters();
  const vendors = useVendors(org);
  const secrets = useSecrets(org);
  const canListEgress = can('egress:manage');
  const egress = useEgressPolicies(org, canListEgress);
  const action = useAction();

  const [step, setStep] = useState(0);
  const [name, setName] = useState('');
  const [adapterKey, setAdapterKey] = useState('');
  const [type, setType] = useState<ConnectionType | ''>('');
  const [vendorId, setVendorId] = useState('');
  const [scope, setScope] = useState({ workspace: '', project: '' });
  const [values, setValues] = useState<FormValues>({});
  const [authMethod, setAuthMethod] = useState('');
  const [secretId, setSecretId] = useState('');
  const [egressId, setEgressId] = useState('');
  const [access, setAccess] = useState<AccessMode>('read');
  const [errors, setErrors] = useState<Record<string, string>>({});

  const adapter = adapters.data?.find((a) => a.key === adapterKey);
  const fields = fieldsFromSchema(adapter?.config_schema, adapter?.secret_fields);
  const fieldKeys = fields.map((f) => f.key);
  const serverErr =
    action.error instanceof ApiError
      ? mapServerErrors(action.error.fieldErrors, [...fieldKeys, ...BASE_KEYS])
      : null;
  const shown = { ...(serverErr?.byField ?? {}), ...errors };
  const needsSecret = authMethod !== '' && authMethod !== 'none';

  function pickAdapter(key: string) {
    setAdapterKey(key);
    const a = adapters.data?.find((x) => x.key === key);
    setValues(a ? initialValues(fieldsFromSchema(a.config_schema, a.secret_fields)) : {});
    setAuthMethod(a?.auth_methods[0] ?? '');
    setSecretId('');
    const inferred = a ? inferType(a) : null;
    if (inferred) setType(inferred);
  }

  function validate(s: number): Record<string, string> {
    const e: Record<string, string> = {};
    if (s === 0) {
      if (name.trim() === '') e.name = 'Name is required.';
      if (adapterKey === '') e.adapter_key = 'Choose an adapter.';
      if (type === '') e.connection_type = 'Choose a connection type.';
    } else if (s === 1) {
      Object.assign(e, validateValues(fields, values));
    } else {
      if (authMethod === '') e.auth_method = 'Choose an authentication method.';
      if (needsSecret && secretId === '')
        e.secret_id = 'Choose the secret that holds the credential.';
    }
    return e;
  }

  function next() {
    const e = validate(step);
    setErrors(e);
    if (Object.keys(e).length === 0) setStep(step + 1);
  }

  async function create() {
    const all = [0, 1, 2].map(validate);
    const firstBad = all.findIndex((e) => Object.keys(e).length > 0);
    if (firstBad >= 0) {
      setErrors(all[firstBad] ?? {});
      setStep(firstBad);
      return;
    }
    setErrors({});
    const input: ConnectionInput = {
      name: name.trim(),
      scope: {
        ...(scope.workspace ? { workspace: scope.workspace } : {}),
        ...(scope.project ? { project: scope.project } : {}),
      },
      connection_type: type as ConnectionType,
      adapter_key: adapterKey,
      ...(vendorId ? { vendor_id: vendorId } : {}),
      config: buildConfig(fields, values),
      auth_method: authMethod,
      ...(needsSecret ? { secret_id: secretId } : {}),
      ...(egressId ? { egress_policy_id: egressId } : {}),
      access_mode: access,
    };
    const res = await action.run(() => endpoints.createConnection(org, input));
    if (res.ok) {
      await refresh('connections');
      onCreated(res.value.id);
      return;
    }
  }

  // After a failed create, surface the step of the first field the server complained about.
  const serverStep = serverErr
    ? Math.min(
        ...Object.keys(serverErr.byField).map((k) =>
          fieldKeys.includes(k) ? 1 : (STEP_OF[k] ?? 2),
        ),
        99,
      )
    : 99;

  const adapterOptions = adapters.data ?? [];
  const activeSecrets = (secrets.data?.items ?? []).filter((s) => s.status !== 'disabled');
  const isLast = step === STEPS.length - 1;

  return (
    <Modal title="Create connection" onClose={onClose} busy={action.busy} wide>
      <ol aria-label="Steps" className="mb-4 flex gap-4 text-sm">
        {STEPS.map((s, i) => (
          <li
            key={s}
            aria-current={i === step ? 'step' : undefined}
            className={i === step ? 'font-semibold text-blue-900' : 'text-slate-600'}
          >
            {i + 1}. {s}
          </li>
        ))}
      </ol>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (isLast) void create();
          else next();
        }}
        className="space-y-4"
        noValidate
      >
        {step === 0 && (
          <>
            <TextField
              label="Name"
              value={name}
              onChange={setName}
              error={shown.name}
              maxLength={200}
            />
            <div>
              <SelectField
                label="Adapter"
                value={adapterKey}
                onChange={pickAdapter}
                options={[
                  { value: '', label: 'Select an adapter...' },
                  ...adapterOptions.map((a) => ({
                    value: a.key,
                    label: isPlannedAdapter(a) ? `${a.key} (declared only, unavailable)` : a.key,
                  })),
                ]}
              />
              {shown.adapter_key && (
                <p className="mt-1 text-sm text-red-700">{shown.adapter_key}</p>
              )}
              {adapter && isPlannedAdapter(adapter) && (
                <p className="mt-1 text-sm text-slate-600">
                  Declared only: it cannot be tested or invoked.
                </p>
              )}
            </div>
            <div>
              <SelectField
                label="Connection type"
                value={type}
                onChange={(v) => setType(v as ConnectionType)}
                options={[{ value: '', label: 'Select a type...' }, ...CONNECTION_TYPES]}
              />
              {shown.connection_type && (
                <p className="mt-1 text-sm text-red-700">{shown.connection_type}</p>
              )}
            </div>
            <SelectField
              label="Vendor (optional)"
              value={vendorId}
              onChange={setVendorId}
              options={[
                { value: '', label: 'None' },
                ...(vendors.data?.items ?? [])
                  .filter((v) => v.status === 'active')
                  .map((v) => ({ value: v.id, label: v.name })),
              ]}
            />
            <SlugScopeSelect org={org} {...scope} onChange={setScope} />
          </>
        )}

        {step === 1 && (
          <SchemaForm
            fields={fields}
            values={values}
            errors={shown}
            onChange={(k, v) => setValues((cur) => ({ ...cur, [k]: v }))}
          />
        )}

        {step === 2 && (
          <>
            <div>
              <SelectField
                label="Authentication method"
                value={authMethod}
                onChange={(v) => {
                  setAuthMethod(v);
                  if (v === 'none') setSecretId('');
                }}
                options={[
                  { value: '', label: 'Select a method...' },
                  ...(adapter?.auth_methods ?? []).map((m) => ({ value: m, label: m })),
                ]}
              />
              {shown.auth_method && (
                <p className="mt-1 text-sm text-red-700">{shown.auth_method}</p>
              )}
            </div>
            {needsSecret && (
              <div>
                <SelectField
                  label="Secret"
                  value={secretId}
                  onChange={setSecretId}
                  options={[
                    { value: '', label: 'Select a secret...' },
                    ...activeSecrets.map((s) => ({
                      value: s.id,
                      label: `${s.name} (version ${s.version})`,
                    })),
                  ]}
                />
                <p className="mt-1 text-xs text-slate-600">
                  Only the secret&apos;s name is listed. Its value is never displayed, and you need
                  permission to use it.
                </p>
                {secrets.isError && (
                  <p className="mt-1 text-sm text-amber-900">
                    Secrets could not be listed with your permissions.
                  </p>
                )}
                {shown.secret_id && <p className="mt-1 text-sm text-red-700">{shown.secret_id}</p>}
              </div>
            )}
            <div>
              <SelectField
                label="Egress policy"
                value={egressId}
                disabled={!canListEgress}
                onChange={setEgressId}
                options={[
                  { value: '', label: 'Default (installation and organization policy)' },
                  ...(egress.data?.items ?? []).map((p) => ({ value: p.id, label: p.name })),
                ]}
              />
              {!canListEgress && (
                <p className="mt-1 text-xs text-slate-600">
                  Choosing a custom egress policy needs the egress:manage permission. The default
                  policy applies.
                </p>
              )}
            </div>
            <SelectField
              label="Access mode"
              value={access}
              onChange={(v) => setAccess(v as AccessMode)}
              options={[
                { value: 'read', label: 'Read only' },
                { value: 'read_write', label: 'Read and write' },
              ]}
            />
            {access === 'read_write' && (
              <p className="text-xs text-slate-600">
                Writes also require the connection:write permission at invocation time.
              </p>
            )}
          </>
        )}

        {action.error !== null && (
          <>
            <ErrorMessage error={action.error} title="Could not create the connection" />
            {serverStep !== 99 && serverStep !== step && (
              <button type="button" className="btn" onClick={() => setStep(serverStep)}>
                Go to the field with the error
              </button>
            )}
          </>
        )}

        <div className="mt-5 flex justify-end gap-2">
          <button type="button" className="btn" onClick={onClose} disabled={action.busy}>
            Cancel
          </button>
          {step > 0 && (
            <button
              type="button"
              className="btn"
              onClick={() => setStep(step - 1)}
              disabled={action.busy}
            >
              Back
            </button>
          )}
          <button type="submit" className="btn btn-primary" disabled={action.busy}>
            {isLast ? (action.busy ? 'Working...' : 'Create connection') : 'Next'}
          </button>
        </div>
      </form>
    </Modal>
  );
}
