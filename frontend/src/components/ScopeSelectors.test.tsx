import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { parseScope } from '@/lib/scope';
import { jsonResponse, renderWithClient, routeFetch } from '@/test/utils';
import { ScopeSelectors } from './ScopeSelectors';

const push = vi.fn();
vi.mock('next/navigation', () => ({ useRouter: () => ({ push, replace: vi.fn() }) }));

const memberships = [
  { org: { id: '1', slug: 'acme', name: 'Acme Inc', status: 'active' as const }, roles: [] },
  { org: { id: '2', slug: 'globex', name: 'Globex', status: 'active' as const }, roles: [] },
  { org: { id: '3', slug: 'old', name: 'Old Org', status: 'archived' as const }, roles: [] },
];
const scopeItem = (slug: string, name: string, status = 'active') => ({
  id: slug,
  slug,
  name,
  status,
  row_version: 1,
});

beforeEach(() => {
  push.mockClear();
  vi.stubGlobal(
    'fetch',
    routeFetch({
      'GET /api/v1/orgs/acme/workspaces': () =>
        jsonResponse({
          items: [
            scopeItem('eng', 'Engineering'),
            scopeItem('ops', 'Operations'),
            scopeItem('gone', 'Gone', 'archived'),
          ],
          next_cursor: null,
        }),
      'GET /api/v1/orgs/acme/workspaces/eng/projects': () =>
        jsonResponse({ items: [scopeItem('api', 'API Service')], next_cursor: null }),
    }),
  );
});
afterEach(() => vi.unstubAllGlobals());

describe('ScopeSelectors', () => {
  it('populates org selector from memberships (excluding archived) and navigates on change', async () => {
    renderWithClient(<ScopeSelectors scope={parseScope('/o/acme')} memberships={memberships} />);
    const org = screen.getByLabelText('Organization');
    expect(
      within(org)
        .getAllByRole('option')
        .map((o) => o.textContent),
    ).toEqual(['Acme Inc', 'Globex']);
    expect(org).toHaveValue('acme');
    await userEvent.selectOptions(org, 'globex');
    expect(push).toHaveBeenCalledWith('/o/globex');
  });

  it('populates workspaces from the API, hides archived, and drives the URL', async () => {
    renderWithClient(<ScopeSelectors scope={parseScope('/o/acme')} memberships={memberships} />);
    const ws = screen.getByLabelText('Workspace');
    await waitFor(() =>
      expect(
        within(ws)
          .getAllByRole('option')
          .map((o) => o.textContent),
      ).toEqual(['All workspaces', 'Engineering', 'Operations']),
    );
    expect(screen.queryByLabelText('Project')).not.toBeInTheDocument();
    await userEvent.selectOptions(ws, 'ops');
    expect(push).toHaveBeenCalledWith('/o/acme/w/ops');
  });

  it('shows the project selector within a workspace and navigates to the project dashboard', async () => {
    renderWithClient(
      <ScopeSelectors scope={parseScope('/o/acme/w/eng')} memberships={memberships} />,
    );
    const proj = screen.getByLabelText('Project');
    await waitFor(() =>
      expect(within(proj).getByRole('option', { name: 'API Service' })).toBeInTheDocument(),
    );
    await userEvent.selectOptions(proj, 'api');
    expect(push).toHaveBeenCalledWith('/o/acme/w/eng/p/api');
  });

  it('clearing the workspace returns to the org dashboard', async () => {
    renderWithClient(
      <ScopeSelectors scope={parseScope('/o/acme/w/eng')} memberships={memberships} />,
    );
    const ws = screen.getByLabelText('Workspace');
    await waitFor(() =>
      expect(within(ws).getByRole('option', { name: 'Engineering' })).toBeInTheDocument(),
    );
    await userEvent.selectOptions(ws, '');
    expect(push).toHaveBeenCalledWith('/o/acme');
  });
});
