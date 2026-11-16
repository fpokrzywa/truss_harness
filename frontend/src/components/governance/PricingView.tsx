'use client';

import { useQueryClient } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';
import { endpoints } from '@/lib/api/endpoints';
import { ApiError } from '@/lib/api/errors';
import type {
  RateCard,
  RateCardVersion,
  RateCardVersionInput,
  RateUnit,
} from '@/lib/api/governance-types';
import { useCan, useSession } from '@/lib/hooks';
import { govKey, useRateCards, useRateCardVersions } from '@/lib/governance-hooks';
import {
  ActionError,
  Badge,
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

const UNIT_LABEL: Record<RateUnit, string> = {
  per_1m_tokens: 'per 1M tokens',
  per_call: 'per call',
  per_second: 'per second',
};

/** Effective dates are UTC instants; show the UTC day so the date matches what was entered. */
const utcDay = (iso: string): string => {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toISOString().slice(0, 10);
};

export const isSample = (source: string): boolean => /sample/i.test(source);

/** Tenant rate cards under Govern → Pricing. */
export function PricingView() {
  const org = useOrgParam();
  const { can } = useCan(org);
  return <Registry org={org} platform={false} manage={can('pricing:manage')} />;
}

/** Global rate cards (`/platform/rate-cards`); platform admins only. */
export function PlatformRateCardsView() {
  const session = useSession();
  const admin = session.data?.user.is_platform_admin === true;
  if (session.isPending)
    return (
      <p role="status" className="text-sm text-slate-600">
        Loading...
      </p>
    );
  if (!admin)
    return (
      <p role="alert" className="text-sm text-slate-700">
        Global rate cards are available to platform administrators only.
      </p>
    );
  return <Registry org="" platform manage />;
}

function Registry({ org, platform, manage }: { org: string; platform: boolean; manage: boolean }) {
  const cards = useRateCards(org, platform);
  const [selected, setSelected] = useState<string>('');
  const [creating, setCreating] = useState(false);
  const base = platform ? '/api/v1/platform' : `/api/v1/orgs/${encodeURIComponent(org)}`;

  return (
    <div className="space-y-6">
      <PageHeader
        title={platform ? 'Global rate cards' : 'Pricing'}
        description="Rate cards price token usage. Versions are immutable once created; change a rate by adding a version with a new effective range. Calculated costs are never an invoice."
        actions={
          manage ? (
            <button type="button" className="btn btn-primary" onClick={() => setCreating(true)}>
              Create rate card
            </button>
          ) : undefined
        }
      />
      <QueryGate query={cards} noun="rate cards">
        {(items) =>
          items.length === 0 ? (
            <section className="card p-0">
              <EmptyState>
                No rate cards yet. Without a rate, costs are recorded as unmeasured.
              </EmptyState>
            </section>
          ) : (
            <section aria-label="Rate cards" className="card overflow-x-auto p-0">
              <table className="w-full">
                <caption className="sr-only">Rate cards</caption>
                <thead className="border-b border-slate-200">
                  <tr>
                    {['Name', 'Provider', 'Model', ''].map((h, i) => (
                      <th key={`${h}-${i}`} scope="col" className="th">
                        {h || <span className="sr-only">Actions</span>}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {items.map((c) => (
                    <tr key={c.id}>
                      <th scope="row" className="td text-left font-normal">
                        {c.name}
                      </th>
                      <td className="td font-mono text-xs">{c.provider_key}</td>
                      <td className="td font-mono text-xs">{c.model_key}</td>
                      <td className="td">
                        <button
                          type="button"
                          className="btn"
                          aria-pressed={selected === c.id}
                          aria-label={`Versions of ${c.name}`}
                          onClick={() => setSelected(selected === c.id ? '' : c.id)}
                        >
                          Versions
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>
          )
        }
      </QueryGate>
      {selected && (
        <Versions
          key={selected}
          org={org}
          base={base}
          card={cards.data?.find((c) => c.id === selected)}
          manage={manage}
        />
      )}
      {creating && <CardDialog org={org} platform={platform} onClose={() => setCreating(false)} />}
    </div>
  );
}

function Versions({
  org,
  base,
  card,
  manage,
}: {
  org: string;
  base: string;
  card: RateCard | undefined;
  manage: boolean;
}) {
  const versions = useRateCardVersions(org, base, card?.id ?? '');
  const [adding, setAdding] = useState(false);
  if (!card) return null;
  return (
    <section aria-label={`Versions of ${card.name}`} className="card space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-sm font-semibold">
          Versions of {card.name} <span className="font-mono text-xs">({card.model_key})</span>
        </h2>
        {manage && (
          <button type="button" className="btn" onClick={() => setAdding(true)}>
            Add version
          </button>
        )}
      </div>
      <p className="text-xs text-slate-600">
        Versions are immutable. There is no edit or delete; add a new version to change a rate.
      </p>
      <QueryGate query={versions} noun="versions">
        {(items) =>
          items.length === 0 ? (
            <EmptyState>No versions yet.</EmptyState>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full">
                <caption className="sr-only">Rate card versions</caption>
                <thead>
                  <tr>
                    {[
                      'Version',
                      'Effective',
                      'Unit',
                      'Input',
                      'Output',
                      'Cached read',
                      'Cached write',
                      'Source',
                    ].map((h) => (
                      <th key={h} scope="col" className="th">
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {items.map((v) => (
                    <VersionRow key={v.id} v={v} />
                  ))}
                </tbody>
              </table>
            </div>
          )
        }
      </QueryGate>
      {adding && (
        <VersionDialog org={org} base={base} card={card} onClose={() => setAdding(false)} />
      )}
    </section>
  );
}

function VersionRow({ v }: { v: RateCardVersion }) {
  const rate = (n: number | null | undefined) => (n == null ? '-' : `${n} ${v.currency}`);
  return (
    <tr>
      <th scope="row" className="td text-left font-normal">
        {v.version_no != null ? `v${v.version_no}` : `rev ${v.revision}`}
      </th>
      <td className="td">
        {utcDay(v.effective_from)} to {v.effective_to ? utcDay(v.effective_to) : 'open'}
      </td>
      <td className="td">{UNIT_LABEL[v.unit] ?? v.unit}</td>
      <td className="td font-mono">{rate(v.input_rate)}</td>
      <td className="td font-mono">{rate(v.output_rate)}</td>
      <td className="td font-mono">{rate(v.cached_read_rate)}</td>
      <td className="td font-mono">{rate(v.cached_write_rate)}</td>
      <td className="td">
        {isSample(v.source) && <Badge tone="warn">SAMPLE: not vendor pricing</Badge>}
        <span className="block text-xs text-slate-600">
          {v.source} (revision {v.revision})
        </span>
      </td>
    </tr>
  );
}

function CardDialog({
  org,
  platform,
  onClose,
}: {
  org: string;
  platform: boolean;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const act = useAction();
  const [name, setName] = useState('');
  const [provider, setProvider] = useState('');
  const [model, setModel] = useState('');
  const valid = !!name.trim() && !!provider.trim() && !!model.trim();

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!valid) return;
    const input = { name: name.trim(), provider_key: provider.trim(), model_key: model.trim() };
    const r = await act.run(() =>
      platform ? endpoints.createPlatformRateCard(input) : endpoints.createRateCard(org, input),
    );
    if (r.ok) {
      await qc.invalidateQueries({ queryKey: govKey(org, 'rate-cards') });
      onClose();
    }
  }
  return (
    <Modal title="Create rate card" onClose={onClose} busy={act.busy}>
      <form onSubmit={(e) => void submit(e)} className="space-y-3" noValidate>
        <TextField label="Name" value={name} onChange={setName} />
        <TextField label="Provider key" value={provider} onChange={setProvider} monospace />
        <TextField label="Model key" value={model} onChange={setModel} monospace />
        <ActionError error={act.error} title="Could not create the rate card" />
        <ModalActions
          onCancel={onClose}
          submitLabel="Create rate card"
          busy={act.busy}
          submitDisabled={!valid}
        />
      </form>
    </Modal>
  );
}

const optNum = (s: string): number | undefined | null => {
  if (s.trim() === '') return undefined;
  const n = Number(s);
  return Number.isFinite(n) && n >= 0 ? n : null;
};

function VersionDialog({
  org,
  base,
  card,
  onClose,
}: {
  org: string;
  base: string;
  card: RateCard;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const act = useAction();
  const [from, setFrom] = useState('');
  const [to, setTo] = useState('');
  const [currency, setCurrency] = useState('USD');
  const [unit, setUnit] = useState<RateUnit>('per_1m_tokens');
  const [input, setInput] = useState('');
  const [output, setOutput] = useState('');
  const [cacheRead, setCacheRead] = useState('');
  const [cacheWrite, setCacheWrite] = useState('');
  const [source, setSource] = useState('');
  const [revision, setRevision] = useState('1');

  const inN = optNum(input);
  const outN = optNum(output);
  const crN = optNum(cacheRead);
  const cwN = optNum(cacheWrite);
  const rateError = (s: string, n: number | null | undefined, required: boolean) =>
    n === null || (required && s.trim() === '') ? 'Enter a rate of 0 or more.' : null;
  const rangeError = from && to && to <= from ? 'The end must be after the start.' : null;
  const valid =
    !!from &&
    !rangeError &&
    typeof inN === 'number' &&
    typeof outN === 'number' &&
    crN !== null &&
    cwN !== null &&
    !!source.trim() &&
    !!revision.trim() &&
    /^[A-Za-z]{3}$/.test(currency.trim());
  const overlap = act.error instanceof ApiError && act.error.status === 409;
  // The server also answers 409 when a new version would close the open-ended one from a start in
  // the past (history is never rewritten); a date-only start of today is midnight UTC, i.e. past.
  const pastStart = overlap && /past/i.test((act.error as ApiError).detail ?? '');

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!valid || typeof inN !== 'number' || typeof outN !== 'number') return;
    const body: RateCardVersionInput = {
      effective_from: new Date(`${from}T00:00:00Z`).toISOString(),
      ...(to ? { effective_to: new Date(`${to}T00:00:00Z`).toISOString() } : {}),
      currency: currency.trim().toUpperCase(),
      unit,
      input_rate: inN,
      output_rate: outN,
      ...(typeof crN === 'number' ? { cached_read_rate: crN } : {}),
      ...(typeof cwN === 'number' ? { cached_write_rate: cwN } : {}),
      source: source.trim(),
      revision: revision.trim(),
    };
    const r = await act.run(() => endpoints.createRateCardVersion(base, card.id, body));
    if (r.ok) {
      await qc.invalidateQueries({ queryKey: govKey(org, 'rate-cards', 'versions') });
      onClose();
    }
  }

  return (
    <Modal title={`Add version to ${card.name}`} onClose={onClose} busy={act.busy} wide>
      <form onSubmit={(e) => void submit(e)} className="space-y-3" noValidate>
        <div className="grid gap-3 sm:grid-cols-2">
          <TextField label="Effective from" type="date" value={from} onChange={setFrom} />
          <TextField
            label="Effective to (optional)"
            type="date"
            value={to}
            onChange={setTo}
            error={rangeError}
          />
          <TextField label="Currency" value={currency} onChange={setCurrency} maxLength={3} />
          <SelectField
            label="Unit"
            value={unit}
            onChange={(v) => setUnit(v as RateUnit)}
            options={(Object.keys(UNIT_LABEL) as RateUnit[]).map((u) => ({
              value: u,
              label: UNIT_LABEL[u],
            }))}
          />
          <TextField
            label="Input rate"
            type="number"
            value={input}
            onChange={setInput}
            error={rateError(input, inN, true)}
          />
          <TextField
            label="Output rate"
            type="number"
            value={output}
            onChange={setOutput}
            error={rateError(output, outN, true)}
          />
          <TextField
            label="Cached read rate (optional)"
            type="number"
            value={cacheRead}
            onChange={setCacheRead}
            error={rateError(cacheRead, crN, false)}
          />
          <TextField
            label="Cached write rate (optional)"
            type="number"
            value={cacheWrite}
            onChange={setCacheWrite}
            error={rateError(cacheWrite, cwN, false)}
          />
          <TextField
            label="Source"
            value={source}
            onChange={setSource}
            help='Where the rate comes from, e.g. "Vendor price list 2026-10". Use "SAMPLE" for placeholder data.'
          />
          <TextField label="Revision" value={revision} onChange={setRevision} />
        </div>
        {overlap ? (
          <p
            role="alert"
            className="rounded-md border border-red-300 bg-red-50 p-3 text-sm text-red-900"
          >
            {pastStart
              ? 'This version would close the current open-ended version from a start in the past (409). Past prices are never rewritten; choose a start date after today.'
              : 'This effective range overlaps an existing version of this rate card (409). Versions cannot be changed; choose a range that does not overlap an existing version.'}
            {act.error instanceof ApiError && act.error.detail
              ? ` Server: ${act.error.detail}`
              : ''}
          </p>
        ) : (
          <ActionError error={act.error} title="Could not create the version" />
        )}
        <ModalActions
          onCancel={onClose}
          submitLabel="Create version"
          busy={act.busy}
          submitDisabled={!valid}
        />
      </form>
    </Modal>
  );
}
