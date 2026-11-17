import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { run, stubOperate } from '@/test/operate';
import { list } from '@/test/security';
import { renderWithClient } from '@/test/utils';
import { RunHistoryView } from './RunHistoryView';

vi.mock('next/navigation', () => ({ useParams: () => ({ org: 'acme' }) }));
afterEach(() => vi.unstubAllGlobals());

const R = '/api/v1/orgs/acme/runs';
const AGENTS = 'GET /api/v1/orgs/acme/agents';
const lastRunsQuery = (calls: { key: string; url: string }[]) => {
  const c = calls.filter((x) => x.key === `GET ${R}`).at(-1);
  return new URL(c?.url ?? '', 'http://x').searchParams;
};

describe('RunHistoryView: unknown write outcome (R1)', () => {
  it.each(['cancelled', 'timed_out', 'failed'] as const)(
    'marks a %s run with an unknown outcome and never shows the plain status',
    async (status) => {
      stubOperate({
        [`GET ${R}`]: () =>
          list([
            run({
              id: 'r-u',
              status,
              outcome_unknown: true,
              error_class: 'indeterminate_side_effect',
              error_message: 'a write may have been delivered before this run ended',
            }),
          ]),
        [AGENTS]: () => list([]),
      });
      renderWithClient(<RunHistoryView />);
      expect(await screen.findByLabelText('Run status: Write outcome unknown')).toHaveTextContent(
        'Write outcome unknown',
      );
      const table = screen.getByRole('table');
      for (const plain of ['Cancelled', 'Timed out', 'Failed'])
        expect(within(table).queryByText(plain)).not.toBeInTheDocument();
      expect(screen.getByText(/a write may have been delivered/)).toBeInTheDocument();
    },
  );

  it('shows the verdict message once the question has been answered', async () => {
    stubOperate({
      [`GET ${R}`]: () =>
        list([
          run({
            status: 'cancelled',
            outcome_unknown: false,
            error_class: 'indeterminate_side_effect',
            error_message: 'an operator confirmed the write succeeded',
          }),
        ]),
      [AGENTS]: () => list([]),
    });
    renderWithClient(<RunHistoryView />);
    expect(await screen.findByLabelText('Run status: Cancelled')).toBeInTheDocument();
    expect(screen.getByText(/an operator confirmed the write succeeded/)).toBeInTheDocument();
    expect(screen.queryByText('Write outcome unknown')).not.toBeInTheDocument();
  });

  it('filters on outcome_unknown=true through the query string', async () => {
    const fetchMock = stubOperate({
      [`GET ${R}`]: () => list([run()]),
      [AGENTS]: () => list([]),
    });
    renderWithClient(<RunHistoryView />);
    const user = userEvent.setup();
    await screen.findByLabelText('Run status: Running');
    expect(lastRunsQuery(fetchMock.calls).has('outcome_unknown')).toBe(false);
    await user.selectOptions(screen.getByLabelText('Write outcome'), 'true');
    await waitFor(() => expect(lastRunsQuery(fetchMock.calls).get('outcome_unknown')).toBe('true'));
  });
});
