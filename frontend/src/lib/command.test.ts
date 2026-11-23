import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import type { Run } from './api/operate-types';
import type { PlatformInstallation, PlatformStatus } from './api/types';
import { contrastRatio } from './contrast';
import {
  adminGroups,
  boardCardHref,
  buildNodes,
  buildReadout,
  buildWorkerNodes,
  headline,
  runDuration,
  runLabel,
  runTone,
  statusTone,
  tallyRuns,
} from './command';

const status: PlatformStatus = {
  version: '0.9.0',
  environment: 'development',
  database: { status: 'ok', revision: 'r1', head: 'r1' },
  auth: { local_enabled: true, oidc_providers: 0 },
  worker: { status: 'healthy', count: 1, last_heartbeat_at: '2026-11-06T12:00:00Z' },
};
const ready = { status: 200, body: { status: 'ready' } };

const run = (over: Partial<Run>): Run =>
  ({ id: 'r', status: 'completed', outcome_unknown: false, ...over }) as Run;

describe('component state', () => {
  it('asks for attention on a status it does not recognise', () => {
    expect(statusTone('healthy')).toBe('good');
    expect(statusTone('not_deployed')).toBe('idle');
    expect(statusTone('stale')).toBe('warn');
    expect(statusTone('something_new')).toBe('warn');
  });

  it('never reads as healthy when readiness has no body', () => {
    const nodes = buildNodes({ status, ready: { status: 503, body: undefined } });
    expect(nodes[0]).toMatchObject({ id: 'api', raw: 'not_ready', tone: 'warn' });
    expect(headline(nodes)).toBe('API needs attention.');
  });

  it('states the condition in the headline', () => {
    const healthy = buildNodes({ status, ready });
    expect(headline(healthy)).toBe('Every component is carrying load.');
    const down = buildNodes({
      status: { ...status, worker: { status: 'stale' }, database: { status: 'behind' } },
      ready,
    });
    expect(headline(down)).toBe('2 components need attention.');
  });

  it('shows queue and traces only when the admin source is present', () => {
    expect(buildNodes({ status, ready }).map((n) => n.id)).toEqual(['api', 'database', 'workers']);
    const nodes = buildNodes({
      status,
      ready,
      details: {
        queue: { queued: 1, leased: 2, dead_in_window: 3 },
        workers: { running: 1, known: 1 },
        telemetry: { exporter: 'none', otlp: null, buffered_spans: 4 },
      },
    });
    expect(nodes.find((n) => n.id === 'queue')?.tone).toBe('warn');
    expect(nodes.find((n) => n.id === 'traces')?.tone).toBe('idle');
  });
});

describe('workers', () => {
  it('lists live workers and rolls stopped ones into one idle row', () => {
    const nodes = buildWorkerNodes({
      workers: [
        { name: 'w1', last_seen_at: '2026-11-06T12:00:00Z', status: 'alive' },
        { name: 'old1', last_seen_at: '2026-11-04T12:00:00Z', status: 'stale' },
        { name: 'old2', last_seen_at: '2026-11-05T12:00:00Z', status: 'stale' },
      ],
    } as PlatformInstallation);
    expect(nodes.map((n) => n.name)).toEqual(['w1', 'Stopped workers']);
    expect(nodes[1]).toMatchObject({
      tone: 'idle',
      state: '2 stopped',
      lastSeenAt: '2026-11-05T12:00:00Z',
    });
  });

  it('has no rows without the admin source', () => {
    expect(buildWorkerNodes(undefined)).toEqual([]);
  });
});

describe('runs', () => {
  it('flags an unknown write outcome whatever the status says (R1)', () => {
    const unknown = run({ status: 'completed', outcome_unknown: true });
    expect(runTone(unknown)).toBe('warn');
    expect(runLabel(unknown)).toBe('Write outcome unknown');
  });

  it('tallies by tone', () => {
    const t = tallyRuns([
      run({}),
      run({ status: 'running' }),
      run({ status: 'failed' }),
      run({ status: 'timed_out' }),
      run({ status: 'cancelled' }),
    ]);
    expect(t).toEqual({ good: 1, active: 1, warn: 2, idle: 1 });
  });

  it('formats duration', () => {
    expect(runDuration({ started_at: null })).toBe('Not started');
    expect(runDuration({ started_at: '2026-11-06T12:00:00Z' })).toBe('Running');
    expect(
      runDuration({ started_at: '2026-11-06T12:00:00Z', finished_at: '2026-11-06T12:00:02.5Z' }),
    ).toBe('2.5s');
  });

  it('adds a run line to the readout only when runs could be read', () => {
    const nodes = buildNodes({ status, ready });
    expect(buildReadout(nodes, [], undefined)).toHaveLength(3);
    expect(buildReadout(nodes, [], [run({ status: 'failed' })]).at(-1)).toEqual({
      key: 'Recent runs',
      value: '1 read, 0 in progress, 1 need attention',
      tone: 'warn',
    });
  });
});

describe('administration', () => {
  const features = [
    'tenancy',
    'operate',
    'integrate',
    'security',
    'build',
    'pipelines',
    'governance',
  ];

  it('follows the nav permission rules and drops empty groups', () => {
    const groups = adminGroups('acme', {
      isPlatformAdmin: false,
      permissions: new Set(['run:read']),
      features,
    });
    expect(groups.map((g) => g.title)).toEqual(['Monitor']);
    expect(groups[0]!.items.map((i) => i.id)).toEqual(['operate-runs', 'platform-status']);
  });

  it('adds platform settings for platform admins only', () => {
    const groups = adminGroups('acme', { isPlatformAdmin: true, features });
    expect(groups.flatMap((g) => g.items.map((i) => i.id))).toContain('platform-settings');
  });

  it('links the Board card only where the nav shows Board', () => {
    const opts = (perms: string[]) => ({
      isPlatformAdmin: false,
      permissions: new Set(perms),
      features,
    });
    expect(boardCardHref('acme', opts(['packaging:solution:read']))).toBe('/o/acme/board');
    expect(boardCardHref('acme', opts(['delivery:target:read']))).toBeUndefined();
  });
});

describe('brand tokens meet WCAG AAA on both dark bases', () => {
  const css = readFileSync('src/app/globals.css', 'utf8');
  const token = (name: string): string => {
    const m = new RegExp(`--color-shi-${name}: (#[0-9a-f]{6})`).exec(css);
    if (!m) throw new Error(`missing token ${name}`);
    return m[1]!;
  };

  /** Page background where a 1px grid line crosses it: the lightest pixel text can sit on. */
  function gridPixel(): string {
    const alpha = /rgb\(60 96 128 \/ ([\d.]+)\)/.exec(css);
    const a = Number(alpha![1]);
    const base = parseInt(token('slate').slice(1), 16);
    const line = parseInt(token('blue-slate').slice(1), 16);
    const mix = (shift: number) =>
      Math.round(((line >> shift) & 255) * a + ((base >> shift) & 255) * (1 - a))
        .toString(16)
        .padStart(2, '0');
    return `#${mix(16)}${mix(8)}${mix(0)}`;
  }

  it.each(['ink', 'tint', 'green'])('%s text reaches 7:1', (name) => {
    for (const bg of [token('slate'), token('grey'), gridPixel()])
      expect(contrastRatio(token(name), bg)).toBeGreaterThanOrEqual(7);
  });

  it.each(['pink', 'orange', 'light'])('%s reaches 3:1 for non-text use', (name) => {
    for (const bg of [token('slate'), token('grey')])
      expect(contrastRatio(token(name), bg)).toBeGreaterThanOrEqual(3);
  });

  it('filled buttons carry 7:1 white text', () => {
    expect(contrastRatio(token('ink'), token('mid-deep'))).toBeGreaterThanOrEqual(7);
    expect(contrastRatio(token('ink'), token('danger'))).toBeGreaterThanOrEqual(7);
  });

  it('remapped Tailwind ramps stay AAA on the surfaces they are used on', () => {
    const ramp = (name: string): string => {
      const m = new RegExp(`--color-${name}: (#[0-9a-f]{6})`).exec(css);
      if (!m) throw new Error(`missing ramp step ${name}`);
      return m[1]!;
    };
    const pairs: [string[], string[]][] = [
      [
        ['slate-500', 'slate-600', 'slate-700', 'slate-800', 'slate-900'],
        ['slate-50', 'slate-100'],
      ],
      [
        ['red-700', 'red-800', 'red-900', 'slate-600'],
        ['red-50', 'slate-50', 'slate-100'],
      ],
      [
        ['amber-800', 'amber-900', 'slate-600'],
        ['amber-50', 'slate-50', 'slate-100'],
      ],
      [
        ['green-800', 'green-900', 'slate-600'],
        ['green-50', 'slate-50', 'slate-100'],
      ],
      [
        ['blue-800', 'blue-900', 'slate-600'],
        ['blue-50', 'slate-50', 'slate-100'],
      ],
    ];
    for (const [texts, surfaces] of pairs)
      for (const text of texts)
        for (const surface of surfaces)
          expect(
            contrastRatio(ramp(text), ramp(surface)),
            `${text} on ${surface}`,
          ).toBeGreaterThanOrEqual(7);
  });

  it('white on Mid Blue reaches 4.5:1 for the large hover text', () => {
    expect(contrastRatio(token('ink'), token('mid'))).toBeGreaterThanOrEqual(4.5);
  });
});
