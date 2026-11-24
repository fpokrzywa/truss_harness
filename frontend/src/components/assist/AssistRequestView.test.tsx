import { act, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { AssistRequestOut, ProposalOut } from '@/lib/api/assist-types';
import {
  A,
  APP_PROPOSAL,
  EVAL_PROPOSAL,
  ORG,
  PLAN_PROPOSAL,
  refused,
  RELEASE_ID,
  REQ,
  REQ_ID,
  request,
  RESULT,
  RUN_ID,
  stubAssist,
  USER_PERMS,
} from '@/test/assist';
import { D, TARGET } from '@/test/delivery';
import { list } from '@/test/security';
import { jsonResponse, problemResponse, renderWithClient } from '@/test/utils';
import { AssistRequestView } from './AssistRequestView';

vi.mock('next/navigation', () => ({ useParams: () => ({ org: 'acme' }) }));
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

const GET = `GET ${REQ}`;
const run = (
  status: 'running' | 'waiting_for_approval' | 'completed' | 'failed',
  error_code: string | null = null,
) => ({ id: RUN_ID, status, error_code });
const serve = (r: AssistRequestOut) => ({ [GET]: () => jsonResponse(r) });
const count = (fetchMock: ReturnType<typeof stubAssist>, key: string) =>
  fetchMock.calls.filter((c) => c.key === key).length;
const view = () => renderWithClient(<AssistRequestView id={REQ_ID} />);
const card = (name: RegExp | string) => screen.findByRole('region', { name });

describe('AssistRequestView route', () => {
  it('shows the route decision, the context link and a link back', async () => {
    stubAssist(
      serve(
        request({
          requested_specialist: 'cost',
          candidates: ['cost', 'deployment'],
          context: { kind: 'release', id: RELEASE_ID },
        }),
      ),
    );
    view();
    const route = await card('Request');
    expect(within(route).getByText('Routed')).toBeVisible();
    await waitFor(() => expect(route).toHaveTextContent('Cost, Deployment'));
    expect(route).toHaveTextContent('Routing rule');
    expect(route).toHaveTextContent('assist.router/1');
    expect(within(route).getByRole('link')).toHaveAttribute(
      'href',
      `/o/acme/releases/${RELEASE_ID}`,
    );
    expect(await screen.findByRole('link', { name: 'Back to Ask SETH' })).toHaveAttribute(
      'href',
      '/o/acme/w/eng/p/api/assist',
    );
    expect(screen.queryByRole('link', { name: 'Ask again' })).not.toBeInTheDocument();
  });

  it('shows a refused request with its reason and an Ask again link carrying the context', async () => {
    stubAssist(
      serve(refused('route_unclear', { context: { kind: 'deployment', id: RELEASE_ID } })),
    );
    view();
    const route = await card('Request');
    expect(route).toHaveTextContent('SETH could not tell which specialist fits this request.');
    expect(within(route).getByText('route_unclear')).toBeVisible();
    expect(await screen.findByRole('link', { name: 'Ask again' })).toHaveAttribute(
      'href',
      `/o/acme/w/eng/p/api/assist?context=deployment:${RELEASE_ID}`,
    );
    expect(screen.getByText('No run was started for this request.')).toBeVisible();
  });

  it.each(['queued', 'classifying', 'handing_over'] as const)(
    'says the run of a %s request has not started yet',
    async (status) => {
      stubAssist(serve(request({ status, run: null, run_id: null })));
      view();
      const section = await card('Run');
      expect(section).toHaveTextContent('The run has not started yet');
      expect(section).not.toHaveTextContent('No run was started');
      expect(await card('Proposals')).toHaveTextContent('No proposals yet.');
    },
  );

  it('says no run was started for a failed request, and that there are no proposals', async () => {
    stubAssist(serve(refused('budget_denied', { status: 'failed' })));
    view();
    expect(await card('Run')).toHaveTextContent('No run was started for this request.');
    expect(await card('Proposals')).toHaveTextContent('No proposals.');
  });

  it('shows an unknown reason code once and offers Ask again on a failed request', async () => {
    stubAssist(serve(refused('budget_denied', { status: 'failed' })));
    view();
    expect(await screen.findAllByText('budget_denied')).toHaveLength(1);
    expect(await screen.findByRole('link', { name: 'Ask again' })).toHaveAttribute(
      'href',
      '/o/acme/w/eng/p/api/assist',
    );
  });

  it('hides project links and actions while the project is not resolved', async () => {
    stubAssist({
      ...serve(refused('route_unclear', { proposals: [EVAL_PROPOSAL] })),
      [`GET ${ORG}/workspaces`]: () => list([]),
    });
    view();
    await card(/Proposal 1/);
    expect(screen.queryByRole('link', { name: 'Ask again' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Accept/ })).not.toBeInTheDocument();
  });

  it('resolves an archived project', async () => {
    const fetchMock = stubAssist(serve(refused('route_unclear')));
    view();
    await screen.findByRole('link', { name: 'Ask again' });
    for (const path of ['/workspaces', '/workspaces/eng/projects'])
      expect(fetchMock.calls.find((c) => c.key === `GET ${ORG}${path}`)!.url).toContain(
        'include_archived=true',
      );
  });

  it('shows the no-access view on 404', async () => {
    stubAssist({ [GET]: () => problemResponse(404, 'not_found') });
    view();
    expect(await screen.findByText('Not found or no access')).toBeVisible();
  });
});

describe('AssistRequestView run', () => {
  it('polls while the run is running and shows the answer once it completed', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const answers = [request(), request(), request({ run: run('completed') })];
    const fetchMock = stubAssist({
      [GET]: () => jsonResponse(answers.length > 1 ? answers.shift() : answers[0]),
      [`GET ${RESULT}`]: () => jsonResponse({ output: 'Release **3** runs in prod.' }),
    });
    view();
    expect(await screen.findByText('The specialist is working...')).toBeVisible();
    await act(() => vi.advanceTimersByTimeAsync(3000));
    expect(count(fetchMock, GET)).toBe(2);
    expect(count(fetchMock, `GET ${RESULT}`)).toBe(0);
    await act(() => vi.advanceTimersByTimeAsync(3000));
    const answer = await card('Answer');
    expect((await within(answer).findByText('3')).tagName).toBe('STRONG');
    expect(count(fetchMock, GET)).toBe(3);
    await act(() => vi.advanceTimersByTimeAsync(30_000));
    expect(count(fetchMock, GET)).toBe(3);
    expect(screen.queryByText('The specialist is working...')).not.toBeInTheDocument();
  });

  it('keeps the request on screen when a poll fails', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let calls = 0;
    stubAssist({
      [GET]: () => (++calls === 1 ? jsonResponse(request()) : problemResponse(503, 'unavailable')),
    });
    view();
    await card('Request');
    await act(() => vi.advanceTimersByTimeAsync(3000));
    expect(await screen.findByText('Could not refresh the request')).toBeVisible();
    expect(await card('Request')).toBeVisible();
  });

  it('stops after 10 minutes and offers Refresh', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const fetchMock = stubAssist();
    view();
    await card('Request');
    await act(() => vi.advanceTimersByTimeAsync(10 * 60 * 1000 + 1000));
    expect(await screen.findByText(/stopped checking after 10 minutes/)).toBeVisible();
    const stopped = count(fetchMock, GET);
    await act(() => vi.advanceTimersByTimeAsync(30_000));
    expect(count(fetchMock, GET)).toBe(stopped);
    screen.getByRole('button', { name: 'Refresh' }).click();
    await waitFor(() => expect(count(fetchMock, GET)).toBe(stopped + 1));
  });

  it('links the run, and its cost only with usage:read', async () => {
    stubAssist();
    const { unmount } = view();
    expect(await screen.findByRole('link', { name: 'Cost of the run' })).toHaveAttribute(
      'href',
      `/o/acme/govern/runs/${RUN_ID}/cost`,
    );
    expect(screen.getByRole('link', { name: 'Open the run' })).toHaveAttribute(
      'href',
      `/o/acme/runs/${RUN_ID}`,
    );
    unmount();

    stubAssist({}, { org: ['usage:read'], project: USER_PERMS.filter((p) => p !== 'usage:read') });
    view();
    await screen.findByRole('link', { name: 'Back to Ask SETH' });
    expect(screen.queryByRole('link', { name: 'Cost of the run' })).not.toBeInTheDocument();
  });

  it('says a waiting run needs an approval', async () => {
    stubAssist(serve(request({ run: run('waiting_for_approval') })));
    view();
    expect(await screen.findByText(/The run waits for an approval/)).toBeVisible();
    expect(screen.getByRole('link', { name: 'Open the approval queue' })).toHaveAttribute(
      'href',
      '/o/acme/approvals',
    );
  });

  it('says a run that is gone cannot be shown', async () => {
    stubAssist(serve(request({ run: null })));
    view();
    expect(await screen.findByText(/The run cannot be shown/)).toBeVisible();
    expect(screen.queryByRole('link', { name: 'Open the run' })).not.toBeInTheDocument();
  });

  it('shows a result error', async () => {
    stubAssist({
      ...serve(request({ run: run('completed') })),
      [`GET ${RESULT}`]: () => problemResponse(404, 'not_found', { detail: 'result not found' }),
    });
    view();
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Could not load the answer');
    expect(alert).toHaveTextContent('result not found');
  });

  it('shows the failure of a failed run, whose open proposals stay decidable', async () => {
    const fetchMock = stubAssist(
      serve(request({ run: run('failed', 'timeout'), proposals: [EVAL_PROPOSAL] })),
    );
    view();
    const section = await card('Run');
    expect(within(section).getByText('Failed')).toBeVisible();
    expect(within(section).getByText('Timed out')).toBeVisible();
    expect(within(section).getByText('timeout')).toBeVisible();
    const p = await card(/Proposal 1/);
    expect(await within(p).findByRole('button', { name: 'Accept proposal 1' })).toBeEnabled();
    expect(within(p).getByRole('button', { name: 'Dismiss proposal 1' })).toBeEnabled();
    expect(count(fetchMock, `GET ${RESULT}`)).toBe(0);
  });
});

describe('AssistRequestView proposals', () => {
  const done = (proposals: ProposalOut[]) => request({ run: run('completed'), proposals });
  const result = { [`GET ${RESULT}`]: () => jsonResponse({ output: 'Done' }) };
  const targets = { [`GET ${D}/targets`]: () => list([TARGET]) };
  const accept = (id: string) => `POST ${A}/proposals/${id}/accept`;
  const dismiss = (id: string) => `POST ${A}/proposals/${id}/dismiss`;
  const clickAccept = async (region: HTMLElement) =>
    userEvent.click(await within(region).findByRole('button', { name: /^Accept/ }));

  it('shows each kind with its payload, in order', async () => {
    stubAssist({
      ...serve(done([APP_PROPOSAL, EVAL_PROPOSAL, PLAN_PROPOSAL])),
      ...result,
      ...targets,
    });
    view();
    const app = await card('Proposal 1: App request');
    expect(app).toHaveTextContent('An app for sales managers.');
    expect(within(app).getAllByRole('listitem')).toHaveLength(2);
    expect(within(app).getByRole('link', { name: 'conn-dbx' })).toHaveAttribute(
      'href',
      '/o/acme/connections/conn-dbx',
    );
    expect(app).toHaveTextContent('m-1');
    const evaluation = await card('Proposal 2: Evaluation run');
    expect(within(evaluation).getByRole('link')).toHaveAttribute(
      'href',
      `/o/acme/releases/${RELEASE_ID}`,
    );
    const plan = await card('Proposal 3: Delivery plan');
    expect(plan).toHaveTextContent('sales-app');
    expect(plan).toHaveTextContent('Sales app');
    expect(await within(plan).findByText('Analytics prod')).toBeVisible();
    expect(within(plan).getByRole('link')).toHaveAttribute(
      'href',
      `/o/acme/releases/${RELEASE_ID}`,
    );
  });

  it('renders markup in a payload as literal text', async () => {
    const evil = '<img src=x onerror=alert(1)>';
    stubAssist({
      ...serve(done([{ ...APP_PROPOSAL, payload: { ...APP_PROPOSAL.payload, brief: evil } }])),
      ...result,
    });
    const { container } = view();
    expect(await screen.findByText(evil)).toBeVisible();
    expect(container.querySelector('img')).toBeNull();
  });

  it.each([
    [APP_PROPOSAL, 'Open the app request', '/o/acme/solutions/generate/new-1?ws=eng&proj=api'],
    [EVAL_PROPOSAL, 'Open the evaluation run', '/o/acme/evaluations/new-1'],
    [PLAN_PROPOSAL, 'Open the plan', '/o/acme/deliver/plans/new-1'],
  ] as const)('accepting $kind links what it created', async (p, label, href) => {
    const fetchMock = stubAssist({
      ...serve(done([p])),
      ...result,
      ...targets,
      [accept(p.id)]: () =>
        jsonResponse({ ...p, status: 'accepted', result_ref: { kind: p.kind, id: 'new-1' } }),
    });
    view();
    const region = await card(/Proposal 1/);
    await clickAccept(region);
    expect(await within(region).findByRole('link', { name: label })).toHaveAttribute('href', href);
    expect(within(region).getByText('Accepted')).toBeVisible();
    expect(within(region).queryByRole('button')).not.toBeInTheDocument();
    expect(fetchMock.calls.find((c) => c.key === accept(p.id))!.init.body).toBe('{}');
    expect(count(fetchMock, GET)).toBe(1);
  });

  it('shows a refused accept as failed, with only Dismiss, and leaves the others alone', async () => {
    stubAssist({
      ...serve(done([EVAL_PROPOSAL, { ...EVAL_PROPOSAL, id: 'prop-2' }])),
      ...result,
      [accept(EVAL_PROPOSAL.id)]: () =>
        jsonResponse({ ...EVAL_PROPOSAL, status: 'failed', status_reason: 'release_withdrawn' }),
    });
    view();
    const region = await card(/Proposal 1/);
    await clickAccept(region);
    expect(await within(region).findByText('Failed')).toBeVisible();
    expect(within(region).getByText('release_withdrawn')).toBeVisible();
    expect(within(region).queryByText('Accepted')).not.toBeInTheDocument();
    expect(within(region).queryByRole('link', { name: /^Open the/ })).not.toBeInTheDocument();
    expect(
      within(region)
        .getAllByRole('button')
        .map((b) => b.textContent),
    ).toEqual(['Dismiss proposal 1']);
    const other = await card(/Proposal 2/);
    expect(within(other).getByText('Open')).toBeVisible();
    expect(within(other).getAllByRole('button')).toHaveLength(2);
  });

  it('says a proposal is no longer open, refetches, and keeps the message', async () => {
    const answers = [done([EVAL_PROPOSAL]), done([{ ...EVAL_PROPOSAL, status: 'dismissed' }])];
    const fetchMock = stubAssist({
      [GET]: () => jsonResponse(answers.length > 1 ? answers.shift() : answers[0]),
      ...result,
      [accept(EVAL_PROPOSAL.id)]: () => problemResponse(409, 'proposal_not_open'),
    });
    view();
    const region = await card(/Proposal 1/);
    await clickAccept(region);
    expect(await within(region).findByText('Dismissed')).toBeVisible();
    expect(within(region).queryByRole('button')).not.toBeInTheDocument();
    expect(within(region).getByRole('alert')).toHaveTextContent(/no longer open/);
    expect(count(fetchMock, GET)).toBe(2);
  });

  it('shows any other error', async () => {
    stubAssist({
      ...serve(done([EVAL_PROPOSAL])),
      ...result,
      [accept(EVAL_PROPOSAL.id)]: () =>
        problemResponse(403, 'forbidden', { detail: 'missing permission' }),
    });
    view();
    const region = await card(/Proposal 1/);
    await clickAccept(region);
    const alert = await within(region).findByRole('alert');
    expect(alert).toHaveTextContent('Could not decide the proposal');
    expect(alert).toHaveTextContent('missing permission');
    expect(within(region).getAllByRole('button', { name: /proposal 1/ })).toHaveLength(2);
  });

  it('dismisses a proposal', async () => {
    stubAssist({
      ...serve(done([EVAL_PROPOSAL])),
      ...result,
      [dismiss(EVAL_PROPOSAL.id)]: () => jsonResponse({ ...EVAL_PROPOSAL, status: 'dismissed' }),
    });
    view();
    const region = await card(/Proposal 1/);
    await userEvent.click(await within(region).findByRole('button', { name: /^Dismiss/ }));
    expect(await within(region).findByText('Dismissed')).toBeVisible();
    expect(within(region).queryByRole('button')).not.toBeInTheDocument();
  });

  it('sends one call for a second click while the first is in flight', async () => {
    const fetchMock = stubAssist({
      ...serve(done([EVAL_PROPOSAL])),
      ...result,
      [accept(EVAL_PROPOSAL.id)]: () => new Promise(() => {}),
    });
    view();
    const region = await card(/Proposal 1/);
    await userEvent.dblClick(await within(region).findByRole('button', { name: /^Accept/ }));
    for (const b of within(region).getAllByRole('button')) expect(b).toBeDisabled();
    expect(count(fetchMock, accept(EVAL_PROPOSAL.id))).toBe(1);
  });

  it('drops a poll that started before the decision was answered', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const stale: ((r: Response) => void)[] = [];
    let calls = 0;
    const open = request({ proposals: [EVAL_PROPOSAL] });
    stubAssist({
      [GET]: () =>
        ++calls === 1 ? jsonResponse(open) : new Promise<Response>((r) => stale.push(r)),
      [dismiss(EVAL_PROPOSAL.id)]: () => jsonResponse({ ...EVAL_PROPOSAL, status: 'dismissed' }),
    });
    view();
    const region = await card(/Proposal 1/);
    const button = await within(region).findByRole('button', { name: /^Dismiss/ });
    await act(() => vi.advanceTimersByTimeAsync(3000));
    expect(stale).toHaveLength(1);
    act(() => button.click());
    expect(await within(region).findByText('Dismissed')).toBeVisible();
    await act(async () => stale[0]!(jsonResponse(open)));
    expect(within(region).getByText('Dismissed')).toBeVisible();
  });

  it('has no buttons without assist:proposal:accept on the project', async () => {
    stubAssist(
      { ...serve(done([EVAL_PROPOSAL])), ...result },
      { org: ['assist:proposal:accept'], project: ['assist:request:read', 'usage:read'] },
    );
    view();
    await screen.findByRole('link', { name: 'Cost of the run' });
    expect(within(await card(/Proposal 1/)).queryByRole('button')).not.toBeInTheDocument();
  });

  it('never offers Accept on contents the user cannot see', async () => {
    stubAssist({ ...serve(done([{ ...EVAL_PROPOSAL, payload: null }])), ...result });
    view();
    const region = await card(/Proposal 1/);
    expect(
      within(region).getByText(/do not have access to this proposal's contents/),
    ).toBeVisible();
    expect(await within(region).findByRole('button', { name: /^Dismiss/ })).toBeVisible();
    expect(within(region).queryByRole('button', { name: /^Accept/ })).not.toBeInTheDocument();
  });

  it('says when the created object cannot be opened', async () => {
    stubAssist({
      ...serve(done([{ ...EVAL_PROPOSAL, status: 'accepted', result_ref: null }])),
      ...result,
    });
    view();
    const region = await card(/Proposal 1/);
    expect(
      within(region).getByText(/do not have access to what this proposal created/),
    ).toBeVisible();
    expect(within(region).queryByRole('link', { name: /^Open the/ })).not.toBeInTheDocument();
  });
});
