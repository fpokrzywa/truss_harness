import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { artifact, stubBuild, version } from '@/test/build';
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

const A = '/api/v1/orgs/acme/agents/ag-1';
const baseSpec = (over: Record<string, unknown> = {}) => ({
  prompt_version_id: 'v-old',
  model: { model_id: 'm-1', fallback_model_ids: [] },
  tool_version_ids: [],
  skill_version_ids: [],
  memory: { mode: 'none', max_turns: 10 },
  limits: { max_iterations: 10, max_tool_calls: 20, max_tokens: 4000, deadline_s: 120 },
  execution_permissions: {
    allowed_principal_types: ['user'],
    required_permission: 'agent:execute',
  },
  approval_rules: { require_approval_for: ['write_idempotent'] },
  variables_map: {},
  ...over,
});

function setup(spec: Record<string, unknown>, routes: Parameters<typeof stubBuild>[2] = {}) {
  const fetchMock = stubBuild('agents', 'ag-1', {
    [`GET ${A}`]: () => jsonResponse(artifact({ id: 'ag-1', name: 'Support agent' })),
    [`GET ${A}/draft`]: () =>
      jsonResponse({ id: 'd-1', version_no: 1, state: 'draft', spec, row_version: 5 }),
    [`PUT ${A}/draft`]: () =>
      jsonResponse({ id: 'd-1', version_no: 1, state: 'draft', row_version: 6 }),
    'GET /api/v1/orgs/acme/prompts': () =>
      list([artifact({ id: 'p-9', key: 'greeter', name: 'Greeter' })]),
    'GET /api/v1/orgs/acme/prompts/p-9/versions': () =>
      list([
        version({ id: 'v-9', version_no: 3 }),
        version({ id: 'v-8', version_no: 2, state: 'retired' }),
      ]),
    'GET /api/v1/orgs/acme/tools': () =>
      list([artifact({ id: 't-9', key: 'lookup', name: 'Lookup' })]),
    'GET /api/v1/orgs/acme/tools/t-9/versions': () =>
      list([version({ id: 'tv-4', version_no: 4 })]),
    ...routes,
  });
  renderWithClient(<ArtifactDetailView kind="agents" id="ag-1" />);
  return fetchMock;
}

const savedSpec = (fetchMock: ReturnType<typeof setup>) =>
  bodyOf(fetchMock, `PUT ${A}/draft`) as { spec: Record<string, unknown>; row_version: number };

describe('agent editor payload: latest vs pinned', () => {
  it('saves "latest" as latest:{artifact_id} and a pin as the version id', async () => {
    const fetchMock = setup(baseSpec());
    const user = userEvent.setup();
    await screen.findByRole('option', { name: /Greeter/ });
    await user.selectOptions(screen.getByLabelText('Prompt: artifact'), 'p-9');
    await user.selectOptions(await screen.findByLabelText('Prompt: version'), 'latest');
    await user.click(screen.getByRole('button', { name: 'Save draft' }));
    await waitFor(() => expect(fetchMock.calls.some((c) => c.key === `PUT ${A}/draft`)).toBe(true));
    expect(savedSpec(fetchMock).spec.prompt_version_id).toBe('latest:p-9');
    expect(savedSpec(fetchMock).row_version).toBe(5);

    await user.selectOptions(screen.getByLabelText('Prompt: version'), 'v-9');
    await user.click(screen.getByRole('button', { name: 'Save draft' }));
    await waitFor(() =>
      expect(fetchMock.calls.filter((c) => c.key === `PUT ${A}/draft`).length).toBe(2),
    );
    const last = [...fetchMock.calls].reverse().find((c) => c.key === `PUT ${A}/draft`);
    expect(
      (JSON.parse(last?.init.body as string) as { spec: { prompt_version_id: string } }).spec
        .prompt_version_id,
    ).toBe('v-9');
  });

  it('offers only published versions to pin', async () => {
    setup(baseSpec());
    const user = userEvent.setup();
    await screen.findByRole('option', { name: /Greeter/ });
    await user.selectOptions(screen.getByLabelText('Prompt: artifact'), 'p-9');
    const sel = await screen.findByLabelText('Prompt: version');
    await waitFor(() =>
      expect(within(sel).getByRole('option', { name: 'Pinned v3' })).toBeInTheDocument(),
    );
    expect(within(sel).queryByRole('option', { name: 'Pinned v2' })).not.toBeInTheDocument();
  });

  it('adds tools as latest or pinned entries in order', async () => {
    const fetchMock = setup(baseSpec());
    const user = userEvent.setup();
    await screen.findByLabelText('Prompt: artifact');
    await user.click(screen.getByRole('button', { name: 'Add tool' }));
    await screen.findByRole('option', { name: /Lookup/ });
    await user.selectOptions(screen.getByLabelText('Tool 1: artifact'), 't-9');
    await user.selectOptions(screen.getByLabelText('Tool 1: version'), 'tv-4');
    await user.click(screen.getByRole('button', { name: 'Add tool' }));
    await user.selectOptions(screen.getByLabelText('Tool 2: artifact'), 't-9');
    await user.selectOptions(screen.getByLabelText('Tool 2: version'), 'latest');
    await user.click(screen.getByRole('button', { name: 'Save draft' }));
    await waitFor(() => expect(fetchMock.calls.some((c) => c.key === `PUT ${A}/draft`)).toBe(true));
    expect(savedSpec(fetchMock).spec.tool_version_ids).toEqual(['tv-4', 'latest:t-9']);
  });

  it('requires a prompt and a model before saving', async () => {
    setup(baseSpec({ prompt_version_id: '', model: { model_id: '', fallback_model_ids: [] } }));
    await screen.findByLabelText('Prompt: artifact');
    expect(screen.getByRole('alert')).toHaveTextContent(/Select a prompt/);
    expect(screen.getByRole('alert')).toHaveTextContent(/Select a model/);
    expect(screen.getByRole('button', { name: 'Save draft' })).toBeDisabled();
  });
});

describe('agent approval rules', () => {
  it('locks destructive approval on and cannot be unchecked', async () => {
    setup(baseSpec());
    const user = userEvent.setup();
    const destructive = await screen.findByLabelText('Require approval for destructive');
    expect(destructive).toBeChecked();
    expect(destructive).toBeDisabled();
    await user.click(destructive);
    expect(destructive).toBeChecked();
    const nonIdem = screen.getByLabelText('Require approval for write_non_idempotent');
    expect(nonIdem).toBeEnabled();
    expect(nonIdem).not.toBeChecked(); // draft only had write_idempotent (plus forced destructive)
  });

  it('always sends destructive, even when the stored draft lacked it', async () => {
    const fetchMock = setup(baseSpec());
    const user = userEvent.setup();
    await user.click(await screen.findByLabelText('Require approval for write_non_idempotent'));
    await user.click(screen.getByRole('button', { name: 'Save draft' }));
    await waitFor(() => expect(fetchMock.calls.some((c) => c.key === `PUT ${A}/draft`)).toBe(true));
    const rules = (savedSpec(fetchMock).spec.approval_rules as { require_approval_for: string[] })
      .require_approval_for;
    expect(rules).toEqual(
      expect.arrayContaining(['write_idempotent', 'write_non_idempotent', 'destructive']),
    );
  });

  it('defaults a new agent to write_non_idempotent and destructive', async () => {
    setup({});
    expect(await screen.findByLabelText('Require approval for write_non_idempotent')).toBeChecked();
    expect(screen.getByLabelText('Require approval for write_idempotent')).not.toBeChecked();
    expect(screen.getByLabelText('Require approval for destructive')).toBeChecked();
  });
});

describe('agent assembly test and activation', () => {
  it('shows assembled messages, tool declarations, output and usage with the harness note', async () => {
    const fetchMock = setup(baseSpec(), {
      [`POST ${A}/test`]: () =>
        jsonResponse({
          assembled: {
            messages: [
              { role: 'system', content: 'Be helpful.' },
              { role: 'user', content: '<untrusted source="variable:q">hi</untrusted>' },
            ],
            tools: [{ name: 'lookup', description: 'Find a customer' }],
          },
          output: 'Hello there',
          usage: { input_tokens: 30, output_tokens: 4 },
        }),
    });
    const user = userEvent.setup();
    const panel = await screen.findByRole('region', { name: 'Assembly test' });
    expect(within(panel).getByRole('note')).toHaveTextContent(/arrives with the execution harness/);
    await user.click(within(panel).getByLabelText('Input'));
    await user.paste('hello');
    await user.click(within(panel).getByRole('button', { name: 'Run assembly test' }));
    expect(await within(panel).findByText('Hello there')).toBeInTheDocument();
    expect(within(panel).getByText('Be helpful.')).toBeInTheDocument();
    expect(within(panel).getAllByTestId('untrusted-block')).toHaveLength(1);
    expect(within(panel).getByLabelText('Tool declarations')).toHaveTextContent('Find a customer');
    expect(within(panel).getByText('Tokens: 30 in / 4 out')).toBeInTheDocument();
    expect(bodyOf(fetchMock, `POST ${A}/test`)).toEqual({
      version_id: 'draft',
      variables: {},
      input: 'hello',
    });
  });

  it('deactivates through the status endpoint', async () => {
    const fetchMock = setup(baseSpec(), {
      [`POST ${A}/status`]: () => jsonResponse(artifact({ id: 'ag-1', status: 'disabled' })),
    });
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Deactivate' }));
    await waitFor(() =>
      expect(bodyOf(fetchMock, `POST ${A}/status`)).toEqual({ status: 'disabled' }),
    );
  });

  it('shows the resolved pinned components for a published version', async () => {
    setup(baseSpec(), {
      [`GET ${A}/versions`]: () => list([version({ id: 'v-1' })]),
      [`GET ${A}/versions/v-1`]: () =>
        jsonResponse(version({ id: 'v-1', spec: { prompt_version_id: 'v-9' } })),
      [`GET ${A}/versions/v-1/resolved`]: () =>
        jsonResponse({ spec_hash: 'deadbeef', prompt_version: { id: 'v-9', version_no: 3 } }),
    });
    const user = userEvent.setup();
    await screen.findByLabelText('Prompt: artifact');
    await user.click(screen.getByRole('tab', { name: 'Versions' }));
    await user.click(await screen.findByRole('button', { name: 'View v1' }));
    const dlg = await screen.findByRole('dialog', { name: 'Version details' });
    await user.click(await within(dlg).findByRole('button', { name: 'Show resolved components' }));
    expect(await within(dlg).findByLabelText('Resolved pinned components')).toHaveTextContent(
      '"version_no": 3',
    );
    expect(within(dlg).getByText('deadbeef')).toBeInTheDocument();
  });
});
