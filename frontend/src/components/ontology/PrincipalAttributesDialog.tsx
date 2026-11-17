'use client';

import { useState, type FormEvent } from 'react';
import { endpoints } from '@/lib/api/endpoints';
import { ApiError } from '@/lib/api/errors';
import type { PrincipalAttributes, PrincipalType } from '@/lib/api/ontology-types';
import { useUsers } from '@/lib/security-hooks';
import { ActionError, Modal, ModalActions, SelectField, TextField, useAction } from '../admin/ui';

const MAX_ATTRIBUTES = 50;

export type AttributesParse =
  { ok: true; value: Record<string, unknown> } | { ok: false; error: string };

const isScalar = (v: unknown) =>
  typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean';

/** Mirrors the server rules: names are letters/digits/_, values are scalars or lists of scalars. */
export function parseAttributes(text: string): AttributesParse {
  let parsed: unknown;
  try {
    parsed = JSON.parse(text.trim() || '{}');
  } catch {
    return { ok: false, error: 'Attributes are not valid JSON.' };
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed))
    return { ok: false, error: 'Attributes must be a JSON object.' };
  const entries = Object.entries(parsed);
  if (entries.length > MAX_ATTRIBUTES)
    return { ok: false, error: `At most ${MAX_ATTRIBUTES} attributes are allowed.` };
  for (const [k, v] of entries) {
    if (!/^[A-Za-z0-9_]+$/.test(k))
      return { ok: false, error: `Attribute name "${k}" may only use letters, digits and _.` };
    if (!(isScalar(v) || (Array.isArray(v) && v.every(isScalar))))
      return { ok: false, error: `Attribute "${k}" must be a string, number, boolean or a list.` };
  }
  return { ok: true, value: parsed as Record<string, unknown> };
}

/**
 * Edits the attribute map that policy row filters read as `principal.attributes.*` (for example
 * `region`). Requires `ontology:manage_policies`. A principal without attributes matches no
 * attribute-based row filter (fail closed).
 */
export function PrincipalAttributesDialog({ org, onClose }: { org: string; onClose: () => void }) {
  const users = useUsers(org);
  const load = useAction();
  const save = useAction();
  const [type, setType] = useState<PrincipalType>('user');
  const [principalId, setPrincipalId] = useState('');
  const [loaded, setLoaded] = useState<{ id: string; rowVersion?: number } | null>(null);
  const [text, setText] = useState('{}');
  const [saved, setSaved] = useState(false);

  const parsed = parseAttributes(text);

  function reset(nextType: PrincipalType, nextId: string) {
    setType(nextType);
    setPrincipalId(nextId);
    setLoaded(null);
    setSaved(false);
  }

  async function doLoad() {
    const id = principalId.trim();
    if (!id) return;
    const r = await load.run(async (): Promise<PrincipalAttributes | null> => {
      try {
        return await endpoints.getPrincipalAttributes(org, type, id);
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) return null; // none set yet
        throw e;
      }
    });
    if (r.ok) {
      setLoaded({ id, ...(r.value ? { rowVersion: r.value.row_version } : {}) });
      setText(JSON.stringify(r.value?.attributes ?? {}, null, 2));
      setSaved(false);
    }
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!loaded || !parsed.ok) return;
    const r = await save.run(() =>
      endpoints.putPrincipalAttributes(org, type, loaded.id, parsed.value, loaded.rowVersion),
    );
    if (r.ok) {
      setLoaded({ id: loaded.id, rowVersion: r.value.row_version });
      setText(JSON.stringify(r.value.attributes, null, 2));
      setSaved(true);
    }
  }

  return (
    <Modal title="Principal attributes" onClose={onClose} busy={save.busy || load.busy} wide>
      <form onSubmit={(e) => void submit(e)} className="space-y-3" noValidate>
        <p className="text-sm text-slate-700">
          Attributes feed row filters such as <code>row.region == principal.attributes.region</code>
          . A principal without the attribute sees no rows from that policy.
        </p>
        <div className="grid gap-3 sm:grid-cols-2">
          <SelectField
            label="Principal type"
            value={type}
            onChange={(v) => reset(v as PrincipalType, '')}
            options={[
              { value: 'user', label: 'User' },
              { value: 'service_account', label: 'Service account' },
            ]}
          />
          {type === 'user' && users.isSuccess ? (
            <SelectField
              label="Principal"
              value={principalId}
              onChange={(v) => reset(type, v)}
              options={[
                { value: '', label: 'Select...' },
                ...users.data.items.map((u) => ({
                  value: u.user.id,
                  label: `${u.user.display_name} (${u.user.email})`,
                })),
              ]}
            />
          ) : (
            <TextField
              label="Principal id"
              value={principalId}
              onChange={(v) => reset(type, v)}
              monospace
            />
          )}
        </div>
        <div>
          <button
            type="button"
            className="btn"
            disabled={!principalId.trim() || load.busy}
            onClick={() => void doLoad()}
          >
            {load.busy ? 'Loading...' : 'Load attributes'}
          </button>
        </div>
        <ActionError error={load.error} title="Could not load attributes" />
        {loaded && (
          <>
            <TextField
              label="Attributes (JSON object)"
              value={text}
              onChange={(v) => {
                setText(v);
                setSaved(false);
              }}
              multiline
              monospace
              help='Values are strings, numbers, booleans or lists of those, for example {"region": "emea"}.'
              error={parsed.ok ? null : parsed.error}
            />
            <ActionError error={save.error} title="Could not save attributes" />
            {saved && (
              <p role="status" className="text-sm text-green-800">
                Attributes saved.
              </p>
            )}
            <ModalActions
              onCancel={onClose}
              submitLabel="Save attributes"
              busy={save.busy}
              submitDisabled={!parsed.ok}
            />
          </>
        )}
      </form>
    </Modal>
  );
}
