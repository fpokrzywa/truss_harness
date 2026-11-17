import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { artifact } from '@/test/build';
import { run, stubOperate } from '@/test/operate';
import { list } from '@/test/security';
import { jsonResponse, renderWithClient } from '@/test/utils';
import { RunConsole } from './RunConsole';

let search = '';
vi.mock('next/navigation', () => ({
  useParams: () => ({ org: 'acme', ws: 'eng', proj: 'api' }),
  useSearchParams: () => new URLSearchParams(search),
}));
afterEach(() => {
  vi.unstubAllGlobals();
  search = '';
});

const R = '/api/v1/orgs/acme/runs';
const agent = artifact({
  id: 'ag-1',
  name: 'Helper',
  latest_published_version: { id: 'av-2', version_no: 2 },
});

function setup(post?: () => Response | Promise<Response>, agentSpec?: unknown) {
  return stubOperate({
    'GET /api/v1/orgs/acme/agents': () => list([agent]),
    'GET /api/v1/orgs/acme/agents/ag-1/versions/av-2/resolved': () =>
      jsonResponse({
        spec: agentSpec ?? {},
        prompt_version: {
          spec: {
            variables: [
              { name: 'topic', type: 'string', required: true, trusted: true },
              { name: 'message', type: 'string', required: true, trusted: false },
            ],
          },
        },
      }),
    [`POST ${R}`]: post ?? (() => jsonResponse(run({ status: 'pending' }), { status: 202 })),
    [`GET ${R}/run-1`]: () => jsonResponse(run({ status: 'running' })),
    [`GET ${R}/run-1/steps`]: () => list([]),
    [`GET ${R}/run-1/events`]: () => list([]),
  });
}

async function fill(user: ReturnType<typeof userEvent.setup>) {
  await screen.findByRole('option', { name: /Helper/ });
  await user.selectOptions(screen.getByLabelText('Agent'), 'ag-1');
  await user.type(await screen.findByLabelText('Input'), 'hello');
  await user.type(await screen.findByLabelText(/topic/), 'billing');
  await user.type(await screen.findByLabelText(/message/), 'hi');
}

describe('RunConsole', () => {
  it('generates the variables form from the prompt and sends an Idempotency-Key', async () => {
    const fetchMock = setup();
    renderWithClient(<RunConsole />);
    const user = userEvent.setup();
    await fill(user);
    await user.click(screen.getByRole('button', { name: 'Start run' }));
    const post = await waitFor(() => {
      const c = fetchMock.calls.find((x) => x.key === `POST ${R}`);
      expect(c).toBeDefined();
      return c!;
    });
    const headers = post.init.headers as Record<string, string>;
    expect(headers['Idempotency-Key']).toMatch(/^[0-9a-f-]{36}$/);
    expect(JSON.parse(post.init.body as string)).toEqual({
      kind: 'agent',
      agent_id: 'ag-1',
      version_id: 'av-2',
      workspace: 'eng',
      project: 'api',
      input: 'hello',
      variables: { topic: 'billing', message: 'hi' },
    });
    expect(await screen.findByLabelText('Run status: Running')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Cancel run' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Open run detail' })).toHaveAttribute(
      'href',
      '/o/acme/runs/run-1',
    );
  });

  it('prevents a double submit', async () => {
    let release: () => void = () => undefined;
    const gate = new Promise<void>((r) => (release = r));
    const fetchMock = setup(async () => {
      await gate;
      return jsonResponse(run({ status: 'pending' }), { status: 202 });
    });
    renderWithClient(<RunConsole />);
    const user = userEvent.setup();
    await fill(user);
    await user.dblClick(screen.getByRole('button', { name: 'Start run' }));
    release();
    await screen.findByLabelText(/Run status:/);
    expect(fetchMock.calls.filter((c) => c.key === `POST ${R}`)).toHaveLength(1);
  });

  it('blocks the run when a required variable is missing', async () => {
    const fetchMock = setup();
    renderWithClient(<RunConsole />);
    const user = userEvent.setup();
    await screen.findByRole('option', { name: /Helper/ });
    await user.selectOptions(screen.getByLabelText('Agent'), 'ag-1');
    await user.type(await screen.findByLabelText('Input'), 'hello');
    await user.click(screen.getByRole('button', { name: 'Start run' }));
    expect(await screen.findByText('topic is required.')).toBeInTheDocument();
    expect(fetchMock.calls.some((c) => c.key === `POST ${R}`)).toBe(false);
  });

  it('preselects the agent from ?agent= and omits variables the agent maps from the input', async () => {
    search = 'agent=ag-1';
    const fetchMock = setup(undefined, { variables_map: { message: '{{input}}' } });
    renderWithClient(<RunConsole />);
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText('Input'), 'hello');
    await user.type(await screen.findByLabelText(/topic/), 'billing');
    expect(screen.queryByLabelText(/message/)).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Start run' }));
    const post = await waitFor(() => {
      const c = fetchMock.calls.find((x) => x.key === `POST ${R}`);
      expect(c).toBeDefined();
      return c!;
    });
    expect(JSON.parse(post.init.body as string).variables).toEqual({ topic: 'billing' });
  });
});
