'use client';

import { useState, type FormEvent } from 'react';
import { endpoints } from '@/lib/api/endpoints';
import type { Allowlist, Model, ModelRoute, RouteScope } from '@/lib/api/integrate-types';
import { useAllowlist, useEffectiveModels, useRefreshInt, useRoutes } from '@/lib/integrate-hooks';
import { ScopePicker, useOrgId } from '../admin/ScopePicker';
import {
  ActionError,
  EmptyState,
  Modal,
  ModalActions,
  QueryGate,
  SelectField,
  TextField,
  useAction,
} from '../admin/ui';
import { StatusBadge } from './Maturity';
import { SlugScopeSelect } from './SlugScopeSelect';

type PickerScope = { type: 'tenant' | 'workspace' | 'project'; id: string };

/** Effective allowlist per scope: tenant > workspace > project, each level can only narrow. */
export function AllowlistEditor({
  org,
  models,
  canManage,
}: {
  org: string;
  models: Model[];
  canManage: boolean;
}) {
  const orgId = useOrgId(org) ?? '';
  const [picked, setPicked] = useState<PickerScope>({ type: 'tenant', id: '' });
  const scope = picked.type === 'tenant' ? { ...picked, id: orgId } : picked;
  const allowlist = useAllowlist(org, scope.type, scope.id);

  return (
    <section aria-labelledby="allow-h" className="card space-y-4">
      <div>
        <h2 id="allow-h" className="text-lg font-semibold">
          Model allowlists
        </h2>
        <p className="text-sm text-slate-600">
          When an allowlist is enabled at a scope, models not on it are denied. The effective set is
          the intersection down the organization, workspace and project chain.
        </p>
      </div>
      <ScopePicker org={org} value={scope} onChange={setPicked} />
      {scope.id === '' ? (
        <p className="text-sm text-slate-600">Choose a scope to edit its allowlist.</p>
      ) : (
        <QueryGate query={allowlist} noun="allowlist">
          {(data) => (
            <AllowlistForm
              key={`${scope.type}:${scope.id}:${data.enabled}:${data.model_ids.join(',')}`}
              org={org}
              scope={scope}
              models={models}
              initial={data}
              canManage={canManage}
            />
          )}
        </QueryGate>
      )}
    </section>
  );
}

function AllowlistForm({
  org,
  scope,
  models,
  initial,
  canManage,
}: {
  org: string;
  scope: PickerScope;
  models: Model[];
  initial: Allowlist;
  canManage: boolean;
}) {
  const refresh = useRefreshInt(org);
  const action = useAction();
  const [enabled, setEnabled] = useState(initial.enabled);
  const [ids, setIds] = useState<string[]>(initial.model_ids);
  const [saved, setSaved] = useState(false);

  async function save(e: FormEvent) {
    e.preventDefault();
    setSaved(false);
    const res = await action.run(() =>
      endpoints.putAllowlist(org, scope.type, scope.id, { enabled, model_ids: ids }),
    );
    if (res.ok) {
      setSaved(true);
      await refresh('allowlist', 'models');
    }
  }

  return (
    <form onSubmit={(e) => void save(e)} className="space-y-3">
      <label className="flex items-center gap-2 text-sm font-medium">
        <input
          type="checkbox"
          checked={enabled}
          disabled={!canManage}
          onChange={(e) => setEnabled(e.target.checked)}
        />
        Enforce allowlist at this scope
      </label>
      {enabled && ids.length === 0 && (
        <p role="note" className="text-sm text-amber-900">
          The allowlist is enabled but empty: every model is denied at this scope.
        </p>
      )}
      <fieldset disabled={!canManage || !enabled}>
        <legend className="label">Allowed models</legend>
        {models.length === 0 ? (
          <EmptyState>No models are registered.</EmptyState>
        ) : (
          <ul className="grid gap-1 sm:grid-cols-2">
            {models.map((m) => (
              <li key={m.id}>
                <label className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={ids.includes(m.id)}
                    onChange={(e) =>
                      setIds((cur) =>
                        e.target.checked ? [...cur, m.id] : cur.filter((x) => x !== m.id),
                      )
                    }
                  />
                  {m.display_name}
                </label>
              </li>
            ))}
          </ul>
        )}
      </fieldset>
      <ActionError error={action.error} title="Could not save the allowlist" />
      {saved && (
        <p role="status" className="text-sm text-green-800">
          Allowlist saved.
        </p>
      )}
      {canManage && (
        <button type="submit" className="btn btn-primary" disabled={action.busy}>
          {action.busy ? 'Saving...' : 'Save allowlist'}
        </button>
      )}
    </form>
  );
}

export function EffectiveModels({ org }: { org: string }) {
  const [scope, setScope] = useState({ workspace: '', project: '' });
  const effective = useEffectiveModels(
    org,
    scope.workspace || undefined,
    scope.project || undefined,
  );
  return (
    <section aria-labelledby="eff-h" className="card space-y-3">
      <div>
        <h2 id="eff-h" className="text-lg font-semibold">
          Effective models
        </h2>
        <p className="text-sm text-slate-600">
          Models usable at a scope: allowlist, lifecycle and provider health all applied.
        </p>
      </div>
      <SlugScopeSelect org={org} {...scope} onChange={setScope} />
      <QueryGate query={effective} noun="effective models">
        {(data) =>
          data.items.length === 0 ? (
            <EmptyState>No models are usable at this scope.</EmptyState>
          ) : (
            <ul aria-label="Effective models" className="divide-y divide-slate-100">
              {data.items.map((m) => (
                <li key={m.id} className="flex items-center justify-between py-2 text-sm">
                  <span>
                    {m.display_name}{' '}
                    <span className="font-mono text-xs text-slate-600">{m.model_key}</span>
                  </span>
                  <StatusBadge status={m.lifecycle} />
                </li>
              ))}
            </ul>
          )
        }
      </QueryGate>
    </section>
  );
}

export function RoutesEditor({
  org,
  models,
  canManage,
}: {
  org: string;
  models: Model[];
  canManage: boolean;
}) {
  const routes = useRoutes(org);
  const [editing, setEditing] = useState<ModelRoute | 'new' | null>(null);
  const name = (id: string) => models.find((m) => m.id === id)?.display_name ?? id;
  return (
    <section aria-labelledby="routes-h" className="card space-y-3">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h2 id="routes-h" className="text-lg font-semibold">
            Model routes
          </h2>
          <p className="text-sm text-slate-600">
            A primary model plus ordered fallbacks, tried in order. Every model must be allowed at
            the route&apos;s scope.
          </p>
        </div>
        {canManage && (
          <button type="button" className="btn" onClick={() => setEditing('new')}>
            Create route
          </button>
        )}
      </div>
      <QueryGate query={routes} noun="routes">
        {(data) =>
          data.items.length === 0 ? (
            <EmptyState>No routes yet.</EmptyState>
          ) : (
            <table className="w-full">
              <caption className="sr-only">Model routes</caption>
              <thead className="border-b border-slate-200">
                <tr>
                  {['Route', 'Scope', 'Primary', 'Fallbacks (in order)'].map((h) => (
                    <th key={h} scope="col" className="th">
                      {h}
                    </th>
                  ))}
                  <th scope="col" className="th">
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {data.items.map((r) => (
                  <tr key={r.id}>
                    <td className="td font-medium">{r.name}</td>
                    <td className="td">{r.scope.type}</td>
                    <td className="td">{name(r.primary_model_id)}</td>
                    <td className="td">
                      {r.fallback_model_ids.length === 0
                        ? 'None'
                        : r.fallback_model_ids.map((id, i) => `${i + 1}. ${name(id)}`).join(', ')}
                    </td>
                    <td className="td text-right">
                      {canManage && (
                        <button type="button" className="btn" onClick={() => setEditing(r)}>
                          Edit <span className="sr-only">{r.name}</span>
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )
        }
      </QueryGate>
      {editing && (
        <RouteDialog
          org={org}
          models={models}
          route={editing === 'new' ? null : editing}
          onClose={() => setEditing(null)}
        />
      )}
    </section>
  );
}

function RouteDialog({
  org,
  models,
  route,
  onClose,
}: {
  org: string;
  models: Model[];
  route: ModelRoute | null;
  onClose: () => void;
}) {
  const refresh = useRefreshInt(org);
  const orgId = useOrgId(org) ?? '';
  const action = useAction();
  const [name, setName] = useState(route?.name ?? '');
  const [picked, setPicked] = useState<PickerScope>(
    route ? { type: route.scope.type, id: route.scope.id } : { type: 'tenant', id: '' },
  );
  const [primary, setPrimary] = useState(route?.primary_model_id ?? '');
  const [fallbacks, setFallbacks] = useState<string[]>(route?.fallback_model_ids ?? []);
  const [errors, setErrors] = useState<{ name?: string; primary?: string; scope?: string }>({});
  const scope = picked.type === 'tenant' ? { ...picked, id: orgId } : picked;
  const modelName = (id: string) => models.find((m) => m.id === id)?.display_name ?? id;
  const addable = models.filter((m) => m.id !== primary && !fallbacks.includes(m.id));

  function move(i: number, delta: -1 | 1) {
    setFallbacks((cur) => {
      const next = [...cur];
      const [item] = next.splice(i, 1);
      if (item === undefined) return cur;
      next.splice(i + delta, 0, item);
      return next;
    });
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    const next = {
      name: name.trim() === '' ? 'Name is required.' : undefined,
      primary: primary === '' ? 'Choose a primary model.' : undefined,
      scope: scope.id === '' ? 'Choose a scope.' : undefined,
    };
    setErrors(next);
    if (next.name || next.primary || next.scope) return;
    const body = {
      name: name.trim(),
      scope: { type: scope.type, id: scope.id } satisfies RouteScope,
      primary_model_id: primary,
      fallback_model_ids: fallbacks,
    };
    const res = await action.run(() =>
      route
        ? endpoints.updateRoute(org, route.id, { ...body, row_version: route.row_version })
        : endpoints.createRoute(org, body),
    );
    if (res.ok) {
      await refresh('routes');
      onClose();
    }
  }

  return (
    <Modal
      title={route ? `Edit ${route.name}` : 'Create route'}
      onClose={onClose}
      busy={action.busy}
      wide
    >
      <form onSubmit={(e) => void submit(e)} className="space-y-4" noValidate>
        <TextField label="Name" value={name} onChange={setName} error={errors.name} />
        <ScopePicker org={org} value={scope} onChange={setPicked} />
        {errors.scope && <p className="text-sm text-red-700">{errors.scope}</p>}
        <div>
          <SelectField
            label="Primary model"
            value={primary}
            onChange={(v) => {
              setPrimary(v);
              setFallbacks((cur) => cur.filter((x) => x !== v));
            }}
            options={[
              { value: '', label: 'Select a model...' },
              ...models.map((m) => ({ value: m.id, label: m.display_name })),
            ]}
          />
          {errors.primary && <p className="mt-1 text-sm text-red-700">{errors.primary}</p>}
        </div>
        <div>
          <p className="label">Fallbacks, tried in this order</p>
          {fallbacks.length === 0 ? (
            <p className="text-sm text-slate-600">No fallbacks.</p>
          ) : (
            <ol aria-label="Fallback order" className="space-y-1">
              {fallbacks.map((id, i) => (
                <li key={id} className="flex items-center gap-2 text-sm">
                  <span className="w-6 text-slate-500">{i + 1}.</span>
                  <span className="flex-1">{modelName(id)}</span>
                  <button
                    type="button"
                    className="btn"
                    disabled={i === 0}
                    onClick={() => move(i, -1)}
                  >
                    Move up <span className="sr-only">{modelName(id)}</span>
                  </button>
                  <button
                    type="button"
                    className="btn"
                    disabled={i === fallbacks.length - 1}
                    onClick={() => move(i, 1)}
                  >
                    Move down <span className="sr-only">{modelName(id)}</span>
                  </button>
                  <button
                    type="button"
                    className="btn"
                    onClick={() => setFallbacks((cur) => cur.filter((x) => x !== id))}
                  >
                    Remove <span className="sr-only">{modelName(id)}</span>
                  </button>
                </li>
              ))}
            </ol>
          )}
          {addable.length > 0 && (
            <div className="mt-2">
              <SelectField
                label="Add fallback"
                value=""
                onChange={(v) => v && setFallbacks((cur) => [...cur, v])}
                options={[
                  { value: '', label: 'Select a model...' },
                  ...addable.map((m) => ({ value: m.id, label: m.display_name })),
                ]}
              />
            </div>
          )}
        </div>
        <ActionError error={action.error} title="Could not save the route" />
        <ModalActions onCancel={onClose} busy={action.busy} submitLabel="Save route" />
      </form>
    </Modal>
  );
}
