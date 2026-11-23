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
import { credentialError } from '@/lib/credential';
import { useCan, usePermissions, useProjects, useWorkspaces } from '@/lib/hooks';
import { useAdapters, useEgressPolicies, useRefreshInt, useVendors } from '@/lib/integrate-hooks';
import { isPlannedAdapter } from '@/lib/maturity';
import {
  buildConfig,
  fieldsFromSchema,
  initialValues,
  mapServerErrors,
  validateValues,
  visibleConfigFields,
  type FormValues,
} from '@/lib/schema-form';
import { useSecrets } from '@/lib/security-hooks';
import { ErrorMessage } from '../ErrorMessage';
import { useOrgId } from '../admin/ScopePicker';
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

const STEPS = ['Kind', 'Scope', 'Configuration', 'Credential', 'Access', 'Review'] as const;
const CONFIG_STEP = 2;
const CREDENTIAL_STEP = 3;
const ACCESS_STEP = 4;
const BASE_KEYS = [
  'name',
  'auth_method',
  'secret_id',
  'credential',
  'egress_policy_id',
  'access_mode',
  'read_only_confirmed',
];
const STEP_OF: Record<string, number> = {
  name: 0,
  adapter_key: 0,
  connection_type: 0,
  auth_method: 0,
  scope: 1,
  credential: CREDENTIAL_STEP,
  secret_id: CREDENTIAL_STEP,
  egress_policy_id: ACCESS_STEP,
  access_mode: ACCESS_STEP,
  read_only_confirmed: ACCESS_STEP,
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
  const orgId = useOrgId(org);
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
  const [credMode, setCredMode] = useState<'value' | 'secret'>('value');
  // The credential value lives only in this state: never logged, cleared on create and close.
  const [credValue, setCredValue] = useState('');
  const [secretId, setSecretId] = useState('');
  const [egressId, setEgressId] = useState('');
  const [access, setAccess] = useState<AccessMode>('read');
  const [readOnlyConfirmed, setReadOnlyConfirmed] = useState(false);
  const [errors, setErrors] = useState<Record<string, string>>({});

  const workspaces = useWorkspaces(org);
  const projects = useProjects(org, scope.workspace || undefined);
  const scopePerms = usePermissions(org, scope.workspace || undefined, scope.project || undefined);
  const canPickSecret = scopePerms.data?.has('secret:manage') ?? false;

  const adapter = adapters.data?.find((a) => a.key === adapterKey);
  const fields = visibleConfigFields(
    adapter,
    authMethod,
    fieldsFromSchema(adapter?.config_schema, adapter?.secret_fields),
  );
  const fieldKeys = fields.map((f) => f.key);
  const serverErr =
    action.error instanceof ApiError
      ? mapServerErrors(action.error.fieldErrors, [...fieldKeys, ...BASE_KEYS])
      : null;
  const shown = { ...(serverErr?.byField ?? {}), ...errors };
  const needsSecret = authMethod !== '' && authMethod !== 'none';
  const useExisting = needsSecret && canPickSecret && credMode === 'secret';
  const needsConfirm = needsSecret && access === 'read';

  // The chosen scope as a secret scope: the org id client-wide, else the workspace or project id.
  const wsId = workspaces.data?.items.find((w) => w.slug === scope.workspace)?.id;
  const projId = projects.data?.items.find((p) => p.slug === scope.project)?.id;
  const chosenScope = scope.project
    ? { type: 'project', id: projId }
    : scope.workspace
      ? { type: 'workspace', id: wsId }
      : { type: 'tenant', id: orgId };
  const scopeSecrets = (secrets.data?.items ?? []).filter(
    (s) =>
      s.status !== 'disabled' &&
      !s.name.startsWith('connection-') &&
      typeof s.scope === 'object' &&
      s.scope !== null &&
      chosenScope.id !== undefined &&
      s.scope.type === chosenScope.type &&
      s.scope.id === chosenScope.id,
  );
  const chosenSecret = scopeSecrets.find((s) => s.id === secretId);

  function close() {
    setCredValue('');
    onClose();
  }

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
      if (adapterKey !== '' && authMethod === '')
        e.auth_method = 'Choose an authentication method.';
    } else if (s === CONFIG_STEP) {
      Object.assign(e, validateValues(fields, values));
    } else if (s === CREDENTIAL_STEP && needsSecret) {
      if (useExisting) {
        if (secretId === '') e.secret_id = 'Choose the secret that holds the credential.';
      } else {
        const msg = credentialError(credValue);
        if (msg) e.credential = msg;
      }
    } else if (s === ACCESS_STEP) {
      if (needsConfirm && !readOnlyConfirmed)
        e.read_only_confirmed = 'Confirm that the credential is read-only.';
    }
    return e;
  }

  // The credential step has nothing to ask when the method is `none`.
  const skip = (s: number) => s === CREDENTIAL_STEP && !needsSecret;

  function next() {
    const e = validate(step);
    setErrors(e);
    if (Object.keys(e).length > 0) return;
    let n = step + 1;
    while (skip(n)) n += 1;
    setStep(n);
  }

  function back() {
    let n = step - 1;
    while (skip(n)) n -= 1;
    setErrors({});
    setStep(n);
  }

  async function create() {
    const all = [0, 1, CONFIG_STEP, CREDENTIAL_STEP, ACCESS_STEP].map(validate);
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
      ...(needsSecret
        ? useExisting
          ? { secret_id: secretId }
          : { credential: { value: credValue } }
        : {}),
      ...(egressId ? { egress_policy_id: egressId } : {}),
      access_mode: access,
      ...(needsConfirm ? { read_only_confirmed: true } : {}),
    };
    const res = await action.run(() => endpoints.createConnection(org, input));
    if (res.ok) {
      setCredValue('');
      await refresh('connections');
      onCreated(res.value.id);
      return;
    }
  }

  // After a failed create, surface the step of the first field the server complained about.
  const serverStep = serverErr
    ? Math.min(
        ...Object.keys(serverErr.byField).map((k) =>
          fieldKeys.includes(k) ? CONFIG_STEP : (STEP_OF[k] ?? ACCESS_STEP),
        ),
        99,
      )
    : 99;

  const adapterOptions = adapters.data ?? [];
  const isLast = step === STEPS.length - 1;
  const scopeText = scope.project
    ? `Project ${scope.project} in workspace ${scope.workspace}`
    : scope.workspace
      ? `Workspace ${scope.workspace}`
      : 'Whole organization';
  const configEntries = Object.entries(buildConfig(fields, values));

  return (
    <Modal title="Create connection" onClose={close} busy={action.busy} wide>
      <ol aria-label="Steps" className="mb-4 flex flex-wrap gap-4 text-sm">
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
            <div>
              <SelectField
                label="Authentication method"
                value={authMethod}
                onChange={(v) => {
                  setAuthMethod(v);
                  if (v === 'none') {
                    setSecretId('');
                    setCredValue('');
                  }
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
          </>
        )}

        {step === 1 && (
          <SlugScopeSelect
            org={org}
            {...scope}
            onChange={(next) => {
              setScope(next);
              setSecretId('');
            }}
          />
        )}

        {step === CONFIG_STEP && (
          <SchemaForm
            fields={fields}
            values={values}
            errors={shown}
            onChange={(k, v) => setValues((cur) => ({ ...cur, [k]: v }))}
          />
        )}

        {step === CREDENTIAL_STEP && needsSecret && (
          <>
            {canPickSecret ? (
              <fieldset className="space-y-1">
                <legend className="label">Credential source</legend>
                <label className="flex items-center gap-2 text-sm">
                  <input
                    type="radio"
                    name="credential-source"
                    checked={credMode === 'value'}
                    onChange={() => {
                      setCredMode('value');
                      setSecretId('');
                    }}
                  />
                  Enter the credential value
                </label>
                <label className="flex items-center gap-2 text-sm">
                  <input
                    type="radio"
                    name="credential-source"
                    checked={credMode === 'secret'}
                    onChange={() => {
                      setCredMode('secret');
                      setCredValue('');
                    }}
                  />
                  Use an existing secret of this scope
                </label>
              </fieldset>
            ) : (
              <p role="note" className="text-sm text-slate-600">
                Setting a credential needs the secret:manage permission at this scope. The server
                decides.
              </p>
            )}
            {useExisting ? (
              <div>
                <SelectField
                  label="Secret"
                  value={secretId}
                  onChange={setSecretId}
                  options={[
                    { value: '', label: 'Select a secret...' },
                    ...scopeSecrets.map((s) => ({
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
            ) : (
              <TextField
                label="Credential value"
                type="password"
                value={credValue}
                onChange={setCredValue}
                error={shown.credential}
                autoComplete="new-password"
                help="Stored encrypted as this connection's own secret. It is never shown again."
              />
            )}
          </>
        )}

        {step === ACCESS_STEP && (
          <>
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
            {access === 'read_write' ? (
              <p className="text-xs text-slate-600">
                Writes also require the connection:write permission at invocation time.
              </p>
            ) : (
              <p className="text-xs text-slate-600">
                The SQL keyword check is defense in depth. The read-only credential is the real
                control, so bind a credential that can only read.
              </p>
            )}
            {needsConfirm && (
              <div>
                <label className="flex items-center gap-2 text-sm font-medium text-slate-800">
                  <input
                    type="checkbox"
                    required
                    checked={readOnlyConfirmed}
                    aria-invalid={shown.read_only_confirmed ? true : undefined}
                    onChange={(e) => setReadOnlyConfirmed(e.target.checked)}
                  />
                  I confirm this credential is read-only
                </label>
                {shown.read_only_confirmed && (
                  <p className="mt-1 text-sm text-red-700">{shown.read_only_confirmed}</p>
                )}
              </div>
            )}
          </>
        )}

        {step === 5 && (
          <dl aria-label="Review" className="grid gap-2 text-sm sm:grid-cols-2">
            <ReviewItem label="Name">{name.trim()}</ReviewItem>
            <ReviewItem label="Adapter">
              <span className="font-mono">{adapterKey}</span>
            </ReviewItem>
            <ReviewItem label="Connection type">{type}</ReviewItem>
            <ReviewItem label="Vendor">
              {vendors.data?.items.find((v) => v.id === vendorId)?.name ?? 'None'}
            </ReviewItem>
            <ReviewItem label="Scope">{scopeText}</ReviewItem>
            <ReviewItem label="Authentication method">{authMethod}</ReviewItem>
            <ReviewItem label="Credential">
              {!needsSecret
                ? 'None'
                : useExisting
                  ? `Secret ${chosenSecret?.name ?? secretId}`
                  : 'Set (hidden)'}
            </ReviewItem>
            <ReviewItem label="Egress policy">
              {egress.data?.items.find((p) => p.id === egressId)?.name ?? 'Default policy'}
            </ReviewItem>
            <ReviewItem label="Access mode">
              {access === 'read_write' ? 'Read and write' : 'Read only'}
              {needsConfirm && readOnlyConfirmed ? ' (credential confirmed read-only)' : ''}
            </ReviewItem>
            <ReviewItem label="Configuration">
              {configEntries.length === 0 ? (
                'No configuration values.'
              ) : (
                <ul className="font-mono break-all">
                  {configEntries.map(([k, v]) => (
                    <li key={k}>
                      {k}: {typeof v === 'string' ? v : JSON.stringify(v)}
                    </li>
                  ))}
                </ul>
              )}
            </ReviewItem>
          </dl>
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
          <button type="button" className="btn" onClick={close} disabled={action.busy}>
            Cancel
          </button>
          {step > 0 && (
            <button type="button" className="btn" onClick={back} disabled={action.busy}>
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

function ReviewItem({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <dt className="font-medium text-slate-600">{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}
