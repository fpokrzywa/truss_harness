import { screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { jsonResponse, problemResponse, renderWithClient, routeFetch } from '@/test/utils';
import { formatDate } from './admin/ui';
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

  describe('MCP access card', () => {
    const MCP = 'GET /api/v1/orgs/acme/mcp/settings';
    const SETTINGS = {
      enabled: true,
      rate_per_minute: 60,
      max_cost_per_call: 1,
      allowed_tools: null,
      updated_by: 'u-admin-7',
      updated_at: '2026-11-20T09:15:00Z',
      endpoint_url: 'https://seth.example.com/mcp',
    };
    const stub = (permissions: string[], settings: () => Response) => {
      const fetchMock = routeFetch({
        'GET /api/v1/orgs/acme/dashboard': () =>
          jsonResponse({ counts: { workspaces: 2, projects: 5 }, recent_activity: [] }),
        'GET /api/v1/orgs/acme/me/permissions': () => jsonResponse({ permissions }),
        [MCP]: settings,
      });
      vi.stubGlobal('fetch', fetchMock);
      return fetchMock;
    };
    const card = async () => {
      const heading = await screen.findByRole('heading', { name: 'MCP access' });
      return heading.closest('section')!;
    };

    it('shows On, the last change, the endpoint and a link to the settings', async () => {
      params = { org: 'acme' };
      stub(['mcp:settings:read'], () => jsonResponse(SETTINGS));
      renderWithClient(<DashboardView />);
      const c = await card();
      expect(await within(c).findByText('On')).toBeInTheDocument();
      expect(within(c).getByText(formatDate(SETTINGS.updated_at))).toBeInTheDocument();
      expect(within(c).getByText('u-admin-7')).toBeInTheDocument();
      expect(within(c).getByText('https://seth.example.com/mcp')).toBeInTheDocument();
      expect(within(c).getByRole('link', { name: 'MCP settings' })).toHaveAttribute(
        'href',
        '/o/acme/settings/mcp',
      );
      expect(within(c).queryByRole('switch')).not.toBeInTheDocument();
      expect(within(c).queryByRole('button')).not.toBeInTheDocument();
    });

    it('shows Off and Not configured for the defaults on an installation without an endpoint', async () => {
      params = { org: 'acme' };
      stub(['mcp:settings:read'], () =>
        jsonResponse({
          ...SETTINGS,
          enabled: false,
          updated_by: null,
          updated_at: null,
          endpoint_url: null,
        }),
      );
      renderWithClient(<DashboardView />);
      const c = await card();
      expect(await within(c).findByText('Off')).toBeInTheDocument();
      expect(within(c).getByText('Not configured on this installation')).toBeInTheDocument();
      expect(within(c).queryByText('Last changed')).not.toBeInTheDocument();
      expect(within(c).queryByText('Changed by')).not.toBeInTheDocument();
    });

    it('is absent and never requested without mcp:settings:read', async () => {
      params = { org: 'acme' };
      const fetchMock = stub(['member:read'], () => jsonResponse(SETTINGS));
      renderWithClient(<DashboardView />);
      expect(await screen.findByText('Workspaces')).toBeInTheDocument();
      await waitFor(() =>
        expect(fetchMock.calls.some((c) => c.key === 'GET /api/v1/orgs/acme/me/permissions')).toBe(
          true,
        ),
      );
      await new Promise((resolve) => setTimeout(resolve, 50));
      expect(screen.queryByRole('heading', { name: 'MCP access' })).not.toBeInTheDocument();
      expect(fetchMock.calls.some((c) => c.key === MCP)).toBe(false);
    });

    it.each([[{ org: 'acme', ws: 'eng' }], [{ org: 'acme', ws: 'eng', proj: 'api' }]])(
      'is absent at workspace and project level (%j)',
      async (scope) => {
        params = scope;
        const fetchMock = stub(['mcp:settings:read', 'agent:execute'], () =>
          jsonResponse(SETTINGS),
        );
        renderWithClient(<DashboardView />);
        expect(await screen.findByText('Projects')).toBeInTheDocument();
        await waitFor(() =>
          expect(
            fetchMock.calls.some((c) => c.key === 'GET /api/v1/orgs/acme/me/permissions'),
          ).toBe(true),
        );
        await new Promise((resolve) => setTimeout(resolve, 50));
        expect(screen.queryByRole('heading', { name: 'MCP access' })).not.toBeInTheDocument();
        expect(fetchMock.calls.some((c) => c.key === MCP)).toBe(false);
      },
    );

    it('shows a failed read in the card and keeps the counts', async () => {
      params = { org: 'acme' };
      stub(['mcp:settings:read'], () =>
        problemResponse(500, 'internal_error', { detail: 'settings store unavailable' }),
      );
      renderWithClient(<DashboardView />);
      const c = await card();
      expect(await within(c).findByText('settings store unavailable')).toBeInTheDocument();
      expect(within(c).getByText('Could not load the MCP settings')).toBeInTheDocument();
      expect(screen.getByText('Workspaces').nextElementSibling).toHaveTextContent('2');
      expect(screen.getByText('Projects').nextElementSibling).toHaveTextContent('5');
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
