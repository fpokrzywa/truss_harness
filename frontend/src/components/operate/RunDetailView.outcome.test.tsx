import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { approval, OPERATE_PERMS, run, stubOperate } from '@/test/operate';
import { list } from '@/test/security';
import { jsonResponse, problemResponse, renderWithClient } from '@/test/utils';
import { RunDetailView } from './RunDetailView';

const nav = vi.hoisted(() => ({ search: '' }));
vi.mock('next/navigation', () => ({
  useParams: () => ({ org: 'acme' }),
  useSearchParams: () => new URLSearchParams(nav.search),
}));
afterEach(() => {
  vi.unstubAllGlobals();
  nav.search = '';
});

const R = '/api/v1/orgs/acme/runs/run-1';
const A = '/api/v1/orgs/acme/approvals';
const empty = {
  [`GET ${R}/steps`]: () => list([]),
  [`GET ${R}/events`]: () => list([]),
};
const unknownRun = (over = {}) =>
  run({
    id: 'run-1',
    status: 'cancelled',
    outcome_unknown: true,
    error_class: 'indeterminate_side_effect',
    error_message: 'a write may have been delivered before this run ended (cancelled)',
    deadline_at: '2099-01-01T00:00:00Z',
    ...over,
  });
const pendingAp = approval({ id: 'ap-9', reason: 'indeterminate_side_effect', tool: null });
const withApproval = {
  [`GET ${A}`]: () => list([pendingAp]),
  [`GET ${A}/ap-9`]: () => jsonResponse(pendingAp),
};

describe('RunDetailView: unknown write outcome (R1)', () => {
  it.each(['cancelled', 'timed_out', 'failed'] as const)(
    'marks a %s run as write outcome unknown, not as the plain status',
    async (status) => {
      stubOperate({
        ...empty,
        ...withApproval,
        [`GET ${R}`]: () => jsonResponse(unknownRun({ status })),
      });
      renderWithClient(<RunDetailView id="run-1" />);
      expect(await screen.findByLabelText('Run status: Write outcome unknown')).toBeInTheDocument();
      expect(screen.queryByLabelText(/Run status: (Cancelled|Timed out|Failed)/)).toBeNull();
      const panel = await screen.findByRole('region', { name: 'Failure details' });
      expect(panel).toHaveTextContent('a write may have been delivered');
    },
  );

  it('offers only mark_succeeded and fail on an ended run, with a link to the approval', async () => {
    stubOperate({ ...empty, ...withApproval, [`GET ${R}`]: () => jsonResponse(unknownRun()) });
    renderWithClient(<RunDetailView id="run-1" />);
    const section = await screen.findByRole('region', { name: 'Approval detail' });
    expect(within(section).getByRole('button', { name: 'Mark succeeded' })).toBeInTheDocument();
    expect(within(section).getByRole('button', { name: 'Fail' })).toBeInTheDocument();
    expect(within(section).queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Open in the approval queue' })).toHaveAttribute(
      'href',
      '/o/acme/approvals',
    );
  });

  it('offers retry too for a run still waiting within its deadline', async () => {
    stubOperate({
      ...empty,
      ...withApproval,
      [`GET ${R}`]: () => jsonResponse(unknownRun({ status: 'waiting_for_approval' })),
    });
    renderWithClient(<RunDetailView id="run-1" />);
    const section = await screen.findByRole('region', { name: 'Approval detail' });
    expect(within(section).getByRole('button', { name: 'Retry' })).toBeInTheDocument();
  });

  it('withholds retry for a waiting run past its deadline', async () => {
    stubOperate({
      ...empty,
      ...withApproval,
      [`GET ${R}`]: () =>
        jsonResponse(
          unknownRun({ status: 'waiting_for_approval', deadline_at: '2020-01-01T00:00:00Z' }),
        ),
    });
    renderWithClient(<RunDetailView id="run-1" />);
    const section = await screen.findByRole('region', { name: 'Approval detail' });
    expect(within(section).queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument();
    expect(within(section).getByRole('button', { name: 'Fail' })).toBeInTheDocument();
  });

  it('requires a comment and surfaces a server 422 message', async () => {
    const msg = 'the run has ended or is past its deadline, so a retry is not possible';
    const fetchMock = stubOperate({
      ...empty,
      ...withApproval,
      [`GET ${R}`]: () => jsonResponse(unknownRun()),
      [`POST ${A}/ap-9/decide`]: () => problemResponse(422, 'validation_error', { detail: msg }),
    });
    renderWithClient(<RunDetailView id="run-1" />);
    const user = userEvent.setup();
    const section = await screen.findByRole('region', { name: 'Approval detail' });
    expect(within(section).getByRole('button', { name: 'Fail' })).toBeDisabled();
    await user.type(within(section).getByLabelText('Comment (required)'), 'checked CRM');
    await user.click(within(section).getByRole('button', { name: 'Fail' }));
    const dlg = await screen.findByRole('dialog');
    await user.click(within(dlg).getByRole('button', { name: /Confirm Fail/ }));
    expect(await within(dlg).findByText(new RegExp(msg))).toBeInTheDocument();
    expect(fetchMock.calls.some((c) => c.key === `POST ${A}/ap-9/decide`)).toBe(true);
  });

  it('shows the verdict, no marker and no decision form once answered', async () => {
    stubOperate({
      ...empty,
      [`GET ${R}`]: () =>
        jsonResponse(
          unknownRun({
            outcome_unknown: false,
            error_message:
              'the run ended while a write outcome was unknown; an operator confirmed the write succeeded',
          }),
        ),
    });
    renderWithClient(<RunDetailView id="run-1" />);
    expect(await screen.findByLabelText('Run status: Cancelled')).toBeInTheDocument();
    expect(screen.queryByText('Write outcome unknown')).not.toBeInTheDocument();
    const panel = await screen.findByRole('region', { name: 'Failure details' });
    expect(panel).toHaveTextContent('an operator confirmed the write succeeded');
    expect(screen.queryByRole('region', { name: 'Approval detail' })).not.toBeInTheDocument();
  });

  it('labels an indeterminate step as outcome unknown, not failed', async () => {
    stubOperate({
      ...empty,
      ...withApproval,
      [`GET ${R}`]: () => jsonResponse(unknownRun()),
      [`GET ${R}/steps`]: () =>
        list([
          {
            id: 's1',
            run_id: 'run-1',
            seq: 1,
            kind: 'tool_call',
            name: 'crm.update',
            status: 'indeterminate',
            attempt: 1,
            error_class: 'indeterminate_side_effect',
          },
        ]),
    });
    renderWithClient(<RunDetailView id="run-1" />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('tab', { name: 'Execution timeline' }));
    const step = await screen.findByTestId('timeline-step');
    expect(within(step).getAllByText(/Outcome unknown/).length).toBeGreaterThan(0);
    expect(step).not.toHaveTextContent(/\bfailed\b/i);
  });

  it('queries the whole tree and says when the decision belongs to a child run', async () => {
    const childAp = approval({
      id: 'ap-c',
      run_id: 'child-1',
      reason: 'indeterminate_side_effect',
      tool: null,
    });
    const fetchMock = stubOperate({
      ...empty,
      [`GET ${R}`]: () =>
        jsonResponse(
          unknownRun({
            status: 'waiting_for_approval',
            error_class: null,
            error_message: null,
            id: 'run-1',
          }),
        ),
      [`GET ${A}`]: () => list([childAp]),
      [`GET ${A}/ap-c`]: () => jsonResponse(childAp),
      'GET /api/v1/orgs/acme/runs/child-1': () =>
        jsonResponse(unknownRun({ id: 'child-1', status: 'cancelled' })),
    });
    renderWithClient(<RunDetailView id="run-1" />);
    const section = await screen.findByRole('region', { name: 'Approval detail' });
    const q = new URL(fetchMock.calls.find((c) => c.key === `GET ${A}`)?.url ?? '', 'http://x')
      .searchParams;
    expect(q.get('tree_run_id')).toBe('run-1');
    expect(q.has('run_id')).toBe(false);
    expect(screen.queryByText(/No open decision/)).not.toBeInTheDocument();
    expect(screen.getByText(/belongs to a child run/)).toBeInTheDocument();
    for (const l of screen.getAllByRole('link', { name: 'child-1' }))
      expect(l).toHaveAttribute('href', '/o/acme/runs/child-1');
    // Parent still waiting, child ended: retry is judged on the child, so it is not offered.
    expect(await within(section).findByRole('button', { name: 'Fail' })).toBeInTheDocument();
    expect(within(section).queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument();
  });

  it('shows the real status next to the marker for a parent that has not ended', async () => {
    stubOperate({
      ...empty,
      ...withApproval,
      [`GET ${R}`]: () =>
        jsonResponse(unknownRun({ status: 'running', error_class: null, error_message: null })),
    });
    renderWithClient(<RunDetailView id="run-1" />);
    const chip = await screen.findByLabelText(/Run status: Write outcome unknown/);
    expect(chip).toHaveTextContent('Write outcome unknown');
    expect(chip).toHaveTextContent('Running');
    expect(screen.queryByRole('region', { name: 'Failure details' })).not.toBeInTheDocument();
  });

  it('does not call a decided retry step unknown', async () => {
    stubOperate({
      ...empty,
      [`GET ${R}`]: () => jsonResponse(unknownRun({ outcome_unknown: false })),
      [`GET ${R}/steps`]: () =>
        list([
          {
            id: 's1',
            run_id: 'run-1',
            seq: 1,
            kind: 'tool_call',
            name: 'crm.update',
            status: 'indeterminate',
            attempt: 1,
          },
        ]),
    });
    renderWithClient(<RunDetailView id="run-1" />);
    await userEvent.setup().click(await screen.findByRole('tab', { name: 'Execution timeline' }));
    const step = await screen.findByTestId('timeline-step');
    expect(step).toHaveTextContent('Retry authorized');
    expect(step).not.toHaveTextContent('Outcome unknown');
  });

  it('shows the decision form to a caller whose tenant-level check fails but whose API call succeeds', async () => {
    stubOperate(
      { ...empty, ...withApproval, [`GET ${R}`]: () => jsonResponse(unknownRun()) },
      OPERATE_PERMS.filter((p) => p !== 'approval:read' && p !== 'approval:decide'),
    );
    renderWithClient(<RunDetailView id="run-1" />);
    const section = await screen.findByRole('region', { name: 'Approval detail' });
    expect(within(section).getByRole('button', { name: 'Fail' })).toBeInTheDocument();
    expect(screen.queryByText(/do not have permission/i)).not.toBeInTheDocument();
  });

  it('says no permission only when the server answers 403, and hides the queue link', async () => {
    stubOperate({
      ...empty,
      [`GET ${R}`]: () => jsonResponse(unknownRun()),
      [`GET ${A}`]: () => problemResponse(403, 'forbidden'),
    });
    renderWithClient(<RunDetailView id="run-1" />);
    const panel = await screen.findByRole('region', { name: 'Write outcome unknown' });
    expect(await within(panel).findByText(/do not have permission/i)).toBeInTheDocument();
    expect(within(panel).getByText(/decision is open/i)).toBeInTheDocument();
    expect(
      screen.queryByRole('link', { name: 'Open in the approval queue' }),
    ).not.toBeInTheDocument();
  });

  it('does not claim missing permission when the API lists no decision for the caller', async () => {
    stubOperate({
      ...empty,
      [`GET ${R}`]: () => jsonResponse(unknownRun()),
      [`GET ${A}`]: () => list([]),
    });
    renderWithClient(<RunDetailView id="run-1" />);
    const panel = await screen.findByRole('region', { name: 'Write outcome unknown' });
    expect(await within(panel).findByText(/none is visible to you/i)).toBeInTheDocument();
    expect(panel).not.toHaveTextContent(/permission/i);
  });

  it('surfaces a server 403 from the decide call', async () => {
    stubOperate({
      ...empty,
      ...withApproval,
      [`GET ${R}`]: () => jsonResponse(unknownRun()),
      [`POST ${A}/ap-9/decide`]: () =>
        problemResponse(403, 'forbidden', { detail: 'missing permission approval:decide' }),
    });
    renderWithClient(<RunDetailView id="run-1" />);
    const user = userEvent.setup();
    const section = await screen.findByRole('region', { name: 'Approval detail' });
    await user.type(within(section).getByLabelText('Comment (required)'), 'checked');
    await user.click(within(section).getByRole('button', { name: 'Fail' }));
    const dlg = await screen.findByRole('dialog');
    await user.click(within(dlg).getByRole('button', { name: /Confirm Fail/ }));
    expect(await within(dlg).findByText(/missing permission approval:decide/)).toBeInTheDocument();
  });

  it('gives the decision panel the anchor id used by node links', async () => {
    stubOperate({ ...empty, ...withApproval, [`GET ${R}`]: () => jsonResponse(unknownRun()) });
    renderWithClient(<RunDetailView id="run-1" />);
    const panel = await screen.findByRole('region', { name: 'Write outcome unknown' });
    expect(panel).toHaveAttribute('id', 'decision');
  });

  it.each([
    ['tab=timeline', 'Execution timeline'],
    ['tab=nodes', 'Nodes'],
    ['tab=overview', 'Overview'],
    ['tab=bogus', 'Overview'],
    ['', 'Overview'],
  ])('opens the tab named in the URL (%s)', async (search, tab) => {
    nav.search = search;
    stubOperate({
      ...empty,
      [`GET ${R}`]: () => jsonResponse(run({ id: 'run-1', kind: 'pipeline', status: 'completed' })),
      [`GET ${R}/nodes`]: () => list([]),
      [`GET ${R}/result`]: () => jsonResponse({ output: 'x' }),
    });
    renderWithClient(<RunDetailView id="run-1" />);
    expect(await screen.findByRole('tab', { name: tab, selected: true })).toBeInTheDocument();
  });

  it('labels a step left indeterminate after a retry as authorized, with the detail', async () => {
    stubOperate({
      ...empty,
      [`GET ${R}`]: () => jsonResponse(unknownRun({ outcome_unknown: false })),
      [`GET ${R}/steps`]: () =>
        list([
          {
            id: 's1',
            run_id: 'run-1',
            seq: 1,
            kind: 'tool_call',
            name: 'crm.update',
            status: 'indeterminate',
            attempt: 1,
          },
        ]),
    });
    renderWithClient(<RunDetailView id="run-1" />);
    await userEvent.setup().click(await screen.findByRole('tab', { name: 'Execution timeline' }));
    const step = await screen.findByTestId('timeline-step');
    expect(step).toHaveTextContent('Retry authorized');
    expect(step).toHaveTextContent(/never confirmed/);
    expect(step).not.toHaveTextContent('Outcome recorded');
  });
  it('describes the next step without pointing at an approval that may not be on the page', async () => {
    stubOperate({ ...empty, ...withApproval, [`GET ${R}`]: () => jsonResponse(unknownRun()) });
    renderWithClient(<RunDetailView id="run-1" />);
    const panel = await screen.findByRole('region', { name: 'Failure details' });
    expect(panel).not.toHaveTextContent('approval below');
  });

  it('does not print the raw waiting_reason token', async () => {
    stubOperate({
      ...empty,
      ...withApproval,
      [`GET ${R}`]: () =>
        jsonResponse(
          unknownRun({
            status: 'waiting_for_approval',
            waiting_reason: 'indeterminate_side_effect',
          }),
        ),
    });
    renderWithClient(<RunDetailView id="run-1" />);
    await screen.findByRole('region', { name: 'Write outcome unknown' });
    for (const el of screen.getAllByRole('status'))
      expect(el).not.toHaveTextContent('indeterminate_side_effect');
    expect(
      screen.getAllByText(/outcome is unknown until an operator decides/).length,
    ).toBeGreaterThan(0);
  });
});
