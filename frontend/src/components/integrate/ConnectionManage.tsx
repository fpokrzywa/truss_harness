'use client';

import { useState } from 'react';
import { destinationChanged, visibleConfigFields } from '@/lib/adapter-fields';
import { ApiError } from '@/lib/api/errors';
import { endpoints } from '@/lib/api/endpoints';
import type {
  Adapter,
  Connection,
  ConnectionPatch,
  ConnectionTestResult,
} from '@/lib/api/integrate-types';
import { credentialError } from '@/lib/credential';
import { useRefreshInt } from '@/lib/integrate-hooks';
import {
  buildConfig,
  fieldsFromSchema,
  mapServerErrors,
  validateValues,
  valuesFromConfig,
  type FormValues,
} from '@/lib/schema-form';
import { ConfirmDialog } from '../ConfirmDialog';
import { ActionError, Modal, ModalActions, TextField, useAction } from '../admin/ui';
import { TestResultView } from './ConnectionTests';
import { SchemaForm } from './SchemaForm';

/** Edit name and the visible config fields; only changed fields are sent (CM-7). */
export function EditConnectionDialog({
  org,
  connection: c,
  adapter,
  onClose,
}: {
  org: string;
  connection: Connection;
  adapter: Adapter | undefined;
  onClose: () => void;
}) {
  const refresh = useRefreshInt(org);
  const action = useAction();
  const fields = visibleConfigFields(
    c.adapter_key,
    c.auth_method,
    fieldsFromSchema(adapter?.config_schema, adapter?.secret_fields),
  );
  const fieldKeys = fields.map((f) => f.key);
  const [initial] = useState<FormValues>(() => valuesFromConfig(fields, c.config));
  const [name, setName] = useState(c.name);
  const [values, setValues] = useState<FormValues>(initial);
  const [errors, setErrors] = useState<Record<string, string>>({});

  const configDirty = fieldKeys.some((k) => values[k] !== initial[k]);
  // Keys the form does not show (other auth methods, unknown to the schema) are kept as they are.
  const kept = Object.fromEntries(Object.entries(c.config).filter(([k]) => !fieldKeys.includes(k)));
  const nextConfig = { ...kept, ...buildConfig(fields, values) };
  const moved = configDirty && destinationChanged(c.adapter_key, c.config, nextConfig);
  const serverErr =
    action.error instanceof ApiError
      ? mapServerErrors(action.error.fieldErrors, [...fieldKeys, 'name'])
      : null;
  const shown = { ...(serverErr?.byField ?? {}), ...errors };

  async function save() {
    const e: Record<string, string> = validateValues(fields, values);
    if (name.trim() === '') e.name = 'Name is required.';
    setErrors(e);
    if (Object.keys(e).length > 0) return;
    const patch: Omit<ConnectionPatch, 'row_version'> = {
      ...(name.trim() !== c.name ? { name: name.trim() } : {}),
      ...(configDirty ? { config: nextConfig } : {}),
    };
    if (Object.keys(patch).length === 0) {
      onClose();
      return;
    }
    const res = await action.run(() => endpoints.updateConnection(org, c, patch));
    if (res.ok) {
      await refresh('connections');
      onClose();
    }
  }

  return (
    <Modal title="Edit connection" onClose={onClose} busy={action.busy} wide>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void save();
        }}
        className="space-y-4"
        noValidate
      >
        <TextField
          label="Name"
          value={name}
          onChange={setName}
          error={shown.name}
          maxLength={200}
        />
        <SchemaForm
          fields={fields}
          values={values}
          errors={shown}
          onChange={(k, v) => setValues((cur) => ({ ...cur, [k]: v }))}
        />
        {configDirty && (
          <p className="text-xs text-slate-600">
            Saving a configuration change moves the connection back to draft until it is re-tested.
          </p>
        )}
        {moved && (
          <div role="note" className="rounded-md border border-amber-300 bg-amber-50 p-3 text-sm">
            <p className="font-medium text-amber-900">The destination changes.</p>
            <p className="mt-1 text-amber-900">
              The stored credential will be sent to the new destination. This needs the secret:use
              permission on the bound secret, and the connection returns to draft until it passes a
              new test.
            </p>
          </div>
        )}
        <ActionError error={action.error} title="Could not save the connection" />
        <ModalActions onCancel={onClose} busy={action.busy} submitLabel="Save changes" />
      </form>
    </Modal>
  );
}

/**
 * Set or rotate the connection's credential (CM-4, B-2749). The old value is never fetched or
 * shown; the new one lives in this component's state only until the request ends.
 */
export function CredentialSection({
  org,
  connection: c,
  secretName,
  canRotate,
  testBlocked,
}: {
  org: string;
  connection: Connection;
  secretName: string | undefined;
  canRotate: boolean;
  testBlocked: boolean;
}) {
  const refresh = useRefreshInt(org);
  const save = useAction();
  const test = useAction();
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState('');
  const [confirmed, setConfirmed] = useState(false);
  const [valueError, setValueError] = useState<string | undefined>();
  const [confirmError, setConfirmError] = useState<string | undefined>();
  const [saved, setSaved] = useState(false);
  const [result, setResult] = useState<ConnectionTestResult | null>(null);
  const label = c.secret_set ? 'Rotate credential' : 'Set credential';
  const needsConfirm = c.access_mode === 'read';
  const shared = secretName !== undefined && secretName !== `connection-${c.id}`;

  function cancel() {
    setValue('');
    setConfirmed(false);
    setValueError(undefined);
    setConfirmError(undefined);
    setOpen(false);
  }

  async function submit() {
    const bad = credentialError(value);
    const unconfirmed =
      needsConfirm && !confirmed ? 'Confirm that the credential is read-only.' : undefined;
    setValueError(bad);
    setConfirmError(unconfirmed);
    if (bad || unconfirmed) return;
    const v = value;
    setValue('');
    const res = await save.run(() =>
      endpoints.setConnectionCredential(org, c, v, needsConfirm ? true : undefined),
    );
    setConfirmed(false);
    setOpen(false);
    if (res.ok) {
      setSaved(true);
      setResult(null);
      await refresh('connections');
    }
  }

  return (
    <section aria-labelledby="cred-h" className="card space-y-3">
      <h2 id="cred-h" className="text-lg font-semibold">
        Credential
      </h2>
      <p className="text-sm text-slate-600">
        {c.secret_set
          ? 'A credential is set. Its value is never shown.'
          : 'No credential is set for this connection.'}
      </p>
      {canRotate && (
        <button
          type="button"
          className="btn"
          disabled={save.busy}
          onClick={() => {
            setSaved(false);
            setOpen(true);
          }}
        >
          {label}
        </button>
      )}
      <ActionError error={save.error} title="Could not save the credential" />
      {saved && (
        <div role="status" className="space-y-2 text-sm">
          <p>The credential was saved. Test the connection to confirm it works.</p>
          <button
            type="button"
            className="btn btn-primary"
            disabled={testBlocked || test.busy}
            onClick={async () => {
              const res = await test.run(() => endpoints.testConnection(org, c.id));
              if (res.ok) {
                setResult(res.value);
                await refresh('connections');
              }
            }}
          >
            {test.busy ? 'Testing...' : 'Test now'}
          </button>
        </div>
      )}
      <ActionError error={test.error} title="Could not run the test" />
      {result && <TestResultView result={result} />}
      <ConfirmDialog
        open={open}
        title={`${label}?`}
        confirmLabel={label}
        busy={save.busy}
        onConfirm={() => void submit()}
        onCancel={cancel}
      >
        <div className="space-y-3">
          <TextField
            label="New credential value"
            type="password"
            value={value}
            onChange={setValue}
            error={valueError}
            autoComplete="new-password"
          />
          {shared && (
            <p>
              This connection uses the shared secret <strong>{secretName}</strong>. Other
              connections that bind this secret use the new value too.
            </p>
          )}
          {needsConfirm && (
            <div>
              <label className="flex items-center gap-2 font-medium text-slate-800">
                <input
                  type="checkbox"
                  checked={confirmed}
                  onChange={(e) => setConfirmed(e.target.checked)}
                />
                I confirm this credential is read-only
              </label>
              {confirmError && <p className="mt-1 text-red-700">{confirmError}</p>}
            </div>
          )}
          <p className="text-slate-600">
            The connection&apos;s health returns to unknown until it is tested again.
          </p>
        </div>
      </ConfirmDialog>
    </section>
  );
}
