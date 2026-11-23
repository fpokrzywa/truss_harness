import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MCP_TOOLS } from '@/lib/api/mcp-endpoints';
import { list, stubApi } from '@/test/security';
import { jsonResponse, renderWithClient } from '@/test/utils';
import { McpSettingsView } from './McpSettingsView';

vi.mock('next/navigation', () => ({ useParams: () => ({ org: 'acme' }) }));

const SETTINGS = 'GET /api/v1/orgs/acme/mcp/settings';
const PUT = 'PUT /api/v1/orgs/acme/mcp/settings';
const AUDIT = 'GET /api/v1/orgs/acme/audit-events';
const OFF = {
  enabled: false,
  rate_per_minute: 60,
  max_cost_per_call: 1,
  allowed_tools: null,
  updated_by: null,
  updated_at: null,
  endpoint_url: null,
};
const EVENT = {
  id: 'e1',
  occurred_at: '2026-11-23T10:00:00Z',
  actor: 'bot@acme.test',
  action: 'mcp.tool_called',
  outcome: 'success',
};
const WRITE = ['mcp:settings:read', 'mcp:settings:write', 'audit:read'];

afterEach(() => vi.unstubAllGlobals());

const putBodies = (fetchMock: ReturnType<typeof stubApi>) =>
  fetchMock.calls.filter((c) => c.key === PUT).map((c) => JSON.parse(String(c.init.body)));

describe('McpSettingsView', () => {
  it('asks for confirmation before the switch is saved', async () => {
    const fetchMock = stubApi(
      {
        [SETTINGS]: () => jsonResponse(OFF),
        [PUT]: ({ init }) =>
          jsonResponse({
            ...OFF,
            ...JSON.parse(String(init.body)),
            updated_at: '2026-11-23T11:00:00Z',
          }),
        [AUDIT]: () => list([]),
      },
      WRITE,
    );
    renderWithClient(<McpSettingsView />);
    const toggle = await screen.findByRole('switch', { name: 'MCP access' });
    await waitFor(() => expect(toggle).toBeEnabled());
    await userEvent.click(toggle);
    const dialog = await screen.findByRole('dialog', { name: 'Turn MCP access on?' });
    expect(putBodies(fetchMock)).toEqual([]);
    await userEvent.click(within(dialog).getByRole('button', { name: 'Turn on' }));
    await waitFor(() => expect(putBodies(fetchMock)).toHaveLength(1));
    expect(putBodies(fetchMock)[0]).toEqual({
      enabled: true,
      rate_per_minute: 60,
      max_cost_per_call: 1,
      allowed_tools: null,
    });
    expect(await screen.findByRole('switch', { name: 'MCP access' })).toHaveAttribute(
      'aria-checked',
      'true',
    );
  });

  it('cancelling the dialog saves nothing', async () => {
    const fetchMock = stubApi(
      { [SETTINGS]: () => jsonResponse(OFF), [AUDIT]: () => list([]) },
      WRITE,
    );
    renderWithClient(<McpSettingsView />);
    const toggle = await screen.findByRole('switch', { name: 'MCP access' });
    await waitFor(() => expect(toggle).toBeEnabled());
    await userEvent.click(toggle);
    const dialog = await screen.findByRole('dialog');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    expect(putBodies(fetchMock)).toEqual([]);
  });

  it('lists the nine tools, the default set checked, and saves an explicit list', async () => {
    const fetchMock = stubApi(
      {
        [SETTINGS]: () => jsonResponse(OFF),
        [PUT]: ({ init }) => jsonResponse({ ...OFF, ...JSON.parse(String(init.body)) }),
        [AUDIT]: () => list([]),
      },
      WRITE,
    );
    renderWithClient(<McpSettingsView />);
    const tools = await screen.findByRole('group', { name: 'Allowed tools' });
    const boxes = within(tools).getAllByRole('checkbox');
    expect(boxes).toHaveLength(9);
    for (const tool of MCP_TOOLS) {
      const box = within(tools).getByRole('checkbox', { name: tool });
      if (tool === 'start_run') expect(box).not.toBeChecked();
      else expect(box).toBeChecked();
    }
    await waitFor(() =>
      expect(within(tools).getByRole('checkbox', { name: 'whoami' })).toBeEnabled(),
    );
    await userEvent.click(within(tools).getByRole('checkbox', { name: 'start_run' }));
    await userEvent.click(within(tools).getByRole('checkbox', { name: 'get_deployment' }));
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(putBodies(fetchMock)).toHaveLength(1));
    const sent = putBodies(fetchMock)[0];
    expect(sent.enabled).toBe(false);
    expect([...sent.allowed_tools].sort()).toEqual(
      MCP_TOOLS.filter((t) => t !== 'get_deployment').sort(),
    );
  });

  it('is read-only without mcp:settings:write', async () => {
    stubApi({ [SETTINGS]: () => jsonResponse(OFF), [AUDIT]: () => list([]) }, [
      'mcp:settings:read',
    ]);
    renderWithClient(<McpSettingsView />);
    expect(await screen.findByRole('switch', { name: 'MCP access' })).toBeDisabled();
    expect(screen.getByRole('checkbox', { name: 'whoami' })).toBeDisabled();
    expect(screen.queryByRole('button', { name: 'Save' })).not.toBeInTheDocument();
  });

  it('requests the last 50 mcp audit events and shows them', async () => {
    const fetchMock = stubApi(
      { [SETTINGS]: () => jsonResponse(OFF), [AUDIT]: () => list([EVENT]) },
      WRITE,
    );
    renderWithClient(<McpSettingsView />);
    expect(await screen.findByText('mcp.tool_called')).toBeInTheDocument();
    const call = fetchMock.calls.find((c) => c.key === AUDIT);
    const params = new URL(call?.url ?? '', 'http://x').searchParams;
    expect(params.get('category')).toBe('mcp');
    expect(params.get('limit')).toBe('50');
  });

  it('shows the endpoint and a client snippet with a key placeholder', async () => {
    stubApi(
      {
        [SETTINGS]: () => jsonResponse({ ...OFF, endpoint_url: 'https://seth.example.com/mcp' }),
        [AUDIT]: () => list([]),
      },
      WRITE,
    );
    renderWithClient(<McpSettingsView />);
    const box = await screen.findByRole('region', { name: 'Endpoint' });
    expect(within(box).getAllByText(/https:\/\/seth\.example\.com\/mcp/).length).toBeGreaterThan(0);
    expect(box).toHaveTextContent('Bearer shf_<your API key with mcp:access>');
  });

  it('says the endpoint is not configured when the installation has no URL', async () => {
    stubApi({ [SETTINGS]: () => jsonResponse(OFF), [AUDIT]: () => list([]) }, WRITE);
    renderWithClient(<McpSettingsView />);
    const box = await screen.findByRole('region', { name: 'Endpoint' });
    expect(box).toHaveTextContent('The MCP endpoint is not configured on this installation.');
  });
});
