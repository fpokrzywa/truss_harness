import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { artifact, stubBuild } from '@/test/build';
import { bodyOf, list } from '@/test/security';
import { jsonResponse, renderWithClient } from '@/test/utils';
import { ArtifactDetailView } from './ArtifactDetailView';

// These tests are about editor logic: stack the sections as before. Tabs: SectionTabs.test.tsx.
vi.mock('./SectionTabs', async (original) => ({
  ...(await original<typeof import('./SectionTabs')>()),
  SectionTabs: ({ children }: { children: React.ReactNode }) => children,
}));

vi.mock('next/navigation', () => ({
  useParams: () => ({ org: 'acme' }),
  useRouter: () => ({ push: vi.fn() }),
}));
afterEach(() => vi.unstubAllGlobals());

const T = '/api/v1/orgs/acme/tools/t-1';
const spec = (over: Record<string, unknown> = {}) => ({
  executor: 'builtin.function',
  binding: { function_key: 'text.upper' },
  input_schema: {
    type: 'object',
    properties: { name: { type: 'string' }, count: { type: 'integer', minimum: 1 } },
    required: ['name'],
  },
  output_schema: { type: 'object', properties: {} },
  side_effect_class: 'none',
  required_permissions: [],
  invocation_policy: {},
  timeout_s: 30,
  retry: { max_attempts: 1, backoff_s: 1 },
  audit_level: 'metadata',
  idempotency: 'none',
  ...over,
});

function setup(toolSpec: Record<string, unknown>, routes: Parameters<typeof stubBuild>[2] = {}) {
  const fetchMock = stubBuild('tools', 't-1', {
    [`GET ${T}`]: () => jsonResponse(artifact({ id: 't-1', name: 'Upper' })),
    [`GET ${T}/draft`]: () =>
      jsonResponse({ id: 'd-1', version_no: 1, state: 'draft', spec: toolSpec, row_version: 2 }),
    'GET /api/v1/tool-executors': () =>
      jsonResponse({
        items: [
          {
            key: 'builtin.function',
            functions: [
              {
                key: 'text.upper',
                description: 'Uppercases text',
                input_schema: { type: 'object', properties: { text: { type: 'string' } } },
              },
            ],
          },
          { key: 'rest.http' },
          { key: 'mcp.tool' },
          { key: 'connection.sql', maturity: 'planned' },
        ],
      }),
    ...routes,
  });
  renderWithClient(<ArtifactDetailView kind="tools" id="t-1" />);
  return fetchMock;
}

describe('tool test panel side-effect acknowledgement', () => {
  it.each(['write_idempotent', 'write_non_idempotent', 'destructive'])(
    'requires the acknowledge checkbox for %s',
    async (cls) => {
      const fetchMock = setup(spec({ side_effect_class: cls, idempotency: 'key_supported' }), {
        [`POST ${T}/test`]: () =>
          jsonResponse({ output: { ok: true }, valid_output: true, duration_ms: 5, attempts: 1 }),
      });
      const user = userEvent.setup();
      const panel = await screen.findByRole('region', { name: 'Test panel' });
      await user.type(within(panel).getByLabelText('name (required)'), 'Ann');
      const run = within(panel).getByRole('button', { name: 'Run test' });
      expect(run).toBeDisabled();
      expect(within(panel).getByRole('note')).toHaveTextContent(/real/i);
      await user.click(
        within(panel).getByLabelText(/I understand this test run performs real side effects/),
      );
      expect(run).toBeEnabled();
      await user.click(run);
      expect(await within(panel).findByText('Output matches schema')).toBeInTheDocument();
      expect(bodyOf(fetchMock, `POST ${T}/test`)).toEqual({
        version_id: 'draft',
        args: { name: 'Ann' },
        acknowledge_side_effects: true,
      });
    },
  );

  it('needs no acknowledgement for read tools and sends none', async () => {
    const fetchMock = setup(spec({ side_effect_class: 'read' }), {
      [`POST ${T}/test`]: () =>
        jsonResponse({ output: 1, valid_output: false, duration_ms: 5, attempts: 2 }),
    });
    const user = userEvent.setup();
    const panel = await screen.findByRole('region', { name: 'Test panel' });
    expect(within(panel).queryByLabelText(/I understand/)).not.toBeInTheDocument();
    await user.type(within(panel).getByLabelText('name (required)'), 'Ann');
    await user.type(within(panel).getByLabelText('count'), '3');
    await user.click(within(panel).getByRole('button', { name: 'Run test' }));
    expect(await within(panel).findByText('Output does not match schema')).toBeInTheDocument();
    expect(within(panel).getByText('Attempts: 2')).toBeInTheDocument();
    expect(bodyOf(fetchMock, `POST ${T}/test`)).toEqual({
      version_id: 'draft',
      args: { name: 'Ann', count: 3 },
    });
  });

  it('validates the args form from the input schema before calling the server', async () => {
    const fetchMock = setup(spec());
    const user = userEvent.setup();
    const panel = await screen.findByRole('region', { name: 'Test panel' });
    await user.type(within(panel).getByLabelText('count'), '0');
    await user.click(within(panel).getByRole('button', { name: 'Run test' }));
    expect(within(panel).getByText('name is required.')).toBeInTheDocument();
    expect(within(panel).getByText('Must be at least 1.')).toBeInTheDocument();
    expect(fetchMock.calls.some((c) => c.key === `POST ${T}/test`)).toBe(false);
  });
});

describe('tool editor rules', () => {
  it.each(['destructive', 'write_non_idempotent'])(
    'blocks retry above 1 for %s even with an idempotency key',
    async (cls) => {
      const fetchMock = setup(spec({ side_effect_class: cls, idempotency: 'key_supported' }), {
        [`PUT ${T}/draft`]: () =>
          jsonResponse({ id: 'd-1', version_no: 1, state: 'draft', row_version: 3 }),
      });
      const user = userEvent.setup();
      await screen.findByLabelText('Executor');
      const attempts = screen.getByLabelText('Retry max attempts');
      await user.clear(attempts);
      await user.type(attempts, '3');
      expect(screen.getAllByText(/Retries above 1 are not allowed/).length).toBeGreaterThan(0);
      expect(screen.getByRole('button', { name: 'Save draft' })).toBeDisabled();
      expect(screen.getByRole('button', { name: 'Validate' })).toBeDisabled();
      await user.clear(attempts);
      await user.type(attempts, '1');
      expect(screen.queryByText(/Retries above 1 are not allowed/)).not.toBeInTheDocument();
      await user.type(screen.getByLabelText('Timeout (seconds)'), '0'); // make the draft dirty again
      expect(screen.getByRole('button', { name: 'Save draft' })).toBeEnabled();
      await user.click(screen.getByRole('button', { name: 'Save draft' }));
      await waitFor(() =>
        expect(fetchMock.calls.some((c) => c.key === `PUT ${T}/draft`)).toBe(true),
      );
    },
  );

  it('allows retries for read tools', async () => {
    setup(spec({ side_effect_class: 'read' }));
    const user = userEvent.setup();
    const attempts = await screen.findByLabelText('Retry max attempts');
    await user.clear(attempts);
    await user.type(attempts, '3');
    expect(screen.queryByText(/Retries above 1 are not allowed/)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save draft' })).toBeEnabled();
  });

  it('shows a warning per side-effect class and sets the idempotency key for write_idempotent', async () => {
    setup(spec());
    const user = userEvent.setup();
    const cls = await screen.findByLabelText('Side-effect class');
    await user.selectOptions(cls, 'destructive');
    expect(screen.getAllByRole('note').some((n) => /irreversibly/.test(n.textContent ?? ''))).toBe(
      true,
    );
    await user.selectOptions(cls, 'write_idempotent');
    expect(screen.getByLabelText('Idempotency')).toHaveValue('key_supported');
  });

  it('blocks saving while a schema is not valid JSON Schema', async () => {
    setup(spec());
    const user = userEvent.setup();
    const input = await screen.findByLabelText('Input schema');
    await user.click(input);
    await user.clear(input);
    await user.paste('{"type":"strng"}');
    expect((await screen.findAllByText(/\$\.type: must be one of/)).length).toBeGreaterThan(0);
    expect(screen.getByRole('button', { name: 'Save draft' })).toBeDisabled();
    await user.clear(input);
    await user.paste('{"type":"object"}');
    await waitFor(() => expect(screen.getByRole('button', { name: 'Save draft' })).toBeEnabled());
  });

  it('shows the builtin function description and its schemas, and marks planned executors', async () => {
    setup(spec());
    expect(await screen.findByText('Uppercases text')).toBeInTheDocument();
    expect(screen.getByLabelText('Function input schema')).toHaveTextContent('"text"');
    expect(
      screen.getByRole('option', { name: /connection\.sql \(Declared — not validated\)/ }),
    ).toBeInTheDocument();
  });

  it('builds an MCP binding with a schema snapshot from the latest discovery', async () => {
    const fetchMock = setup(spec({ executor: 'mcp.tool', binding: {} }), {
      'GET /api/v1/orgs/acme/connections': () =>
        list([
          {
            id: 'c-mcp',
            name: 'Docs MCP',
            connection_type: 'mcp',
            adapter_key: 'mcp_http',
            status: 'active',
            auth_method: 'none',
            access_mode: 'read',
            config: {},
            row_version: 1,
          },
        ]),
      'GET /api/v1/orgs/acme/connections/c-mcp/discoveries/latest': () =>
        jsonResponse({
          id: 'd-1',
          kind: 'mcp_tools',
          payload: {
            tools: [
              { name: 'search', description: 'Search docs', input_schema: { type: 'object' } },
            ],
          },
        }),
      [`PUT ${T}/draft`]: () =>
        jsonResponse({ id: 'd-1', version_no: 1, state: 'draft', row_version: 3 }),
    });
    const user = userEvent.setup();
    await screen.findByLabelText('MCP connection');
    await screen.findByRole('option', { name: /Docs MCP/ });
    await user.selectOptions(screen.getByLabelText('MCP connection'), 'c-mcp');
    await waitFor(() => expect(screen.getByRole('option', { name: 'search' })).toBeInTheDocument());
    await user.selectOptions(screen.getByLabelText('MCP tool'), 'search');
    expect(screen.getByLabelText('Schema snapshot')).toHaveTextContent('Search docs');
    await user.click(screen.getByRole('button', { name: 'Save draft' }));
    await waitFor(() => expect(fetchMock.calls.some((c) => c.key === `PUT ${T}/draft`)).toBe(true));
    const saved = bodyOf(fetchMock, `PUT ${T}/draft`) as {
      spec: { binding: Record<string, unknown> };
    };
    expect(saved.spec.binding).toMatchObject({
      connection_id: 'c-mcp',
      tool_name: 'search',
      schema_snapshot: { name: 'search', input_schema: { type: 'object' } },
    });
  });
});
