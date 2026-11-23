import { act, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import type { Run } from '@/lib/api/operate-types';
import type { PlatformStatus } from '@/lib/api/types';
import { CommandCenterContent } from './CommandCenter';

const status: PlatformStatus = {
  version: '0.9.0',
  environment: 'development',
  database: { status: 'ok', revision: 'r1', head: 'r1' },
  auth: { local_enabled: true, oidc_providers: 0 },
  worker: { status: 'stale', count: 0, last_heartbeat_at: '2026-11-06T12:00:00Z' },
};
const ready = { status: 200, body: { status: 'ready' } };

const run: Run = {
  id: 'run-1',
  kind: 'agent',
  status: 'completed',
  outcome_unknown: true,
  workspace: { id: 'w', slug: 'eng' },
  project: { id: 'p', slug: 'api' },
  created_at: '2026-11-06T12:00:00Z',
  started_at: '2026-11-06T12:00:00Z',
  finished_at: '2026-11-06T12:00:01Z',
} as Run;

const props = {
  org: 'acme',
  system: { status, ready },
  runs: [run],
  admin: [
    {
      title: 'Monitor',
      items: [{ id: 'operate-runs', label: 'Runs', group: 'Operate', href: '/o/acme/runs' }],
    },
  ],
  updatedAt: Date.parse('2026-11-06T12:00:05Z'),
};

describe('CommandCenterContent', () => {
  it('states the real condition and groups the detail into tabs', () => {
    render(<CommandCenterContent {...props} />);
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Workers need attention.');
    expect(screen.getAllByRole('tab').map((t) => t.textContent)).toEqual([
      'Startup check',
      'Deployment telemetry, needs attention',
      'Network status, needs attention',
    ]);
    expect(screen.getByRole('tab', { name: 'Startup check' })).toHaveAttribute(
      'aria-selected',
      'true',
    );
  });

  it('shows one panel at a time and moves between tabs by click and arrow key', async () => {
    const user = userEvent.setup();
    render(<CommandCenterContent {...props} />);
    expect(screen.getAllByRole('tabpanel')).toHaveLength(1);
    expect(screen.queryByRole('region', { name: 'Network status matrix' })).toBeNull();

    await user.click(screen.getByRole('tab', { name: /^Network status/ }));
    expect(screen.getAllByRole('tabpanel')).toHaveLength(1);
    expect(screen.getByRole('region', { name: 'Network status matrix' })).toBeVisible();

    await user.keyboard('{ArrowRight}');
    expect(screen.getByRole('tab', { name: 'Startup check' })).toHaveFocus();
  });

  it('shows the Board card only when given its link', () => {
    const { unmount } = render(<CommandCenterContent {...props} board="/o/acme/board" />);
    expect(screen.getByRole('heading', { level: 2, name: 'Board' })).toBeVisible();
    expect(screen.getByRole('link', { name: 'Open the board' })).toHaveAttribute(
      'href',
      '/o/acme/board',
    );
    unmount();
    render(<CommandCenterContent {...props} />);
    expect(screen.queryByRole('heading', { name: 'Board' })).toBeNull();
    expect(screen.queryByRole('link', { name: 'Open the board' })).toBeNull();
  });

  it('keeps Administration in view under the tabs, whichever tab is open', async () => {
    render(<CommandCenterContent {...props} />);
    expect(screen.getByRole('heading', { level: 2, name: 'Administration' })).toBeVisible();
    expect(screen.getByRole('link', { name: 'Runs' })).toBeVisible();
    await userEvent.click(screen.getByRole('tab', { name: /^Network status/ }));
    expect(screen.getByRole('link', { name: 'Runs' })).toBeVisible();
  });

  it('marks a tab only when something inside needs attention', () => {
    const healthy = {
      ...status,
      worker: { status: 'healthy', count: 1, last_heartbeat_at: '2026-11-06T12:00:00Z' },
    };
    render(
      <CommandCenterContent
        {...props}
        system={{ status: healthy, ready }}
        runs={[{ ...run, outcome_unknown: false }]}
      />,
    );
    expect(screen.getAllByRole('tab').map((t) => t.textContent)).toEqual([
      'Startup check',
      'Deployment telemetry',
      'Network status',
    ]);
  });

  it('lists each component in the matrix with its reported value', async () => {
    render(<CommandCenterContent {...props} />);
    await userEvent.click(screen.getByRole('tab', { name: /^Network status/ }));
    const matrix = screen.getByRole('region', { name: 'Network status matrix' });
    const row = within(matrix).getByRole('row', { name: /Workers/ });
    expect(within(row).getByText('stale')).toBeInTheDocument();
    expect(within(row).getByText('0 reporting')).toBeInTheDocument();
  });

  it('keeps every readout line available to assistive technology before it prints', () => {
    render(<CommandCenterContent {...props} />);
    expect(screen.getByText('Stale: 0 reporting')).toBeInTheDocument();
    expect(screen.getByText(/1 read, 0 in progress, 1 need attention/)).toBeInTheDocument();
  });

  it('prints the readout line by line', () => {
    vi.useFakeTimers();
    try {
      const { container } = render(<CommandCenterContent {...props} />);
      const printed = () => container.querySelectorAll('.cc-line[data-printed="true"]').length;
      expect(printed()).toBe(0);
      act(() => vi.advanceTimersByTime(200));
      expect(printed()).toBe(1);
      act(() => vi.advanceTimersByTime(5000));
      expect(printed()).toBe(container.querySelectorAll('.cc-line').length);
    } finally {
      vi.useRealTimers();
    }
  });

  it('links runs to their detail page and shows an unknown write outcome', () => {
    render(<CommandCenterContent {...props} />);
    const link = screen.getByRole('link', {
      name: /^Write outcome unknown: eng\/api/,
      hidden: true,
    });
    expect(link).toHaveAttribute('href', '/o/acme/runs/run-1');
  });

  it('says why runs are missing and tells non-admins where the rest went', () => {
    render(
      <CommandCenterContent
        {...props}
        runs={undefined}
        runsUnavailable="Your role cannot read runs."
      />,
    );
    expect(screen.getByText('Your role cannot read runs.')).toBeInTheDocument();
    expect(screen.getByText(/visible to platform administrators/)).toBeInTheDocument();
  });

  it('offers a pause control only while work is in flight', () => {
    const { unmount } = render(<CommandCenterContent {...props} />);
    expect(screen.queryByRole('button', { name: 'Pause motion' })).toBeNull();
    unmount();
    render(
      <CommandCenterContent
        {...props}
        runs={[{ ...run, status: 'running', outcome_unknown: false }]}
      />,
    );
    expect(screen.getByRole('button', { name: 'Pause motion' })).toBeInTheDocument();
  });

  it('explains an empty administration panel and an empty run list', () => {
    render(<CommandCenterContent {...props} runs={[]} admin={[]} />);
    expect(screen.getByText(/No runs in this organization yet/)).toBeInTheDocument();
    expect(screen.getByText(/no administrative areas/)).toBeInTheDocument();
  });
});
