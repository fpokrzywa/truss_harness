'use client';

import { useId, useState, type ReactNode } from 'react';
import type { CostKind, UsageGroup } from '@/lib/api/governance-types';
import {
  compactNumber,
  costKindLabel,
  formatCount,
  formatMoney,
  niceTicks,
  type DayPoint,
} from '@/lib/governance';

/** Series slots 1-3 of the validated reference palette (see globals.css). One fixed color per entity. */
const KIND_COLOR: Partial<Record<CostKind, string>> = {
  provider_reported: 'var(--series-1)',
  calculated: 'var(--series-2)',
  estimated_infra: 'var(--series-3)',
};
const STACK_KINDS = ['provider_reported', 'calculated', 'estimated_infra'] as const;

function Legend({ items }: { items: { color: string; label: string }[] }) {
  return (
    <ul aria-label="Legend" className="flex flex-wrap gap-x-4 gap-y-1 text-xs">
      {items.map((i) => (
        <li key={i.label} className="flex items-center gap-1.5">
          <svg width="10" height="10" aria-hidden="true">
            <rect width="10" height="10" rx="2" fill={i.color} />
          </svg>
          <span style={{ color: 'var(--text-secondary)' }}>{i.label}</span>
        </li>
      ))}
    </ul>
  );
}

/** A chart with a text alternative and a data-table view reachable by keyboard. */
export function ChartFrame({
  title,
  summary,
  chart,
  table,
}: {
  title: string;
  summary: string;
  chart: ReactNode;
  table: ReactNode;
}) {
  const [asTable, setAsTable] = useState(false);
  const id = useId();
  return (
    <figure aria-labelledby={`${id}-t`} className="card space-y-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <figcaption id={`${id}-t`} className="text-sm font-semibold">
          {title}
        </figcaption>
        <button
          type="button"
          className="btn"
          aria-pressed={asTable}
          onClick={() => setAsTable((v) => !v)}
        >
          {asTable ? 'Show chart' : 'Show data table'}
        </button>
      </div>
      <p className="text-xs text-slate-600">{summary}</p>
      {asTable ? table : chart}
    </figure>
  );
}

const W = 640;

/** Horizontal stacked bars of known cost per group, one segment per cost kind. Unmeasured has no amount. */
export function StackedCostBars({
  groups,
  currency,
  ratesVersions,
  max = 10,
}: {
  groups: UsageGroup[];
  currency?: string | null;
  ratesVersions?: (string | number)[];
  max?: number;
}) {
  const shown = [...groups].sort((a, b) => b.cost.total_known - a.cost.total_known).slice(0, max);
  const top = Math.max(...shown.map((g) => g.cost.total_known), 0);
  const ticks = niceTicks(top);
  const axisMax = ticks[ticks.length - 1] ?? 1;
  const left = 150;
  const right = 16;
  const rowH = 30;
  const barH = 20;
  const gap = 2;
  const plotW = W - left - right;
  const H = shown.length * rowH + 52;
  const x = (v: number) => left + (v / axisMax) * plotW;
  const labels = STACK_KINDS.map((k) => ({
    color: KIND_COLOR[k] ?? 'var(--series-1)',
    label: costKindLabel(k, { versions: ratesVersions }),
  }));
  const unit = currency ? ` (${currency})` : '';

  return (
    <div className="viz-root space-y-2 rounded-md p-3">
      <Legend items={labels} />
      <svg
        viewBox={`0 0 ${W} ${H}`}
        role="img"
        aria-label={`Stacked bars of known cost by group and cost kind, top ${shown.length} groups`}
        className="w-full"
      >
        {ticks.map((t) => (
          <g key={t}>
            <line x1={x(t)} x2={x(t)} y1={4} y2={shown.length * rowH + 4} stroke="var(--grid)" />
            <text
              x={x(t)}
              y={shown.length * rowH + 20}
              textAnchor="middle"
              fontSize="11"
              fill="var(--text-secondary)"
            >
              {compactNumber(t)}
            </text>
          </g>
        ))}
        <text
          x={left + plotW / 2}
          y={H - 4}
          textAnchor="middle"
          fontSize="11"
          fill="var(--text-secondary)"
        >
          {`Known cost${unit}`}
        </text>
        {shown.map((g, i) => {
          const y = 4 + i * rowH + (rowH - barH) / 2;
          let acc = 0;
          return (
            <g key={g.key}>
              <text
                x={left - 8}
                y={y + barH / 2 + 4}
                textAnchor="end"
                fontSize="11"
                fill="var(--text-primary)"
              >
                {(g.label ?? g.key).slice(0, 22)}
              </text>
              {STACK_KINDS.map((k) => {
                const v = g.cost[k];
                if (v <= 0) return null;
                const x0 = x(acc);
                const w = Math.max(0, x(acc + v) - x0 - gap);
                acc += v;
                return (
                  <rect key={k} x={x0} y={y} width={w} height={barH} rx={4} fill={KIND_COLOR[k]}>
                    <title>{`${g.label ?? g.key}: ${costKindLabel(k, { versions: ratesVersions })} ${formatMoney(v, currency)}`}</title>
                  </rect>
                );
              })}
            </g>
          );
        })}
      </svg>
      <p className="text-xs" style={{ color: 'var(--text-secondary)' }}>
        Unmeasured events carry no amount and are not drawn; see the table for their counts.
      </p>
    </div>
  );
}

/** Daily input/output tokens as two lines. X is the UTC day, Y is tokens. */
export function TokenLines({ points }: { points: DayPoint[] }) {
  const top = Math.max(...points.flatMap((p) => [p.input_tokens, p.output_tokens]), 0);
  const ticks = niceTicks(top);
  const axisMax = ticks[ticks.length - 1] ?? 1;
  const left = 56;
  const right = 16;
  const topPad = 8;
  const plotH = 160;
  const plotW = W - left - right;
  const H = plotH + topPad + 44;
  const x = (i: number) =>
    left + (points.length <= 1 ? plotW / 2 : (i / (points.length - 1)) * plotW);
  const y = (v: number) => topPad + plotH - (v / axisMax) * plotH;
  const path = (pick: (p: DayPoint) => number) =>
    points
      .map((p, i) => `${i === 0 ? 'M' : 'L'}${x(i).toFixed(1)} ${y(pick(p)).toFixed(1)}`)
      .join(' ');
  const series = [
    { label: 'Input tokens', color: 'var(--series-1)', pick: (p: DayPoint) => p.input_tokens },
    { label: 'Output tokens', color: 'var(--series-2)', pick: (p: DayPoint) => p.output_tokens },
  ];
  const first = points[0];
  const last = points[points.length - 1];

  return (
    <div className="viz-root space-y-2 rounded-md p-3">
      <Legend items={series.map((s) => ({ color: s.color, label: s.label }))} />
      <svg
        viewBox={`0 0 ${W} ${H}`}
        role="img"
        aria-label={`Daily input and output tokens from ${first?.day ?? ''} to ${last?.day ?? ''}`}
        className="w-full"
      >
        {ticks.map((t) => (
          <g key={t}>
            <line x1={left} x2={W - right} y1={y(t)} y2={y(t)} stroke="var(--grid)" />
            <text
              x={left - 6}
              y={y(t) + 4}
              textAnchor="end"
              fontSize="11"
              fill="var(--text-secondary)"
            >
              {compactNumber(t)}
            </text>
          </g>
        ))}
        {series.map((s) => (
          <g key={s.label}>
            {points.length > 1 && (
              <path
                d={path(s.pick)}
                fill="none"
                stroke={s.color}
                strokeWidth={2}
                strokeLinejoin="round"
                strokeLinecap="round"
              />
            )}
            {points.map((p, i) => (
              <circle
                key={p.day}
                cx={x(i)}
                cy={y(s.pick(p))}
                r={4}
                fill={s.color}
                stroke="var(--surface-1)"
                strokeWidth={2}
              >
                <title>{`${p.day}: ${s.label} ${formatCount(s.pick(p))}`}</title>
              </circle>
            ))}
          </g>
        ))}
        {first && (
          <text x={left} y={H - 22} fontSize="11" fill="var(--text-secondary)">
            {first.day}
          </text>
        )}
        {last && last !== first && (
          <text
            x={W - right}
            y={H - 22}
            textAnchor="end"
            fontSize="11"
            fill="var(--text-secondary)"
          >
            {last.day}
          </text>
        )}
        <text
          x={left + plotW / 2}
          y={H - 4}
          textAnchor="middle"
          fontSize="11"
          fill="var(--text-secondary)"
        >
          Day (UTC)
        </text>
        <text
          transform={`translate(12 ${topPad + plotH / 2}) rotate(-90)`}
          textAnchor="middle"
          fontSize="11"
          fill="var(--text-secondary)"
        >
          Tokens
        </text>
      </svg>
    </div>
  );
}
