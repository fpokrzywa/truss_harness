import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { run, stubOperate } from '@/test/operate';
import { list } from '@/test/security';
import { renderWithClient } from '@/test/utils';
import { RunHistoryView } from './RunHistoryView';

vi.mock('next/navigation', () => ({ useParams: () => ({ org: 'acme' }) }));
afterEach(() => vi.unstubAllGlobals());

const R = '/api/v1/orgs/acme/runs';
const lastRunsQuery = (calls: { key: string; url: string }[]) => {
  const c = calls.filter((x) => x.key === `GET ${R}`).at(-1);
  return new URL(c?.url ?? '', 'http://x').searchParams;
};

describe('RunHistoryView', () => {
  it('turns filters into the query string', async () => {
    const fetchMock = stubOperate({
      [`GET ${R}`]: () => list([run({ status: 'failed' })]),
      'GET /api/v1/orgs/acme/agents': () => list([]),
    });
    renderWithClient(<RunHistoryView />);
    const user = userEvent.setup();
    await screen.findByLabelText('Run status: Failed');
    await user.selectOptions(screen.getByLabelText('Status'), 'failed');
    await user.selectOptions(screen.getByLabelText('Kind'), 'orchestration');
    await user.type(screen.getByLabelText('Initiated by'), 'u-1');
    await user.type(screen.getByLabelText('From date'), '2026-11-01');
    await waitFor(() => {
      const q = lastRunsQuery(fetchMock.calls);
      expect(q.get('status')).toBe('failed');
      expect(q.get('kind')).toBe('orchestration');
      expect(q.get('initiated_by')).toBe('u-1');
      expect(q.get('from')).toMatch(/^2026-11-0[1-2]T/);
      expect(q.has('to')).toBe(false);
      expect(q.has('agent_id')).toBe(false);
    });
  });

  it('labels statuses accessibly and links to run detail', async () => {
    stubOperate({
      [`GET ${R}`]: () =>
        list([
          run({ id: 'r-a', status: 'waiting_for_approval' }),
          run({ id: 'r-b', status: 'timed_out' }),
        ]),
      'GET /api/v1/orgs/acme/agents': () => list([]),
    });
    renderWithClient(<RunHistoryView />);
    expect(await screen.findByLabelText('Run status: Waiting for approval')).toBeInTheDocument();
    expect(screen.getByLabelText('Run status: Timed out')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'r-a' })).toHaveAttribute('href', '/o/acme/runs/r-a');
  });

  it('paginates with the cursor via Load more', async () => {
    let page = 0;
    const fetchMock = stubOperate({
      [`GET ${R}`]: ({ url }) => {
        page += 1;
        return page === 1
          ? list([run({ id: 'r-1' })], 'cur-2')
          : (expect(url).toContain('cursor=cur-2'), list([run({ id: 'r-2' })], null));
      },
      'GET /api/v1/orgs/acme/agents': () => list([]),
    });
    renderWithClient(<RunHistoryView />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Load more' }));
    expect(await screen.findByRole('link', { name: 'r-2' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'r-1' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Load more' })).not.toBeInTheDocument();
    expect(fetchMock.calls.filter((c) => c.key === `GET ${R}`)).toHaveLength(2);
  });
});
