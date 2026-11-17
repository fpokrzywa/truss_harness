import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { RunStatus } from '@/lib/api/operate-types';
import { run, stubOperate } from '@/test/operate';
import { list } from '@/test/security';
import { jsonResponse, renderWithClient } from '@/test/utils';
import { RunDetailView } from './RunDetailView';

vi.mock('next/navigation', () => ({
  useParams: () => ({ org: 'acme' }),
  useSearchParams: () => new URLSearchParams(),
}));
afterEach(() => vi.unstubAllGlobals());

const R = '/api/v1/orgs/acme/runs/run-1';
const empty = {
  [`GET ${R}/steps`]: () => list([]),
  [`GET ${R}/events`]: () => list([]),
  [`GET ${R}/result`]: () => jsonResponse({ output: 'All **done**' }),
};

describe('RunDetailView', () => {
  it('stops polling once the run reaches a terminal state', async () => {
    let n = 0;
    const fetchMock = stubOperate({
      ...empty,
      [`GET ${R}`]: () => {
        n += 1;
        return jsonResponse(run({ status: n >= 2 ? 'completed' : 'running' }));
      },
    });
    renderWithClient(<RunDetailView id="run-1" />);
    expect(await screen.findByLabelText('Run status: Running')).toBeInTheDocument();
    expect(
      await screen.findByLabelText('Run status: Completed', {}, { timeout: 4000 }),
    ).toBeInTheDocument();
    const count = () => fetchMock.calls.filter((c) => c.key === `GET ${R}`).length;
    const settled = count();
    await new Promise((r) => setTimeout(r, 2300));
    expect(count()).toBe(settled);
  }, 15000);

  it.each<[RunStatus, boolean]>([
    ['pending', true],
    ['running', true],
    ['waiting_for_approval', true],
    ['completed', false],
    ['failed', false],
    ['cancelled', false],
    ['timed_out', false],
  ])('cancel button for %s is visible: %s', async (status, visible) => {
    stubOperate({ ...empty, [`GET ${R}`]: () => jsonResponse(run({ status })) });
    renderWithClient(<RunDetailView id="run-1" />);
    await screen.findByLabelText(/Run status:/);
    if (visible)
      expect(await screen.findByRole('button', { name: 'Cancel run' })).toBeInTheDocument();
    else expect(screen.queryByRole('button', { name: 'Cancel run' })).not.toBeInTheDocument();
  });

  it('posts cancel for a running run', async () => {
    const fetchMock = stubOperate({
      ...empty,
      [`GET ${R}`]: () => jsonResponse(run()),
      [`POST ${R}/cancel`]: () => jsonResponse({}, { status: 202 }),
    });
    renderWithClient(<RunDetailView id="run-1" />);
    await userEvent.click(await screen.findByRole('button', { name: 'Cancel run' }));
    await waitFor(() =>
      expect(fetchMock.calls.some((c) => c.key === `POST ${R}/cancel`)).toBe(true),
    );
  });

  it('shows identifiers with copy buttons, usage and the safe-rendered result', async () => {
    stubOperate({ ...empty, [`GET ${R}`]: () => jsonResponse(run({ status: 'completed' })) });
    renderWithClient(<RunDetailView id="run-1" />);
    const ids = await screen.findByRole('region', { name: 'Identifiers' });
    expect(within(ids).getByText('trace-abc')).toBeInTheDocument();
    expect(within(ids).getByText('corr-xyz')).toBeInTheDocument();
    expect(within(ids).getByRole('button', { name: 'Copy Trace id' })).toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Usage summary' })).toHaveTextContent('Input tokens');
    const result = await screen.findByRole('region', { name: 'Run result' });
    expect(within(result).getByText('done').tagName).toBe('STRONG');
  });

  it('explains a failed run (error class, retryable flag, suggested action)', async () => {
    stubOperate({
      ...empty,
      [`GET ${R}`]: () =>
        jsonResponse(
          run({ status: 'failed', error_class: 'rate_limited', error_message: 'slow down' }),
        ),
    });
    renderWithClient(<RunDetailView id="run-1" />);
    const panel = await screen.findByRole('region', { name: 'Failure details' });
    expect(within(panel).getByText('Rate limited')).toBeInTheDocument();
    expect(panel).toHaveTextContent('Retryable');
    expect(panel).toHaveTextContent('Yes');
    expect(panel).toHaveTextContent('Retry later');
    expect(panel).toHaveTextContent('slow down');
  });

  it('timeline groups attempts per step, orders events and marks lease loss', async () => {
    stubOperate({
      [`GET ${R}`]: () => jsonResponse(run({ status: 'completed' })),
      [`GET ${R}/result`]: () => jsonResponse({ output: 'x' }),
      [`GET ${R}/events`]: () =>
        list([
          { seq: 3, at: '2026-11-03T10:00:09Z', type: 'recovered', detail: { worker: 'w2' } },
          {
            seq: 1,
            at: '2026-11-03T10:00:00Z',
            type: 'state_change',
            from: 'pending',
            to: 'running',
          },
          { seq: 2, at: '2026-11-03T10:00:05Z', type: 'lease_lost' },
          {
            seq: 4,
            at: '2026-11-03T10:00:10Z',
            type: 'retry_scheduled',
            step_id: 's2',
            detail: { backoff_ms: 750 },
          },
        ]),
      [`GET ${R}/steps`]: () =>
        list([
          {
            id: 's2',
            run_id: 'run-1',
            seq: 1,
            kind: 'tool_call',
            name: 'crm.read',
            status: 'succeeded',
            attempt: 2,
            started_at: '2026-11-03T10:00:11Z',
          },
          {
            id: 's1',
            run_id: 'run-1',
            seq: 1,
            kind: 'tool_call',
            name: 'crm.read',
            status: 'failed',
            attempt: 1,
            error_class: 'timeout',
            started_at: '2026-11-03T10:00:01Z',
          },
        ]),
    });
    renderWithClient(<RunDetailView id="run-1" />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('tab', { name: 'Execution timeline' }));
    const timeline = await screen.findByRole('list', { name: 'Execution timeline' });
    const kinds = within(timeline)
      .getAllByRole('listitem')
      .filter(
        (li) => li.hasAttribute('data-testid') && li.getAttribute('data-testid') !== 'step-attempt',
      )
      .map((li) => li.getAttribute('data-event-type') ?? 'step');
    expect(kinds).toEqual(['state_change', 'step', 'lease_lost', 'recovered', 'retry_scheduled']);
    expect(within(timeline).getAllByTestId('step-attempt')).toHaveLength(2);
    expect(within(timeline).getByText('2 attempts')).toBeInTheDocument();
    expect(within(timeline).getByText('Backoff 750 ms')).toBeInTheDocument();
    expect(within(timeline).getByText('Recovered by another worker')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Details for step 1 crm.read' }));
    const drawer = screen.getByRole('complementary', { name: 'Step details' });
    expect(within(drawer).getByRole('region', { name: 'Attempt 1' })).toHaveTextContent('timeout');
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('complementary', { name: 'Step details' })).not.toBeInTheDocument();
  });
});
