import { screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { EVALUATION, ORG, QUEUED, stubReleases } from '@/test/releases';
import { jsonResponse, renderWithClient } from '@/test/utils';
import { EvaluationRunView } from './EvaluationRunView';

vi.mock('next/navigation', () => ({ useParams: () => ({ org: 'acme' }) }));
afterEach(() => vi.unstubAllGlobals());

const metric = (name: string) =>
  within(screen.getByRole('table', { name: 'Metrics' }))
    .getByRole('rowheader', { name: name })
    .closest('tr')!;

describe('EvaluationRunView', () => {
  it('shows each metric against its threshold with meets or misses', async () => {
    stubReleases();
    renderWithClient(<EvaluationRunView id="ev-1" />);
    expect(await screen.findByText('accuracy below the minimum')).toBeInTheDocument();
    const summary = screen.getByRole('region', { name: 'Evaluation' });
    expect(within(summary).getByText('failed')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /^abcdef012345/ })).toHaveAttribute(
      'href',
      '/o/acme/releases/r-1',
    );
    const accuracy = within(metric('Accuracy')).getAllByRole('cell');
    expect(accuracy.map((c) => c.textContent)).toEqual(['0.5', '0.9', 'misses']);
    const latency = within(metric('p95 latency (ms)')).getAllByRole('cell');
    expect(latency.map((c) => c.textContent)).toEqual(['1200', '2000', 'meets']);
    const cost = within(metric('Total cost (USD)')).getAllByRole('cell');
    expect(cost.map((c) => c.textContent)).toEqual(['0.42', '1.5', 'meets']);
  });

  it('lists each case with its run link and the failed assertion reasons', async () => {
    stubReleases();
    renderWithClient(<EvaluationRunView id="ev-1" />);
    const table = await screen.findByRole('table', { name: 'Case results' });
    const c1 = within(table).getByText('c1').closest('tr')!;
    expect(c1).toHaveTextContent('passed');
    expect(within(c1).getByRole('link')).toHaveAttribute('href', '/o/acme/runs/run-1');
    const c2 = within(table).getByText('c2').closest('tr')!;
    expect(c2).toHaveTextContent('failed');
    expect(c2).toHaveTextContent('1200');
    expect(c2).toHaveTextContent('0.22');
    expect(within(c2).getByRole('link')).toHaveAttribute('href', '/o/acme/runs/run-2');
    expect(within(c2).getByText(/output does not contain "refund"/)).toHaveTextContent(
      'a2: output does not contain "refund"',
    );
  });

  it('shows pending values for a queued run with no results', async () => {
    stubReleases({ [`GET ${ORG}/evaluation-runs/ev-2`]: () => jsonResponse(QUEUED) });
    renderWithClient(<EvaluationRunView id="ev-2" />);
    expect(
      await screen.findByText(
        'The evaluation is queued; results are pending and refresh automatically.',
      ),
    ).toBeInTheDocument();
    for (const name of ['Accuracy', 'p95 latency (ms)', 'Total cost (USD)']) {
      const cells = within(metric(name)).getAllByRole('cell');
      expect(cells[0]).toHaveTextContent('pending');
      expect(cells[2]).toHaveTextContent('pending');
    }
    expect(screen.getByText('Case results are pending.')).toBeInTheDocument();
  });

  it('polls a queued run until it is scored', async () => {
    let calls = 0;
    const fetchMock = stubReleases({
      [`GET ${ORG}/evaluation-runs/ev-2`]: () =>
        jsonResponse(++calls === 1 ? QUEUED : { ...EVALUATION, id: 'ev-2' }),
    });
    renderWithClient(<EvaluationRunView id="ev-2" />);
    expect(
      await screen.findByText('accuracy below the minimum', {}, { timeout: 3000 }),
    ).toBeInTheDocument();
    const polls = fetchMock.calls.filter((c) => c.key === `GET ${ORG}/evaluation-runs/ev-2`).length;
    expect(polls).toBe(2);
  });
});
