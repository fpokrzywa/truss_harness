import { screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { jsonResponse, problemResponse, renderWithClient, routeFetch } from '@/test/utils';
import { DashboardView } from './DashboardView';

let params: Record<string, string> = { org: 'acme' };
vi.mock('next/navigation', () => ({ useParams: () => params }));

afterEach(() => vi.unstubAllGlobals());

describe('DashboardView', () => {
  it('renders real counts and recent activity, scoped via query params', async () => {
    params = { org: 'acme', ws: 'eng', proj: 'api' };
    const fetchMock = routeFetch({
      'GET /api/v1/orgs/acme/dashboard': () =>
        jsonResponse({
          scope: { org: 'acme', workspace: 'eng', project: 'api' },
          counts: { workspaces: 3, projects: 7, members: 12 },
          recent_activity: [
            {
              at: '2026-01-02T03:04:05Z',
              actor: 'alice@acme.test',
              action: 'created project',
              target: 'api',
            },
          ],
        }),
    });
    vi.stubGlobal('fetch', fetchMock);
    renderWithClient(<DashboardView />);

    const workspaces = await screen.findByText('Workspaces');
    expect(workspaces.nextElementSibling).toHaveTextContent('3');
    expect(screen.getByText('Projects').nextElementSibling).toHaveTextContent('7');
    expect(screen.getByText('Members').nextElementSibling).toHaveTextContent('12');
    expect(screen.getByText('alice@acme.test')).toBeInTheDocument();
    expect(screen.getByText(/created project/)).toBeInTheDocument();
    expect(fetchMock.calls[0]?.url).toBe('/api/v1/orgs/acme/dashboard?workspace=eng&project=api');
  });

  it('shows an honest empty state, not sample data', async () => {
    params = { org: 'acme' };
    vi.stubGlobal(
      'fetch',
      routeFetch({
        'GET /api/v1/orgs/acme/dashboard': () =>
          jsonResponse({ counts: { workspaces: 0, projects: 0, members: 1 }, recent_activity: [] }),
      }),
    );
    renderWithClient(<DashboardView />);
    expect(await screen.findByText('No recent activity in this scope.')).toBeInTheDocument();
  });

  describe('Connections link (CM-6)', () => {
    const stub = (permissions: string[]) =>
      vi.stubGlobal(
        'fetch',
        routeFetch({
          'GET /api/v1/orgs/acme/dashboard': () =>
            jsonResponse({ counts: { projects: 1 }, recent_activity: [] }),
          'GET /api/v1/orgs/acme/me/permissions': () => jsonResponse({ permissions }),
        }),
      );

    it('links a project reader to the Connections space of the project', async () => {
      params = { org: 'acme', ws: 'eng', proj: 'api' };
      stub(['connection:read']);
      renderWithClient(<DashboardView />);
      expect(await screen.findByRole('link', { name: 'Connections' })).toHaveAttribute(
        'href',
        '/o/acme/connections?ws=eng&proj=api',
      );
      expect(screen.queryByRole('link', { name: 'Run an agent' })).not.toBeInTheDocument();
    });

    it('shows it next to Run an agent', async () => {
      params = { org: 'acme', ws: 'eng', proj: 'api' };
      stub(['agent:execute', 'delivery:target:read']);
      renderWithClient(<DashboardView />);
      const link = await screen.findByRole('link', { name: 'Connections' });
      expect(link.parentElement).toContainElement(
        screen.getByRole('link', { name: 'Run an agent' }),
      );
    });

    it('has no link without connection, model or target read', async () => {
      params = { org: 'acme', ws: 'eng', proj: 'api' };
      stub(['agent:execute']);
      renderWithClient(<DashboardView />);
      expect(await screen.findByRole('link', { name: 'Run an agent' })).toBeInTheDocument();
      expect(screen.queryByRole('link', { name: 'Connections' })).not.toBeInTheDocument();
    });
  });

  it('renders the neutral no-access page for 404 without echoing existence', async () => {
    params = { org: 'secret' };
    vi.stubGlobal(
      'fetch',
      routeFetch({ 'GET /api/v1/orgs/secret/dashboard': () => problemResponse(404, 'not_found') }),
    );
    renderWithClient(<DashboardView />);
    expect(
      await screen.findByRole('heading', { name: 'Not found or no access' }),
    ).toBeInTheDocument();
  });
});
