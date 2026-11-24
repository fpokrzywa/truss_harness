import { act, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { DRAFT, ONE, ORG, request, revision, stubAppgen } from '@/test/appgen';
import { ALL_PERMS, bodyOf, list } from '@/test/security';
import { jsonResponse, problemResponse, renderWithClient } from '@/test/utils';
import { AppRequestView } from './AppRequestView';

const nav = vi.hoisted(() => ({ query: 'ws=eng&proj=api' }));
vi.mock('next/navigation', () => ({
  useParams: () => ({ org: 'acme' }),
  useSearchParams: () => new URLSearchParams(nav.query),
  usePathname: () => '/o/acme/solutions/generate/ar-1',
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));
beforeEach(() => {
  nav.query = 'ws=eng&proj=api';
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

const SQL_ERROR = {
  path: 'tools.0.sql',
  code: 'sql_write_statement',
  message: 'The SQL must only read.',
  severity: 'error' as const,
};
const gets = (fetchMock: ReturnType<typeof stubAppgen>) =>
  fetchMock.calls.filter((c) => c.key === `GET ${ONE}`).length;
/** Waits for the permissions: the editor is editable once they load. */
const editable = () => screen.findByRole('button', { name: 'Save as new revision' });
const toolGroup = () => screen.getByRole('group', { name: /Tool 1/ });

describe('AppRequestView states', () => {
  it('shows the loading text, then the request', async () => {
    stubAppgen();
    renderWithClient(<AppRequestView id="ar-1" />);
    expect(screen.getByText('Loading app request...')).toBeInTheDocument();
    expect(await screen.findByRole('heading', { name: 'App request' })).toBeInTheDocument();
    expect(screen.getByText('An app for regional sales managers.')).toBeInTheDocument();
    expect(screen.getByText('main.sales.orders')).toBeInTheDocument();
  });

  it('shows the no-access view for 403 and 404', async () => {
    for (const status of [403, 404]) {
      stubAppgen({ [`GET ${ONE}`]: () => problemResponse(status, 'not_found') });
      const { unmount } = renderWithClient(<AppRequestView id="ar-1" />);
      expect(
        await screen.findByRole('heading', { name: 'Not found or no access' }),
      ).toBeInTheDocument();
      unmount();
    }
  });

  it('shows an error message for a 500', async () => {
    stubAppgen({ [`GET ${ONE}`]: () => problemResponse(500, 'internal_error') });
    renderWithClient(<AppRequestView id="ar-1" />);
    expect(await screen.findByRole('alert')).toHaveTextContent('internal_error');
  });

  it('says there is no draft yet while the request is queued', async () => {
    stubAppgen({
      [`GET ${ONE}`]: () =>
        jsonResponse(request({ status: 'queued', latest_revision_no: null, revisions: [] })),
    });
    renderWithClient(<AppRequestView id="ar-1" />);
    expect(await screen.findByText('No draft revisions yet.')).toBeInTheDocument();
    expect(screen.getByText('Queued...')).toBeInTheDocument();
    expect(screen.queryByRole('form', { name: 'Accept draft' })).not.toBeInTheDocument();
  });
});

describe('AppRequestView polling', () => {
  it('refetches a drafting request after 3 s and stops once it is drafted', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let n = 0;
    const fetchMock = stubAppgen({
      [`GET ${ONE}`]: () => {
        n += 1;
        return jsonResponse(n === 1 ? request({ status: 'drafting', revisions: [] }) : request());
      },
    });
    renderWithClient(<AppRequestView id="ar-1" />);
    expect(await screen.findByText('Drafting...')).toBeInTheDocument();
    expect(gets(fetchMock)).toBe(1);
    await act(() => vi.advanceTimersByTimeAsync(3000));
    expect(await screen.findByRole('form', { name: 'Draft revision 1' })).toBeInTheDocument();
    expect(gets(fetchMock)).toBe(2);
    expect(screen.queryByText('Drafting...')).not.toBeInTheDocument();
    await act(() => vi.advanceTimersByTimeAsync(30_000));
    expect(gets(fetchMock)).toBe(2);
  });

  it('stops polling a request that stays queued after 10 minutes', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const fetchMock = stubAppgen({
      [`GET ${ONE}`]: () => jsonResponse(request({ status: 'queued', revisions: [] })),
    });
    renderWithClient(<AppRequestView id="ar-1" />);
    expect(await screen.findByText('Queued...')).toBeInTheDocument();
    await act(() => vi.advanceTimersByTimeAsync(9 * 60 * 1000));
    expect(gets(fetchMock)).toBeGreaterThan(100);
    expect(screen.getByText('Queued...')).toBeInTheDocument();
    await act(() => vi.advanceTimersByTimeAsync(60 * 1000 + 100));
    expect(await screen.findByText('Still working; refresh to check again')).toBeInTheDocument();
    const stopped = gets(fetchMock);
    await act(() => vi.advanceTimersByTimeAsync(60_000));
    expect(gets(fetchMock)).toBe(stopped);
  });
});

describe('AppRequestView draft editor', () => {
  it('places a diagnostic next to the field its path names and blocks Accept', async () => {
    stubAppgen({
      [`GET ${ONE}`]: () =>
        jsonResponse(
          request({
            revisions: [
              revision({
                validation: [
                  SQL_ERROR,
                  {
                    path: 'agent.name',
                    code: 'duplicate_name',
                    message: 'taken',
                    severity: 'warning',
                  },
                ],
              }),
            ],
          }),
        ),
    });
    renderWithClient(<AppRequestView id="ar-1" />);
    const sql = within(await screen.findByRole('group', { name: /Tool 1/ })).getByLabelText('SQL');
    expect(sql).toHaveAccessibleDescription(/Error sql_write_statement: The SQL must only read\./);
    expect(within(toolGroup()).getByText('The SQL must only read.', { exact: false }));
    const top = screen.getByRole('region', { name: 'Draft diagnostics' });
    expect(top).toHaveTextContent('Warning duplicate_name: taken');
    expect(top).not.toHaveTextContent('sql_write_statement');
    const accept = await screen.findByRole('button', { name: 'Accept' });
    expect(accept).toBeDisabled();
    expect(accept).toHaveAccessibleDescription(/Fix the 1 error/);
  });

  it('places screen input and assertion diagnostics under their fields', async () => {
    stubAppgen({
      [`GET ${ONE}`]: () =>
        jsonResponse(
          request({
            revisions: [
              revision({
                validation: [
                  {
                    path: 'screens.1.inputs.0',
                    code: 'screen_input_unknown',
                    message: 'no',
                    severity: 'error',
                  },
                  {
                    path: 'evaluation_cases.0.assertions.0.value',
                    code: 'case_trivial',
                    message: 'trivial',
                    severity: 'warning',
                  },
                ],
              }),
            ],
          }),
        ),
    });
    renderWithClient(<AppRequestView id="ar-1" />);
    const table = await screen.findByRole('group', { name: 'Screen 2 (table)' });
    expect(within(table).getByLabelText('Inputs')).toHaveAccessibleDescription(
      /screen_input_unknown/,
    );
    const evalCase = screen.getByRole('group', { name: /Evaluation case 1/ });
    expect(within(evalCase).getByLabelText('Assertions (JSON)')).toHaveAccessibleDescription(
      /Warning case_trivial: trivial/,
    );
    expect(screen.queryByRole('region', { name: 'Draft diagnostics' })).not.toBeInTheDocument();
  });

  it('saves an edit as a new revision on the latest revision', async () => {
    const two = request({
      latest_revision_no: 2,
      revisions: [revision(), revision({ revision_no: 2, source: 'edited' })],
    });
    const fetchMock = stubAppgen({
      [`GET ${ONE}`]: () => jsonResponse(two),
      [`POST ${ONE}/drafts`]: () =>
        jsonResponse(revision({ revision_no: 3, source: 'edited' }), { status: 201 }),
    });
    renderWithClient(<AppRequestView id="ar-1" />);
    await editable();
    const instructions = screen.getByLabelText('Agent instructions');
    await userEvent.clear(instructions);
    await userEvent.type(instructions, 'Be brief.');
    await userEvent.click(
      screen.getByRole('button', { name: 'Add parameter to orders_by_region' }),
    );
    await userEvent.type(within(toolGroup()).getByLabelText('Parameter 2 name'), 'year');
    await userEvent.selectOptions(
      within(toolGroup()).getByLabelText('Parameter 2 type'),
      'integer',
    );
    const before = gets(fetchMock);
    await userEvent.click(screen.getByRole('button', { name: 'Save as new revision' }));
    await waitFor(() => expect(gets(fetchMock)).toBeGreaterThan(before));
    const body = bodyOf(fetchMock, `POST ${ONE}/drafts`) as {
      base_revision_no: number;
      draft: typeof DRAFT;
    };
    expect(body.base_revision_no).toBe(2);
    expect(body.draft.agent).toEqual({ name: 'sales-agent', instructions: 'Be brief.' });
    expect(body.draft.tools[0]!.parameters).toEqual([
      { name: 'region', type: 'string', required: true },
      { name: 'year', type: 'integer', required: true },
    ]);
    expect(body.draft.screens).toEqual(DRAFT.screens);
    expect(body.draft.evaluation_cases).toEqual(DRAFT.evaluation_cases);
  });

  it('blocks the save on assertions that are not JSON', async () => {
    const fetchMock = stubAppgen();
    renderWithClient(<AppRequestView id="ar-1" />);
    await editable();
    const field = screen.getByLabelText('Assertions (JSON)');
    await userEvent.clear(field);
    await userEvent.type(field, 'not json');
    await userEvent.click(screen.getByRole('button', { name: 'Save as new revision' }));
    expect(await screen.findByText('Enter the assertions as a JSON list.')).toBeInTheDocument();
    expect(fetchMock.calls.some((c) => c.key === `POST ${ONE}/drafts`)).toBe(false);
  });

  it('says the draft changed elsewhere on 409 stale_revision', async () => {
    stubAppgen({ [`POST ${ONE}/drafts`]: () => problemResponse(409, 'stale_revision') });
    renderWithClient(<AppRequestView id="ar-1" />);
    await userEvent.click(await editable());
    expect(await screen.findByText(/This draft changed elsewhere/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Reload' })).toBeInTheDocument();
  });

  it('shows other refusals of the save with their code', async () => {
    stubAppgen({ [`POST ${ONE}/drafts`]: () => problemResponse(422, 'validation_error') });
    renderWithClient(<AppRequestView id="ar-1" />);
    await userEvent.click(await editable());
    const form = screen.getByRole('form', { name: 'Draft revision 1' });
    expect(await within(form).findByRole('alert')).toHaveTextContent('validation_error');
  });

  it('lists every revision and shows an older one read-only', async () => {
    stubAppgen({
      [`GET ${ONE}`]: () =>
        jsonResponse(
          request({
            latest_revision_no: 2,
            revisions: [
              revision({ validation: [SQL_ERROR] }),
              revision({ revision_no: 2, source: 'edited' }),
            ],
          }),
        ),
    });
    renderWithClient(<AppRequestView id="ar-1" />);
    const history = await screen.findByRole('table', { name: 'Revision history' });
    const old = within(history).getByRole('button', { name: 'Show r1' }).closest('tr')!;
    expect(within(old).getByText('model')).toBeInTheDocument();
    expect(within(old).getAllByRole('cell')[3]).toHaveTextContent('1');
    await editable();
    expect(screen.getByLabelText('Agent instructions')).not.toHaveAttribute('readonly');
    await userEvent.click(within(history).getByRole('button', { name: 'Show r1' }));
    expect(await screen.findByRole('form', { name: 'Draft revision 1' })).toBeInTheDocument();
    expect(screen.getByLabelText('Agent instructions')).toHaveAttribute('readonly');
    expect(screen.queryByRole('button', { name: 'Save as new revision' })).not.toBeInTheDocument();
  });

  it('is read-only without appgen:request:write', async () => {
    stubAppgen({}, [...ALL_PERMS, 'appgen:request:read']);
    renderWithClient(<AppRequestView id="ar-1" />);
    expect(await screen.findByLabelText('Agent instructions')).toHaveAttribute('readonly');
    expect(screen.queryByRole('button', { name: 'Save as new revision' })).not.toBeInTheDocument();
    expect(screen.queryByRole('form', { name: 'Accept draft' })).not.toBeInTheDocument();
  });
});

describe('AppRequestView accept', () => {
  async function fillAccept() {
    const form = await screen.findByRole('form', { name: 'Accept draft' });
    await userEvent.type(within(form).getByLabelText('Solution name'), 'Sales app');
    await userEvent.selectOptions(within(form).getByLabelText('Runtime model'), 'm-1');
    await userEvent.type(within(form).getByLabelText('Min accuracy'), '0.8');
    await userEvent.type(within(form).getByLabelText('Max p95 latency (ms)'), '2500');
    await userEvent.type(within(form).getByLabelText('Max total cost (USD)'), '1.25');
    return form;
  }

  it('posts the latest revision with decimal-string thresholds', async () => {
    const fetchMock = stubAppgen({
      [`POST ${ONE}/accept`]: () => jsonResponse(request({ status: 'accepting' })),
    });
    renderWithClient(<AppRequestView id="ar-1" />);
    const form = await fillAccept();
    await userEvent.click(within(form).getByRole('button', { name: 'Accept' }));
    await waitFor(() =>
      expect(fetchMock.calls.some((c) => c.key === `POST ${ONE}/accept`)).toBe(true),
    );
    expect(bodyOf(fetchMock, `POST ${ONE}/accept`)).toEqual({
      revision_no: 1,
      solution_name: 'Sales app',
      model_id: 'm-1',
      thresholds: { min_accuracy: '0.8', max_p95_latency_ms: 2500, max_total_cost_usd: '1.25' },
    });
  });

  it('checks the thresholds on the client', async () => {
    const fetchMock = stubAppgen();
    renderWithClient(<AppRequestView id="ar-1" />);
    const form = await screen.findByRole('form', { name: 'Accept draft' });
    await userEvent.type(within(form).getByLabelText('Min accuracy'), '1.5');
    await userEvent.type(within(form).getByLabelText('Max p95 latency (ms)'), '0');
    await userEvent.click(within(form).getByRole('button', { name: 'Accept' }));
    expect(within(form).getByText('Name must be 1 to 200 characters.')).toBeInTheDocument();
    expect(within(form).getByText('Enter a decimal between 0 and 1.')).toBeInTheDocument();
    expect(
      within(form).getByText('Enter a positive whole number of milliseconds.'),
    ).toBeInTheDocument();
    expect(within(form).getByText('Enter a decimal amount in USD.')).toBeInTheDocument();
    expect(fetchMock.calls.some((c) => c.key === `POST ${ONE}/accept`)).toBe(false);
  });

  it.each([
    [409, 'request_not_acceptable'],
    [422, 'draft_invalid'],
    [403, 'forbidden'],
  ])('shows a %i %s refusal', async (status, code) => {
    stubAppgen({ [`POST ${ONE}/accept`]: () => problemResponse(status, code) });
    renderWithClient(<AppRequestView id="ar-1" />);
    const form = await fillAccept();
    await userEvent.click(within(form).getByRole('button', { name: 'Accept' }));
    const alert = await within(form).findByRole('alert');
    expect(alert).toHaveTextContent('Could not accept the draft');
    expect(alert).toHaveTextContent(code);
  });

  it('resumes a failed accept step with the accepted revision', async () => {
    const fetchMock = stubAppgen({
      [`GET ${ONE}`]: () =>
        jsonResponse(
          request({
            status: 'accepting',
            status_reason: 'publishing the tools failed',
            accepted_revision_no: 1,
            latest_revision_no: 2,
            revisions: [revision(), revision({ revision_no: 2, validation: [SQL_ERROR] })],
          }),
        ),
      [`POST ${ONE}/accept`]: () => jsonResponse(request({ status: 'accepting' })),
    });
    renderWithClient(<AppRequestView id="ar-1" />);
    const form = await fillAccept();
    const button = within(form).getByRole('button', { name: 'Resume accept' });
    expect(button).toBeEnabled();
    await userEvent.click(button);
    await waitFor(() =>
      expect(fetchMock.calls.some((c) => c.key === `POST ${ONE}/accept`)).toBe(true),
    );
    expect(bodyOf(fetchMock, `POST ${ONE}/accept`).revision_no).toBe(1);
  });

  it('links an accepted request to its solution', async () => {
    stubAppgen({
      [`GET ${ONE}`]: () =>
        jsonResponse(
          request({
            status: 'accepted',
            accepted_revision_no: 1,
            solution_version_id: 'sv-7',
            accept_progress: { solution_id: 's-7', solution_version_id: 'sv-7' },
          }),
        ),
    });
    renderWithClient(<AppRequestView id="ar-1" />);
    const accepted = await screen.findByRole('region', { name: 'Accepted' });
    expect(within(accepted).getByRole('link', { name: 'Open the solution' })).toHaveAttribute(
      'href',
      '/o/acme/solutions/s-7?ws=eng&proj=api',
    );
    expect(screen.queryByRole('form', { name: 'Accept draft' })).not.toBeInTheDocument();
  });

  it('shows the solution version id when the link names no project', async () => {
    nav.query = '';
    stubAppgen({
      [`GET ${ONE}`]: () =>
        jsonResponse(
          request({
            status: 'accepted',
            solution_version_id: 'sv-7',
            accept_progress: { solution_id: 's-7' },
          }),
        ),
    });
    renderWithClient(<AppRequestView id="ar-1" />);
    const accepted = await screen.findByRole('region', { name: 'Accepted' });
    expect(within(accepted).queryByRole('link')).not.toBeInTheDocument();
    expect(within(accepted).getByText('sv-7')).toBeInTheDocument();
  });
});

describe('AppRequestView unsaved edits and fixed names', () => {
  const UNSAVED = /Save your draft edits as a new revision before accepting/;
  const FIXED_TEXT = 'This editor cannot change the agent name, tool names or case ids';
  const FIXED_REASON = /discard this request and generate a new one/;

  it('blocks Accept while the draft has unsaved edits, and not after a revert', async () => {
    stubAppgen();
    renderWithClient(<AppRequestView id="ar-1" />);
    await editable();
    const accept = await screen.findByRole('button', { name: 'Accept' });
    expect(accept).toBeEnabled();
    const instructions = screen.getByLabelText('Agent instructions');
    await userEvent.type(instructions, ' More.');
    expect(accept).toBeDisabled();
    expect(accept).toHaveAccessibleDescription(UNSAVED);
    expect(screen.getByText(UNSAVED)).toHaveTextContent('Accept uses the latest saved revision.');
    await userEvent.clear(instructions);
    await userEvent.type(instructions, DRAFT.agent.instructions);
    expect(accept).toBeEnabled();
    expect(screen.queryByText(UNSAVED)).not.toBeInTheDocument();
  });

  it('enables Accept again once the edit is saved as a new revision', async () => {
    let saved = false;
    const fetchMock = stubAppgen({
      [`GET ${ONE}`]: () =>
        jsonResponse(
          saved
            ? request({
                latest_revision_no: 2,
                revisions: [revision(), revision({ revision_no: 2, source: 'edited' })],
              })
            : request(),
        ),
      [`POST ${ONE}/drafts`]: () => {
        saved = true;
        return jsonResponse(revision({ revision_no: 2, source: 'edited' }), { status: 201 });
      },
    });
    renderWithClient(<AppRequestView id="ar-1" />);
    await editable();
    await userEvent.type(screen.getByLabelText('Agent instructions'), ' More.');
    expect(screen.getByRole('button', { name: 'Accept' })).toBeDisabled();
    await userEvent.click(screen.getByRole('button', { name: 'Save as new revision' }));
    expect(await screen.findByRole('form', { name: 'Draft revision 2' })).toBeInTheDocument();
    expect(fetchMock.calls.some((c) => c.key === `POST ${ONE}/drafts`)).toBe(true);
    await waitFor(() => expect(screen.queryByText(UNSAVED)).not.toBeInTheDocument());
    expect(screen.getByRole('button', { name: 'Accept' })).toBeEnabled();
    expect(screen.getByRole('form', { name: 'Accept draft' })).toHaveTextContent('r2');
  });

  it('shows both reasons when the draft has errors and unsaved edits', async () => {
    stubAppgen({
      [`GET ${ONE}`]: () =>
        jsonResponse(request({ revisions: [revision({ validation: [SQL_ERROR] })] })),
    });
    renderWithClient(<AppRequestView id="ar-1" />);
    await editable();
    await userEvent.type(screen.getByLabelText('Agent instructions'), ' More.');
    const accept = screen.getByRole('button', { name: 'Accept' });
    expect(accept).toHaveAccessibleDescription(/Fix the 1 error/);
    expect(accept).toHaveAccessibleDescription(UNSAVED);
  });

  it('explains a diagnostic on a tool name and keeps Accept blocked', async () => {
    stubAppgen({
      [`GET ${ONE}`]: () =>
        jsonResponse(
          request({
            revisions: [
              revision({
                validation: [
                  {
                    path: 'tools.0.name',
                    code: 'duplicate_name',
                    message: "tool name 'orders_by_region' is repeated",
                    severity: 'error',
                  },
                ],
              }),
            ],
          }),
        ),
    });
    renderWithClient(<AppRequestView id="ar-1" />);
    const top = await screen.findByRole('region', { name: 'Draft diagnostics' });
    expect(top).toHaveTextContent('duplicate_name');
    expect(top).toHaveTextContent(FIXED_TEXT);
    expect(top).toHaveTextContent('generate a new one with a revised brief');
    const accept = await screen.findByRole('button', { name: 'Accept' });
    expect(accept).toBeDisabled();
    expect(accept).toHaveAccessibleDescription(/Fix the 1 error/);
    expect(accept).toHaveAccessibleDescription(FIXED_REASON);
  });

  it('shows neither fixed-name text for an error on an editable path', async () => {
    stubAppgen({
      [`GET ${ONE}`]: () =>
        jsonResponse(request({ revisions: [revision({ validation: [SQL_ERROR] })] })),
    });
    renderWithClient(<AppRequestView id="ar-1" />);
    const accept = await screen.findByRole('button', { name: 'Accept' });
    expect(accept).toBeDisabled();
    expect(accept).toHaveAccessibleDescription(/Fix the 1 error/);
    expect(accept).not.toHaveAccessibleDescription(FIXED_REASON);
    expect(screen.queryByText(new RegExp(FIXED_TEXT))).not.toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Draft diagnostics' })).not.toBeInTheDocument();
  });

  it('shows the empty state of the runtime model list', async () => {
    stubAppgen({ [`GET ${ORG}/models/effective`]: () => list([]) });
    renderWithClient(<AppRequestView id="ar-1" />);
    const form = await screen.findByRole('form', { name: 'Accept draft' });
    expect(await within(form).findByText(/No models are available here/)).toBeInTheDocument();
    expect(within(form).getByLabelText('Runtime model')).toBeDisabled();
  });
});

describe('AppRequestView discard and retry', () => {
  const FAILED = () =>
    jsonResponse(request({ status: 'failed', status_reason: 'model timed out', revisions: [] }));

  it('retries a failed request without asking', async () => {
    const fetchMock = stubAppgen({
      [`GET ${ONE}`]: FAILED,
      [`POST ${ONE}/retry`]: () => jsonResponse(request({ status: 'queued' }), { status: 202 }),
    });
    renderWithClient(<AppRequestView id="ar-1" />);
    expect(await screen.findByText('model timed out')).toBeInTheDocument();
    await userEvent.click(await screen.findByRole('button', { name: 'Retry' }));
    await waitFor(() =>
      expect(fetchMock.calls.some((c) => c.key === `POST ${ONE}/retry`)).toBe(true),
    );
  });

  it('discards after a confirmation', async () => {
    const fetchMock = stubAppgen({
      [`GET ${ONE}`]: FAILED,
      [`POST ${ONE}/discard`]: () => jsonResponse(request({ status: 'discarded' })),
    });
    renderWithClient(<AppRequestView id="ar-1" />);
    await userEvent.click(await screen.findByRole('button', { name: 'Discard' }));
    expect(fetchMock.calls.some((c) => c.key === `POST ${ONE}/discard`)).toBe(false);
    const dialog = screen.getByRole('dialog', { name: 'Discard request?' });
    await userEvent.click(within(dialog).getByRole('button', { name: 'Discard request' }));
    await waitFor(() =>
      expect(fetchMock.calls.some((c) => c.key === `POST ${ONE}/discard`)).toBe(true),
    );
  });

  it('shows a 409 of retry and of discard', async () => {
    stubAppgen({
      [`GET ${ONE}`]: FAILED,
      [`POST ${ONE}/retry`]: () => problemResponse(409, 'request_not_failed'),
      [`POST ${ONE}/discard`]: () => problemResponse(409, 'request_not_discardable'),
    });
    renderWithClient(<AppRequestView id="ar-1" />);
    await userEvent.click(await screen.findByRole('button', { name: 'Retry' }));
    expect(await screen.findByText('request_not_failed')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Discard' }));
    await userEvent.click(
      within(screen.getByRole('dialog', { name: 'Discard request?' })).getByRole('button', {
        name: 'Discard request',
      }),
    );
    expect(await screen.findByText('request_not_discardable')).toBeInTheDocument();
  });

  it('offers Discard but not Retry on a drafted request', async () => {
    stubAppgen();
    renderWithClient(<AppRequestView id="ar-1" />);
    expect(await screen.findByRole('button', { name: 'Discard' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument();
  });

  it('has neither button without appgen:request:write', async () => {
    stubAppgen({ [`GET ${ONE}`]: FAILED }, [...ALL_PERMS, 'appgen:request:read']);
    renderWithClient(<AppRequestView id="ar-1" />);
    expect(await screen.findByText('model timed out')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Discard' })).not.toBeInTheDocument();
  });
});
