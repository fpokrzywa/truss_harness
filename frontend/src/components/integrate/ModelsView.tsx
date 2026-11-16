'use client';

import Link from 'next/link';
import { useSearchParams } from 'next/navigation';
import { useState, type FormEvent } from 'react';
import { endpoints } from '@/lib/api/endpoints';
import type {
  ModelCapability,
  ModelInput,
  ModelLifecycle,
  ModelProvider,
} from '@/lib/api/integrate-types';
import { useCan } from '@/lib/hooks';
import { useModels, useProviders, useRefreshInt } from '@/lib/integrate-hooks';
import {
  ActionError,
  EmptyState,
  Modal,
  ModalActions,
  PageHeader,
  QueryGate,
  SelectField,
  TextField,
  useAction,
} from '../admin/ui';
import { useOrgParam } from '../security/useOrg';
import { StatusBadge } from './Maturity';
import { AllowlistEditor, EffectiveModels, RoutesEditor } from './ModelPolicyPanels';

export const CAPABILITIES: ModelCapability[] = [
  'chat',
  'tools',
  'vision',
  'json_mode',
  'embeddings',
];
export const LIFECYCLES: ModelLifecycle[] = ['preview', 'active', 'deprecated', 'retired'];

interface Prefill {
  provider_id?: string;
  model_key?: string;
  display_name?: string;
  context_window?: string;
  max_output_tokens?: string;
  capabilities?: ModelCapability[];
}

export function ModelsView() {
  const org = useOrgParam();
  const params = useSearchParams();
  const { can } = useCan(org);
  const models = useModels(org);
  const providers = useProviders(org);
  const providerName = (id: string) => providers.data?.items.find((p) => p.id === id)?.name ?? id;
  // The provider page links here with ?register=1&... to pre-fill the dialog from a discovered model.
  const initial: Prefill | null =
    params?.get('register') === '1'
      ? {
          provider_id: params.get('provider_id') ?? undefined,
          model_key: params.get('model_key') ?? undefined,
          display_name: params.get('display_name') ?? undefined,
          context_window: params.get('context_window') ?? undefined,
          max_output_tokens: params.get('max_output_tokens') ?? undefined,
          capabilities: (params.get('capabilities') ?? '')
            .split(',')
            .filter((c): c is ModelCapability => (CAPABILITIES as string[]).includes(c)),
        }
      : null;
  const [registering, setRegistering] = useState<Prefill | null | 'closed'>(initial ?? 'closed');
  const canManage = can('model:manage');

  return (
    <div className="space-y-8">
      <PageHeader
        title="Models"
        description="Registered models, who may use them, and how requests are routed."
        actions={
          canManage && (
            <button type="button" className="btn btn-primary" onClick={() => setRegistering(null)}>
              Register model
            </button>
          )
        }
      />
      <QueryGate query={models} noun="models">
        {(data) => (
          <section aria-label="Models" className="card p-0">
            {data.items.length === 0 ? (
              <EmptyState>No models registered yet.</EmptyState>
            ) : (
              <table className="w-full">
                <caption className="sr-only">Models</caption>
                <thead className="border-b border-slate-200">
                  <tr>
                    {['Model', 'Provider', 'Capabilities', 'Context', 'Lifecycle'].map((h) => (
                      <th key={h} scope="col" className="th">
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {data.items.map((m) => (
                    <tr key={m.id}>
                      <td className="td font-medium">
                        <Link
                          className="text-blue-800 underline"
                          href={`/o/${encodeURIComponent(org)}/integrate/models/${encodeURIComponent(m.id)}`}
                        >
                          {m.display_name}
                        </Link>
                        <div className="font-mono text-xs text-slate-600">{m.model_key}</div>
                      </td>
                      <td className="td">{providerName(m.provider_id)}</td>
                      <td className="td">{m.capabilities.join(', ')}</td>
                      <td className="td">{m.context_window.toLocaleString()}</td>
                      <td className="td">
                        <StatusBadge status={m.lifecycle} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>
        )}
      </QueryGate>

      {models.data && (
        <AllowlistEditor org={org} models={models.data.items} canManage={canManage} />
      )}
      {models.data && <RoutesEditor org={org} models={models.data.items} canManage={canManage} />}
      <EffectiveModels org={org} />

      {registering !== 'closed' && (
        <RegisterModelDialog
          org={org}
          providers={providers.data?.items ?? []}
          prefill={registering}
          onClose={() => setRegistering('closed')}
        />
      )}
    </div>
  );
}

function num(s: string): number | undefined {
  const t = s.trim();
  return t === '' ? undefined : Number(t);
}

function RegisterModelDialog({
  org,
  providers,
  prefill,
  onClose,
}: {
  org: string;
  providers: ModelProvider[];
  prefill: Prefill | null;
  onClose: () => void;
}) {
  const refresh = useRefreshInt(org);
  const action = useAction();
  const [providerId, setProviderId] = useState(prefill?.provider_id ?? '');
  const [modelKey, setModelKey] = useState(prefill?.model_key ?? '');
  const [displayName, setDisplayName] = useState(prefill?.display_name ?? prefill?.model_key ?? '');
  const [family, setFamily] = useState('');
  const [versionLabel, setVersionLabel] = useState('');
  const [caps, setCaps] = useState<ModelCapability[]>(prefill?.capabilities ?? ['chat']);
  const [ctx, setCtx] = useState(prefill?.context_window ?? '');
  const [maxOut, setMaxOut] = useState(prefill?.max_output_tokens ?? '');
  const [lifecycle, setLifecycle] = useState<ModelLifecycle>('preview');
  const [deprecation, setDeprecation] = useState('');
  const [rpm, setRpm] = useState('');
  const [tpm, setTpm] = useState('');
  const [conc, setConc] = useState('');
  const [errors, setErrors] = useState<Record<string, string>>({});

  const posInt = (s: string) => Number.isInteger(Number(s)) && Number(s) > 0;

  async function submit(e: FormEvent) {
    e.preventDefault();
    const next: Record<string, string> = {};
    if (providerId === '') next.provider = 'Choose a provider.';
    if (modelKey.trim() === '') next.modelKey = 'Model key is required.';
    if (displayName.trim() === '') next.displayName = 'Display name is required.';
    if (caps.length === 0) next.caps = 'Select at least one capability.';
    if (!posInt(ctx)) next.ctx = 'Enter a positive whole number.';
    if (!posInt(maxOut)) next.maxOut = 'Enter a positive whole number.';
    else if (posInt(ctx) && Number(maxOut) > Number(ctx))
      next.maxOut = 'Cannot exceed the context window.';
    for (const [k, v] of [
      ['rpm', rpm],
      ['tpm', tpm],
      ['conc', conc],
    ] as const) {
      if (v.trim() !== '' && !posInt(v)) next[k] = 'Enter a positive whole number or leave empty.';
    }
    setErrors(next);
    if (Object.keys(next).length > 0) return;
    const limits = { rpm: num(rpm), tpm: num(tpm), max_concurrency: num(conc) };
    const rate_limits = Object.fromEntries(
      Object.entries(limits).filter(([, v]) => v !== undefined),
    );
    const input: ModelInput = {
      provider_id: providerId,
      model_key: modelKey.trim(),
      display_name: displayName.trim(),
      ...(family.trim() ? { family: family.trim() } : {}),
      ...(versionLabel.trim() ? { version_label: versionLabel.trim() } : {}),
      capabilities: caps,
      context_window: Number(ctx),
      max_output_tokens: Number(maxOut),
      lifecycle,
      ...(deprecation ? { deprecation_date: deprecation } : {}),
      ...(Object.keys(rate_limits).length > 0 ? { rate_limits } : {}),
    };
    const res = await action.run(() => endpoints.createModel(org, input));
    if (res.ok) {
      await refresh('models');
      onClose();
    }
  }

  return (
    <Modal title="Register model" onClose={onClose} busy={action.busy} wide>
      <form onSubmit={(e) => void submit(e)} className="space-y-4" noValidate>
        <div>
          <SelectField
            label="Provider"
            value={providerId}
            onChange={setProviderId}
            options={[
              { value: '', label: 'Select a provider...' },
              ...providers.map((p) => ({ value: p.id, label: p.name })),
            ]}
          />
          {errors.provider && <p className="mt-1 text-sm text-red-700">{errors.provider}</p>}
        </div>
        <div className="grid gap-4 sm:grid-cols-2">
          <TextField
            label="Model key"
            value={modelKey}
            onChange={setModelKey}
            error={errors.modelKey}
          />
          <TextField
            label="Display name"
            value={displayName}
            onChange={setDisplayName}
            error={errors.displayName}
          />
          <TextField label="Family" value={family} onChange={setFamily} />
          <TextField label="Version label" value={versionLabel} onChange={setVersionLabel} />
        </div>
        <fieldset>
          <legend className="label">Capabilities</legend>
          <div className="flex flex-wrap gap-4">
            {CAPABILITIES.map((c) => (
              <label key={c} className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={caps.includes(c)}
                  onChange={(e) =>
                    setCaps((cur) => (e.target.checked ? [...cur, c] : cur.filter((x) => x !== c)))
                  }
                />
                {c}
              </label>
            ))}
          </div>
          {errors.caps && <p className="mt-1 text-sm text-red-700">{errors.caps}</p>}
        </fieldset>
        <div className="grid gap-4 sm:grid-cols-2">
          <TextField
            label="Context window (tokens)"
            type="number"
            value={ctx}
            onChange={setCtx}
            error={errors.ctx}
          />
          <TextField
            label="Max output tokens"
            type="number"
            value={maxOut}
            onChange={setMaxOut}
            error={errors.maxOut}
          />
          <SelectField
            label="Lifecycle"
            value={lifecycle}
            onChange={(v) => setLifecycle(v as ModelLifecycle)}
            options={LIFECYCLES.filter((l) => l !== 'retired').map((l) => ({ value: l, label: l }))}
          />
          <TextField
            label="Deprecation date"
            type="date"
            value={deprecation}
            onChange={setDeprecation}
          />
          <TextField
            label="Requests per minute"
            type="number"
            value={rpm}
            onChange={setRpm}
            error={errors.rpm}
          />
          <TextField
            label="Tokens per minute"
            type="number"
            value={tpm}
            onChange={setTpm}
            error={errors.tpm}
          />
          <TextField
            label="Max concurrency"
            type="number"
            value={conc}
            onChange={setConc}
            error={errors.conc}
          />
        </div>
        <ActionError error={action.error} title="Could not register the model" />
        <ModalActions onCancel={onClose} busy={action.busy} submitLabel="Register model" />
      </form>
    </Modal>
  );
}
