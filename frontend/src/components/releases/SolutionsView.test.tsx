import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { list, RELEASE_READ_PERMS, S, SOLUTION, stubReleases } from '@/test/releases';
import { ALL_PERMS, bodyOf } from '@/test/security';
import { jsonResponse, problemResponse, renderWithClient } from '@/test/utils';
import { SolutionsView } from './SolutionsView';

const nav = vi.hoisted(() => ({ query: '', replace: vi.fn() }));
vi.mock('next/navigation', () => ({
  useParams: () => ({ org: 'acme' }),
  useSearchParams: () => new URLSearchParams(nav.query),
  usePathname: () => '/o/acme/solutions',
  useRouter: () => ({ replace: nav.replace, push: vi.fn() }),
}));
beforeEach(() => {
  nav.query = 'ws=eng&proj=api';
  nav.replace.mockReset();
});
afterEach(() => vi.unstubAllGlobals());

describe('SolutionsView project scope', () => {
  it('asks for a project and requests no solutions without ?ws=&proj=', async () => {
    nav.query = '';
    const fetchMock = stubReleases();
    renderWithClient(<SolutionsView />);
    expect(
      await screen.findByText('Choose a workspace and a project to see its solutions.'),
    ).toBeInTheDocument();
    await screen.findByRole('option', { name: 'Engineering' });
    expect(fetchMock.calls.some((c) => c.key.includes('/solutions'))).toBe(false);
    expect(screen.queryByRole('button', { name: 'New solution' })).not.toBeInTheDocument();
  });

  it('writes the workspace and project choice into the query string', async () => {
    nav.query = 'ws=eng';
    stubReleases();
    renderWithClient(<SolutionsView />);
    await screen.findByRole('option', { name: 'API' });
    await userEvent.selectOptions(screen.getByLabelText('Project'), 'api');
    expect(nav.replace).toHaveBeenCalledWith('/o/acme/solutions?ws=eng&proj=api');
  });
});

describe('SolutionsView list', () => {
  it('renders the solutions with names linking to the detail page with ws and proj', async () => {
    stubReleases();
    renderWithClient(<SolutionsView />);
    const table = await screen.findByRole('table', { name: 'Solutions' });
    const link = within(table).getByRole('link', { name: 'Sales assistant' });
    expect(link).toHaveAttribute('href', '/o/acme/solutions/s-1?ws=eng&proj=api');
    const row = link.closest('tr')!;
    expect(within(row).getByText('databricks.app/v1')).toBeInTheDocument();
    expect(within(row).getByText('active')).toBeInTheDocument();
  });

  it('shows an empty state', async () => {
    stubReleases({ [`GET ${S}`]: () => list([]) });
    renderWithClient(<SolutionsView />);
    expect(await screen.findByText(/No solutions in this project yet/)).toBeInTheDocument();
  });

  it('shows the error alert on a 500', async () => {
    stubReleases({
      [`GET ${S}`]: () => problemResponse(500, 'internal_error', { detail: 'boom' }),
    });
    renderWithClient(<SolutionsView />);
    expect(await screen.findByRole('alert')).toHaveTextContent('boom');
  });

  it('has no New solution or Archive buttons without packaging:solution:write', async () => {
    stubReleases({}, [...ALL_PERMS, ...RELEASE_READ_PERMS]);
    renderWithClient(<SolutionsView />);
    await screen.findByRole('table', { name: 'Solutions' });
    expect(screen.queryByRole('button', { name: 'New solution' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Archive/ })).not.toBeInTheDocument();
  });

  it('links to the solution generator with the chosen project for appgen readers', async () => {
    stubReleases({}, [...ALL_PERMS, ...RELEASE_READ_PERMS, 'appgen:request:read']);
    renderWithClient(<SolutionsView />);
    expect(await screen.findByRole('link', { name: 'Generate solution' })).toHaveAttribute(
      'href',
      '/o/acme/solutions/generate?ws=eng&proj=api',
    );
  });

  it('has no generator link without appgen:request:read', async () => {
    stubReleases({}, [...ALL_PERMS, ...RELEASE_READ_PERMS]);
    renderWithClient(<SolutionsView />);
    await screen.findByRole('table', { name: 'Solutions' });
    expect(screen.queryByRole('link', { name: 'Generate solution' })).not.toBeInTheDocument();
  });
});

describe('SolutionsView write', () => {
  it('creates a solution with the fixed target kind and refreshes the list', async () => {
    const fetchMock = stubReleases({
      [`POST ${S}`]: () => jsonResponse(SOLUTION, { status: 201 }),
    });
    renderWithClient(<SolutionsView />);
    await userEvent.click(await screen.findByRole('button', { name: 'New solution' }));
    const dialog = screen.getByRole('dialog');
    expect(within(dialog).getByText('databricks.app/v1')).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole('button', { name: 'Create solution' }));
    expect(within(dialog).getByText('Name must be 1 to 200 characters.')).toBeInTheDocument();
    expect(fetchMock.calls.some((c) => c.key.startsWith('POST'))).toBe(false);
    await userEvent.type(within(dialog).getByLabelText('Name'), 'Sales assistant');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Create solution' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(bodyOf(fetchMock, `POST ${S}`)).toEqual({
      name: 'Sales assistant',
      target_kind: 'databricks.app/v1',
    });
    await waitFor(() => expect(fetchMock.calls.filter((c) => c.key === `GET ${S}`).length).toBe(2));
  });

  it('archives after confirmation, then refreshes', async () => {
    const fetchMock = stubReleases({
      [`POST ${S}/s-1/archive`]: () => jsonResponse({ ...SOLUTION, status: 'archived' }),
    });
    renderWithClient(<SolutionsView />);
    await userEvent.click(await screen.findByRole('button', { name: /Archive/ }));
    expect(fetchMock.calls.some((c) => c.key.endsWith('/archive'))).toBe(false);
    await userEvent.click(screen.getByRole('button', { name: 'Archive solution' }));
    await waitFor(() =>
      expect(fetchMock.calls.some((c) => c.key === `POST ${S}/s-1/archive`)).toBe(true),
    );
    await waitFor(() => expect(fetchMock.calls.filter((c) => c.key === `GET ${S}`).length).toBe(2));
  });
});
