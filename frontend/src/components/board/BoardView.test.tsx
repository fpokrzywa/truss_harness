import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { formatDate } from '@/components/admin/ui';
import { BOARD, PROJECT, TRUNCATED, board, stubBoard } from '@/test/board';
import { jsonResponse, problemResponse, renderWithClient } from '@/test/utils';
import { BoardView } from './BoardView';

const nav = vi.hoisted(() => ({ query: '', replace: vi.fn() }));
vi.mock('next/navigation', () => ({
  useParams: () => ({ org: 'acme' }),
  useSearchParams: () => new URLSearchParams(nav.query),
  usePathname: () => '/o/acme/board',
  useRouter: () => ({ replace: nav.replace, push: vi.fn() }),
}));
beforeEach(() => {
  nav.query = '';
  nav.replace.mockReset();
});
afterEach(() => vi.unstubAllGlobals());

const grid = () => screen.findByRole('table', { name: 'Board Engineering / API' });

/** The cell of a solution's row under the n-th target column (0-based). */
async function cell(solution: string, column: number) {
  const table = await grid();
  const row = within(table)
    .getByRole('rowheader', { name: new RegExp(solution) })
    .closest('tr')!;
  return within(row).getAllByRole('cell')[column]!;
}

describe('BoardView grid', () => {
  it('groups target columns under their environment, ordered by environment then name', async () => {
    stubBoard();
    renderWithClient(<BoardView />);
    const table = await grid();
    expect(screen.getByRole('heading', { level: 1, name: 'Board' })).toBeInTheDocument();
    const groups = within(table).getAllByRole('columnheader', { name: /^(dev|prod)$/ });
    expect(groups.map((g) => [g.textContent, g.getAttribute('colspan')])).toEqual([
      ['dev', '1'],
      ['prod', '2'],
    ]);
    const targets = within(table)
      .getAllByRole('columnheader')
      .filter((h) => h.textContent?.endsWith('databricks'))
      .map((h) => h.textContent);
    expect(targets).toEqual([
      'Analytics devdatabricks',
      'Analytics proddatabricks',
      'Zeta proddatabricks',
    ]);
    expect(
      within(table)
        .getAllByRole('rowheader')
        .map((h) => h.textContent),
    ).toEqual([
      expect.stringMatching(/^Sales assistant/),
      expect.stringMatching(/^Billing bot archived$/),
    ]);
  });

  it('shows the current version and a verified badge, each linked', async () => {
    stubBoard();
    renderWithClient(<BoardView />);
    const c = await cell('Sales assistant', 2);
    expect(within(c).getByRole('link', { name: 'v3' })).toHaveAttribute(
      'href',
      '/o/acme/releases/r-3',
    );
    expect(within(c).getByRole('link', { name: /verified/ })).toHaveAttribute(
      'href',
      '/o/acme/deliver/deployments/d-3',
    );
  });

  it('shows an unknown outcome without a current version', async () => {
    stubBoard();
    renderWithClient(<BoardView />);
    const c = await cell('Sales assistant', 0);
    expect(within(c).getByText('Outcome unknown')).toBeInTheDocument();
    expect(within(c).queryByText(/^v\d+$/)).not.toBeInTheDocument();
    expect(within(c).getByRole('link', { name: /Outcome unknown/ })).toHaveAttribute(
      'href',
      '/o/acme/deliver/deployments/d-4',
    );
  });

  it('shows a failed badge with its reason', async () => {
    stubBoard();
    renderWithClient(<BoardView />);
    const c = await cell('Billing bot', 0);
    expect(within(c).getByText('failed')).toBeInTheDocument();
    expect(within(c).getByText('warehouse unreachable')).toBeInTheDocument();
  });

  it('shows an open plan with its expiry, and an expired plan', async () => {
    stubBoard();
    renderWithClient(<BoardView />);
    const approved = within(await cell('Sales assistant', 0)).getByRole('link', {
      name: /approved/,
    });
    expect(approved).toHaveAttribute('href', '/o/acme/deliver/plans/pl-1');
    expect(approved).toHaveTextContent(`expires ${formatDate('2026-11-25T10:00:00Z')}`);
    const expired = within(await cell('Billing bot', 0)).getByRole('link', { name: /expired/ });
    expect(expired).toHaveAttribute('href', '/o/acme/deliver/plans/pl-2');
    expect(expired).not.toHaveTextContent('expires');
  });

  it('shows "No deployment" for empty or missing cells, without an error', async () => {
    stubBoard();
    renderWithClient(<BoardView />);
    expect(await cell('Sales assistant', 1)).toHaveTextContent('No deployment');
    expect(await cell('Billing bot', 1)).toHaveTextContent('No deployment');
    expect(await cell('Billing bot', 2)).toHaveTextContent('No deployment');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('lists releases with their evaluation badge, and none without an evaluation', async () => {
    stubBoard();
    renderWithClient(<BoardView />);
    await grid();
    const releases = screen.getByRole('list', { name: 'Releases of Sales assistant' });
    const [v3, v2] = within(releases).getAllByRole('listitem');
    expect(within(v3!).getByRole('link', { name: 'v3' })).toHaveAttribute(
      'href',
      '/o/acme/releases/r-3',
    );
    expect(within(v3!).getByText('abcdef012345')).toBeInTheDocument();
    expect(within(v3!).getByRole('link', { name: /passed/ })).toHaveAttribute(
      'href',
      '/o/acme/evaluations/ev-3',
    );
    expect(within(v2!).getByRole('link', { name: 'v2' })).toBeInTheDocument();
    expect(within(v2!).getByText('withdrawn')).toBeInTheDocument();
    expect(within(v2!).getAllByRole('link')).toHaveLength(1);
  });
});

describe('BoardView projects', () => {
  it('notes a truncated project only, and shows a project without solutions as empty', async () => {
    stubBoard({ [`GET ${BOARD}`]: () => jsonResponse(board([PROJECT, TRUNCATED])) });
    renderWithClient(<BoardView />);
    const web = await screen.findByRole('region', { name: 'Engineering / Web' });
    expect(web).toHaveTextContent("Only part of this project's solutions is shown.");
    expect(web).toHaveTextContent('No solutions in this project.');
    const api = screen.getByRole('region', { name: 'Engineering / API' });
    expect(api).not.toHaveTextContent('Only part');
  });

  it('shows an empty state, not an error, when nothing matches', async () => {
    stubBoard({ [`GET ${BOARD}`]: () => jsonResponse(board([])) });
    renderWithClient(<BoardView />);
    expect(await screen.findByText('Nothing to show for this selection.')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('shows the shared error alert on a 500', async () => {
    stubBoard({
      [`GET ${BOARD}`]: () => problemResponse(500, 'internal_error', { detail: 'boom' }),
    });
    renderWithClient(<BoardView />);
    expect(await screen.findByRole('alert')).toHaveTextContent('boom');
  });

  it('renders server text as plain text', async () => {
    const evil = '<img src=x onerror=alert(1)>';
    stubBoard({
      [`GET ${BOARD}`]: () =>
        jsonResponse(
          board([{ ...PROJECT, solutions: [{ ...PROJECT.solutions[0]!, name: evil }] }]),
        ),
    });
    const { container } = renderWithClient(<BoardView />);
    expect(await screen.findByText(evil)).toBeInTheDocument();
    expect(container.querySelector('img')).toBeNull();
  });
});

describe('BoardView filters and paging', () => {
  it('asks for the whole organization without a selection', async () => {
    const fetchMock = stubBoard();
    renderWithClient(<BoardView />);
    await grid();
    const call = fetchMock.calls.find((c) => c.key === `GET ${BOARD}`)!;
    expect(call.url).toBe(BOARD);
  });

  it('passes ?ws=&proj= as the workspace and project filters', async () => {
    nav.query = 'ws=eng&proj=api';
    const fetchMock = stubBoard();
    renderWithClient(<BoardView />);
    await grid();
    const url = fetchMock.calls.find((c) => c.key === `GET ${BOARD}`)!.url;
    expect(url).toContain('workspace=eng');
    expect(url).toContain('project=api');
  });

  it('filters by workspace alone', async () => {
    nav.query = 'ws=eng';
    const fetchMock = stubBoard();
    renderWithClient(<BoardView />);
    await grid();
    const url = fetchMock.calls.find((c) => c.key === `GET ${BOARD}`)!.url;
    expect(url).toContain('workspace=eng');
    expect(url).not.toContain('project=');
  });

  it('loads the next page with the cursor and appends its projects', async () => {
    nav.query = 'ws=eng&proj=api';
    const fetchMock = stubBoard({
      [`GET ${BOARD}`]: ({ url }) =>
        jsonResponse(url.includes('cursor=c2') ? board([TRUNCATED]) : board([PROJECT], 'c2')),
    });
    renderWithClient(<BoardView />);
    await grid();
    await userEvent.click(screen.getByRole('button', { name: 'Load more' }));
    expect(await screen.findByRole('region', { name: 'Engineering / Web' })).toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Engineering / API' })).toBeInTheDocument();
    const urls = fetchMock.calls.filter((c) => c.key === `GET ${BOARD}`).map((c) => c.url);
    expect(urls.at(-1)).toContain('cursor=c2');
    expect(screen.queryByRole('button', { name: 'Load more' })).not.toBeInTheDocument();
  });
});
