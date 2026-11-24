import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { httpApi } from './api';
import { TableScreen } from './TableScreen';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

const TABLE = {
  kind: 'table' as const,
  title: 'Orders',
  tool: 'orders by/region',
  inputs: ['region', 'since'],
};

const RESULT = {
  columns: [
    { name: 'id', type: 'int' },
    { name: 'note', type: 'string' },
  ],
  rows: [
    [1, 'first'],
    [2, null],
  ],
  row_count: 2,
  truncated: false,
};

afterEach(() => vi.unstubAllGlobals());

describe('TableScreen', () => {
  it('shows one labelled field per input', () => {
    render(<TableScreen screen={TABLE} api={httpApi} />);
    expect(screen.getByLabelText('region')).toBeInTheDocument();
    expect(screen.getByLabelText('since')).toBeInTheDocument();
    expect(screen.getAllByRole('textbox')).toHaveLength(2);
  });

  it('posts the non-empty params to the encoded tool URL and shows the rows', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(jsonResponse(RESULT));
    vi.stubGlobal('fetch', fetchMock);
    render(<TableScreen screen={TABLE} api={httpApi} />);

    await userEvent.type(screen.getByLabelText('region'), 'EU');
    await userEvent.click(screen.getByRole('button', { name: 'Run' }));

    const table = await screen.findByRole('table');
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/api/tools/orders%20by%2Fregion');
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body as string)).toEqual({ params: { region: 'EU' } });

    const headers = within(table)
      .getAllByRole('columnheader')
      .map((th) => th.textContent);
    expect(headers).toEqual(['id', 'note']);
    const rows = within(table).getAllByRole('row').slice(1);
    expect(rows).toHaveLength(2);
    expect(
      within(rows[0]!)
        .getAllByRole('cell')
        .map((td) => td.textContent),
    ).toEqual(['1', 'first']);
    expect(
      within(rows[1]!)
        .getAllByRole('cell')
        .map((td) => td.textContent),
    ).toEqual(['2', '']);
    expect(screen.queryByText(/truncated/)).not.toBeInTheDocument();
  });

  it('shows a running status while pending and the row count after, with a scrollable table', async () => {
    let resolve: (value: Response) => void = () => {};
    vi.stubGlobal(
      'fetch',
      vi.fn().mockReturnValueOnce(new Promise<Response>((r) => (resolve = r))),
    );
    render(<TableScreen screen={TABLE} api={httpApi} />);
    expect(screen.getByRole('status')).toHaveTextContent('');

    await userEvent.click(screen.getByRole('button', { name: 'Run' }));
    expect(screen.getByRole('button', { name: 'Running...' })).toBeDisabled();
    expect(screen.getByRole('status')).toHaveTextContent('Running...');

    resolve(jsonResponse(RESULT));
    const table = await screen.findByRole('table');
    expect(screen.getByRole('status')).toHaveTextContent('2 rows');
    expect(screen.getByRole('button', { name: 'Run' })).toBeEnabled();
    expect(table.parentElement).toHaveClass('table-wrap');
  });

  it('shows the truncated note when the result is truncated', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValueOnce(jsonResponse({ ...RESULT, truncated: true })),
    );
    render(<TableScreen screen={TABLE} api={httpApi} />);
    await userEvent.click(screen.getByRole('button', { name: 'Run' }));
    expect(await screen.findByText(/truncated/)).toBeVisible();
  });

  it('sends empty params when every field is empty', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(jsonResponse(RESULT));
    vi.stubGlobal('fetch', fetchMock);
    render(<TableScreen screen={TABLE} api={httpApi} />);
    await userEvent.click(screen.getByRole('button', { name: 'Run' }));
    await screen.findByRole('table');
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({ params: {} });
  });

  it('shows an error and no stale result on a failed request', async () => {
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValueOnce(jsonResponse(RESULT))
        .mockResolvedValueOnce(jsonResponse({ detail: 'bad' }, 422)),
    );
    render(<TableScreen screen={TABLE} api={httpApi} />);
    await userEvent.click(screen.getByRole('button', { name: 'Run' }));
    expect(await screen.findByRole('table')).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: 'Run' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('HTTP 422');
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
  });
});
