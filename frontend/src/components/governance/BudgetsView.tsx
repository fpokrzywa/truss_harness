'use client';

import { useQueryClient } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';
import { endpoints } from '@/lib/api/endpoints';
import type {
  Budget,
  BudgetInput,
  BudgetMetric,
  BudgetReportedMetric,
  BudgetPeriod,
  Enforcement,
  OnExceed,
  Quota,
  QuotaInput,
  QuotaMetric,
} from '@/lib/api/governance-types';
import { useCan } from '@/lib/hooks';
import { govKey, useBudgets, useBudgetStatus, useQuotas } from '@/lib/governance-hooks';
import {
  crossed,
  formatCount,
  formatMoney,
  formatPercent,
  parseThresholds,
  thresholdsText,
} from '@/lib/governance';
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
import { useOrgId } from '../admin/ScopePicker';
import { useOrgParam } from '../security/useOrg';
import { GovScopePicker, SCOPE_LABEL } from './GovScopePicker';

const scopeText = (s: { type: keyof typeof SCOPE_LABEL; id: string }) =>
  s.type === 'tenant' ? 'Organization' : `${s.type} ${s.id}`;

/** Utilization with threshold markers. Crossed thresholds are marked in text, not only by color. */
export function UtilizationBar({
  utilization,
  thresholds,
  label,
}: {
  utilization: number;
  thresholds: readonly number[];
  label: string;
}) {
  const pct = Math.max(0, utilization) * 100;
  const scale = Math.max(100, pct, ...thresholds.map((t) => t * 100));
  const fill = Math.min(100, (pct / scale) * 100);
  const over = utilization >= 1;
  return (
    <div className="min-w-48 space-y-1">
      <div
        role="meter"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={Math.round(scale)}
        aria-valuenow={Math.round(pct)}
        aria-valuetext={`${formatPercent(utilization)} used`}
        className="relative h-3 rounded bg-slate-200"
      >
        <div
          className={`h-3 rounded ${over ? 'bg-shi-orange' : 'bg-blue-700'}`}
          style={{ width: `${fill}%` }}
        />
        {thresholds.map((t) => (
          <span
            key={t}
            aria-hidden="true"
            data-testid={`threshold-${Math.round(t * 100)}`}
            className="absolute top-[-2px] h-[16px] w-[2px] bg-slate-900"
            style={{ left: `${Math.min(100, ((t * 100) / scale) * 100)}%` }}
          />
        ))}
      </div>
      <p className="text-xs text-slate-700">
        {formatPercent(utilization)} used
        {thresholds.length > 0 && (
          <>
            {'; thresholds '}
            {thresholds.map((t, i) => (
              <span key={t} className={crossed(utilization, t) ? 'font-semibold text-red-800' : ''}>
                {i > 0 ? ', ' : ''}
                {formatPercent(t)}
                {crossed(utilization, t) ? ' (reached)' : ''}
              </span>
            ))}
          </>
        )}
      </p>
    </div>
  );
}

function BudgetStatusCell({ org, budget }: { org: string; budget: Budget }) {
  const status = useBudgetStatus(org, budget.id);
  if (status.isPending) return <span className="text-xs text-slate-600">Loading status...</span>;
  if (status.isError) return <span className="text-xs text-slate-600">Status unavailable</span>;
  const s = status.data;
  const fmt = (n: number | null) =>
    n === null
      ? 'n/a'
      : budget.metric === 'cost'
        ? formatMoney(n, budget.currency)
        : formatCount(n);
  return (
    <div className="space-y-1">
      <UtilizationBar
        utilization={s.utilization}
        thresholds={budget.alert_thresholds}
        label={`Utilization of ${scopeText(budget.scope)} ${budget.period} ${budget.metric} budget`}
      />
      <p className="text-xs text-slate-600">
        settled {fmt(s.settled)}, reserved {fmt(s.reserved)}, remaining {fmt(s.remaining)}
        {s.alerts_fired.length > 0 &&
          `; alerts fired: ${s.alerts_fired.map((a) => (typeof a === 'number' ? formatPercent(a) : a)).join(', ')}`}
      </p>
      {s.note && <p className="text-xs text-slate-600">{s.note}</p>}
    </div>
  );
}

export function BudgetsView() {
  const org = useOrgParam();
  const { can } = useCan(org);
  const manage = can('budget:manage');
  const budgets = useBudgets(org);
  const quotas = useQuotas(org);
  const [dialog, setDialog] = useState<{ budget?: Budget } | null>(null);
  const [qDialog, setQDialog] = useState<{ quota?: Quota } | null>(null);

  return (
    <div className="space-y-8">
      <PageHeader
        title="Budgets and quotas"
        description="Budgets are checked before work starts and reconciled after. Cost budgets count known cost (never an invoice); token budgets count tokens."
        actions={
          manage ? (
            <button type="button" className="btn btn-primary" onClick={() => setDialog({})}>
              Create budget
            </button>
          ) : undefined
        }
      />
      <QueryGate query={budgets} noun="budgets">
        {(items) =>
          items.length === 0 ? (
            <section className="card p-0">
              <EmptyState>No budgets defined.</EmptyState>
            </section>
          ) : (
            <section aria-label="Budgets" className="card overflow-x-auto p-0">
              <table className="w-full">
                <caption className="sr-only">Budgets</caption>
                <thead className="border-b border-slate-200">
                  <tr>
                    {[
                      'Scope',
                      'Metric',
                      'Period',
                      'Limit',
                      'Enforcement',
                      'On exceed',
                      'Utilization',
                      '',
                    ].map((h, i) => (
                      <th key={`${h}-${i}`} scope="col" className="th">
                        {h || <span className="sr-only">Actions</span>}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {items.map((b) => (
                    <tr key={b.id}>
                      <th scope="row" className="td text-left font-normal">
                        {scopeText(b.scope)}
                        {b.status !== 'active' && (
                          <span className="ml-2">
                            <Badge tone="warn">{b.status}</Badge>
                          </span>
                        )}
                      </th>
                      <td className="td">{b.metric}</td>
                      <td className="td">{b.period}</td>
                      <td className="td font-mono">
                        {b.amount === null
                          ? 'No limit'
                          : b.metric === 'cost'
                            ? formatMoney(b.amount, b.currency)
                            : `${formatCount(b.amount)} ${b.metric === 'runs' ? 'runs' : 'tokens'}`}
                      </td>
                      <td className="td">{b.enforcement}</td>
                      <td className="td">
                        {b.on_exceed.replaceAll('_', ' ')}
                        {b.on_exceed_effective && b.on_exceed_effective !== b.on_exceed && (
                          <span className="block text-xs text-amber-800">
                            {effectiveText(b.on_exceed_effective, b.on_exceed)}
                          </span>
                        )}
                      </td>
                      <td className="td">
                        <BudgetStatusCell org={org} budget={b} />
                      </td>
                      <td className="td">
                        {manage && (
                          <button
                            type="button"
                            className="btn"
                            aria-label={`Edit budget ${scopeText(b.scope)} ${b.period}`}
                            onClick={() => setDialog({ budget: b })}
                          >
                            Edit
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>
          )
        }
      </QueryGate>

      <section aria-labelledby="quotas-h" className="space-y-3">
        <div className="flex items-center justify-between">
          <h2 id="quotas-h" className="text-lg font-semibold">
            Quotas
          </h2>
          {manage && (
            <button type="button" className="btn" onClick={() => setQDialog({})}>
              Create quota
            </button>
          )}
        </div>
        <QueryGate query={quotas} noun="quotas">
          {(items) =>
            items.length === 0 ? (
              <section className="card p-0">
                <EmptyState>No quotas defined.</EmptyState>
              </section>
            ) : (
              <section aria-label="Quotas" className="card overflow-x-auto p-0">
                <table className="w-full">
                  <caption className="sr-only">Quotas</caption>
                  <thead className="border-b border-slate-200">
                    <tr>
                      {['Scope', 'Metric', 'Limit', ''].map((h, i) => (
                        <th key={`${h}-${i}`} scope="col" className="th">
                          {h || <span className="sr-only">Actions</span>}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100">
                    {items.map((q) => (
                      <tr key={q.id}>
                        <th scope="row" className="td text-left font-normal">
                          {scopeText(q.scope)}
                        </th>
                        <td className="td">{q.metric.replaceAll('_', ' ')}</td>
                        <td className="td">{formatCount(q.limit)}</td>
                        <td className="td">
                          {manage && (
                            <button
                              type="button"
                              className="btn"
                              aria-label={`Edit quota ${scopeText(q.scope)} ${q.metric}`}
                              onClick={() => setQDialog({ quota: q })}
                            >
                              Edit
                            </button>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </section>
            )
          }
        </QueryGate>
      </section>

      {dialog && <BudgetDialog org={org} budget={dialog.budget} onClose={() => setDialog(null)} />}
      {qDialog && <QuotaDialog org={org} quota={qDialog.quota} onClose={() => setQDialog(null)} />}
    </div>
  );
}

function BudgetDialog({
  org,
  budget,
  onClose,
}: {
  org: string;
  budget?: Budget;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const orgId = useOrgId(org) ?? '';
  const act = useAction();
  const [scope, setScope] = useState(budget?.scope ?? { type: 'tenant' as const, id: orgId });
  const [metric, setMetric] = useState<BudgetReportedMetric>(budget?.metric ?? 'cost');
  const [period, setPeriod] = useState<BudgetPeriod>(budget?.period ?? 'monthly');
  const [name, setName] = useState(budget?.name ?? '');
  const [amount, setAmount] = useState(budget?.amount != null ? String(budget.amount) : '');
  const [currency, setCurrency] = useState(budget?.currency ?? 'USD');
  const [enforcement, setEnforcement] = useState<Enforcement>(budget?.enforcement ?? 'hard');
  const [onExceed, setOnExceed] = useState<OnExceed>(budget?.on_exceed ?? 'deny');
  const [thresholds, setThresholds] = useState(
    thresholdsText(budget?.alert_thresholds ?? [0.5, 0.8, 1]),
  );
  const [status, setStatus] = useState(budget?.status ?? 'active');

  const parsed = parseThresholds(thresholds);
  const amountN = Number(amount);
  const scopeId = scope.type === 'tenant' ? orgId : scope.id;
  const amountError =
    amount !== '' && (!Number.isFinite(amountN) || amountN <= 0)
      ? 'Enter a positive number.'
      : null;
  const thresholdError =
    parsed === null ? 'Enter percentages separated by commas, e.g. 50, 80, 100.' : null;
  // Currency applies to cost budgets only; token budgets carry none.
  const valid =
    amount !== '' &&
    !amountError &&
    !thresholdError &&
    !!scopeId &&
    (metric !== 'cost' || /^[A-Za-z]{3}$/.test(currency.trim()));

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!valid || parsed === null) return;
    const input: BudgetInput = {
      ...(name.trim() ? { name: name.trim() } : {}),
      scope: { type: scope.type, id: scopeId },
      period,
      amount: amountN,
      ...(metric === 'cost' ? { currency: currency.trim().toUpperCase() } : {}),
      // A legacy run-count budget is only ever edited (PATCH never sends the metric).
      metric: metric === 'cost' ? 'cost' : 'tokens',
      enforcement,
      on_exceed: onExceed,
      alert_thresholds: parsed,
      status,
    };
    const r = await act.run(() =>
      budget ? endpoints.updateBudget(org, budget, input) : endpoints.createBudget(org, input),
    );
    if (r.ok) {
      await qc.invalidateQueries({ queryKey: govKey(org, 'budgets') });
      onClose();
    }
  }

  return (
    <Modal title={budget ? 'Edit budget' : 'Create budget'} onClose={onClose} busy={act.busy} wide>
      <form onSubmit={(e) => void submit(e)} className="space-y-3" noValidate>
        <TextField label="Name" value={name} onChange={setName} />
        {budget ? (
          <p className="text-sm text-slate-700">
            Scope: {scopeText(budget.scope)}. Scope and metric cannot be changed; delete and
            recreate the budget to change what it measures.
          </p>
        ) : (
          <GovScopePicker org={org} value={{ type: scope.type, id: scopeId }} onChange={setScope} />
        )}
        <div className="grid gap-3 sm:grid-cols-2">
          <SelectField
            label="Metric"
            value={metric}
            disabled={!!budget}
            onChange={(v) => setMetric(v as BudgetMetric)}
            options={[
              { value: 'cost', label: 'Cost' },
              { value: 'tokens', label: 'Tokens' },
            ]}
          />
          <SelectField
            label="Period"
            value={period}
            onChange={(v) => setPeriod(v as BudgetPeriod)}
            options={[
              { value: 'daily', label: 'Daily' },
              { value: 'monthly', label: 'Monthly' },
              { value: 'total', label: 'Total' },
            ]}
          />
          <TextField
            label={
              metric === 'cost' ? 'Amount' : metric === 'runs' ? 'Amount (runs)' : 'Amount (tokens)'
            }
            type="number"
            value={amount}
            onChange={setAmount}
            error={amountError}
          />
          {metric === 'cost' && (
            <TextField label="Currency" value={currency} onChange={setCurrency} maxLength={3} />
          )}
          <SelectField
            label="Enforcement"
            value={enforcement}
            onChange={(v) => setEnforcement(v as Enforcement)}
            options={[
              { value: 'hard', label: 'Hard (blocks)' },
              { value: 'soft', label: 'Soft (alerts)' },
            ]}
          />
          <SelectField
            label="On exceed"
            value={onExceed}
            onChange={(v) => setOnExceed(v as OnExceed)}
            options={[
              { value: 'deny', label: 'Deny' },
              { value: 'queue_until_reset', label: 'Queue until reset' },
              { value: 'require_approval', label: 'Require approval' },
            ]}
          />
        </div>
        <TextField
          label="Alert thresholds (percent)"
          value={thresholds}
          onChange={setThresholds}
          error={thresholdError}
          help="Alerts fire when utilization reaches each percentage."
        />
        {budget && (
          <SelectField
            label="Status"
            value={status}
            onChange={setStatus}
            options={[
              { value: 'active', label: 'Active' },
              { value: 'disabled', label: 'Disabled' },
            ]}
          />
        )}
        <ActionError error={act.error} title="Could not save the budget" />
        <ModalActions
          onCancel={onClose}
          submitLabel={budget ? 'Save budget' : 'Create budget'}
          busy={act.busy}
          submitDisabled={!valid}
        />
      </form>
    </Modal>
  );
}

function QuotaDialog({ org, quota, onClose }: { org: string; quota?: Quota; onClose: () => void }) {
  const qc = useQueryClient();
  const orgId = useOrgId(org) ?? '';
  const act = useAction();
  const [scope, setScope] = useState(quota?.scope ?? { type: 'tenant' as const, id: orgId });
  const [metric, setMetric] = useState<QuotaMetric>(quota?.metric ?? 'runs_per_day');
  const [limit, setLimit] = useState(quota ? String(quota.limit) : '');
  const scopeId = scope.type === 'tenant' ? orgId : scope.id;
  const limitN = Number(limit);
  const limitError =
    limit !== '' && (!Number.isInteger(limitN) || limitN < 0)
      ? 'Enter a whole number, 0 or more.'
      : null;
  const valid = limit !== '' && !limitError && !!scopeId;

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!valid) return;
    const input: QuotaInput = { scope: { type: scope.type, id: scopeId }, metric, limit: limitN };
    const r = await act.run(() =>
      quota ? endpoints.updateQuota(org, quota, input) : endpoints.createQuota(org, input),
    );
    if (r.ok) {
      await qc.invalidateQueries({ queryKey: govKey(org, 'quotas') });
      onClose();
    }
  }

  return (
    <Modal title={quota ? 'Edit quota' : 'Create quota'} onClose={onClose} busy={act.busy}>
      <form onSubmit={(e) => void submit(e)} className="space-y-3" noValidate>
        {quota ? (
          <p className="text-sm text-slate-700">
            Scope: {scopeText(quota.scope)}. Scope and metric cannot be changed.
          </p>
        ) : (
          <GovScopePicker org={org} value={{ type: scope.type, id: scopeId }} onChange={setScope} />
        )}
        <SelectField
          label="Quota metric"
          value={metric}
          disabled={!!quota}
          onChange={(v) => setMetric(v as QuotaMetric)}
          options={[
            { value: 'runs_per_day', label: 'Runs per day' },
            { value: 'tokens_per_day', label: 'Tokens per day' },
            { value: 'concurrent_runs', label: 'Concurrent runs' },
          ]}
        />
        <TextField
          label="Limit"
          type="number"
          value={limit}
          onChange={setLimit}
          error={limitError}
        />
        <ActionError error={act.error} title="Could not save the quota" />
        <ModalActions
          onCancel={onClose}
          submitLabel={quota ? 'Save quota' : 'Create quota'}
          busy={act.busy}
          submitDisabled={!valid}
        />
      </form>
    </Modal>
  );
}

/** What the platform actually does on exceed, when it differs from the configured `on_exceed`. */
export function effectiveText(effective: string, configured: string): string {
  const cfg = configured.replaceAll('_', ' ');
  if (effective === 'alert_only')
    return `Effective: alert only (soft budget: never blocks work; ${cfg} is not applied)`;
  return `Effective: ${effective.replaceAll('_', ' ')} (${cfg} is not applied yet)`;
}
