import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useSyncExternalStore } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  A,
  bodyOf,
  catalog,
  list,
  PA,
  refused,
  RELEASE_ID,
  REQ_ID,
  request,
  specialist,
  stubAssist,
  summary,
  USER_PERMS,
} from '@/test/assist';
import { jsonResponse, problemResponse, renderWithClient } from '@/test/utils';
import { AskSethView } from './AskSethView';

const nav = vi.hoisted(() => {
  const listeners = new Set<() => void>();
  const state = {
    query: '',
    push: vi.fn(),
    replace: vi.fn((href: string) => {
      state.query = href.split('?')[1] ?? '';
      listeners.forEach((l) => l());
    }),
    subscribe: (l: () => void) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
  };
  return state;
});
vi.mock('next/navigation', () => ({
  useParams: () => ({ org: 'acme', ws: 'eng', proj: 'api' }),
  useSearchParams: () =>
    new URLSearchParams(
      useSyncExternalStore(
        nav.subscribe,
        () => nav.query,
        () => nav.query,
      ),
    ),
  useRouter: () => ({ push: nav.push, replace: nav.replace }),
}));
beforeEach(() => {
  nav.query = '';
  nav.push.mockReset();
  nav.replace.mockClear();
});
afterEach(() => vi.unstubAllGlobals());

const CREATE = `POST ${PA}/requests`;
const LIST = `GET ${PA}/requests`;
const CONTEXT = `context=release:${RELEASE_ID}`;
const SETTINGS_HREF = '/o/acme/assist/settings?ws=eng&proj=api';

const created =
  (body: unknown, status = 201) =>
  () =>
    jsonResponse(body, { status });
const bodies = (fetchMock: ReturnType<typeof stubAssist>) =>
  fetchMock.calls.filter((c) => c.key === CREATE).map((c) => JSON.parse(String(c.init.body)));
const box = () => screen.findByLabelText('Request');
const send = () => userEvent.click(screen.getByRole('button', { name: 'Send' }));
const ask = async (text: string) => {
  await userEvent.type(await box(), text);
  await send();
};

describe('AskSethView request', () => {
  it.each([
    ['201 routed', request(), 201],
    ['202 queued', request({ status: 'queued', run: null, run_id: null }), 202],
    ['201 failed', request({ status: 'failed', status_reason: 'budget_denied' }), 201],
  ] as const)('opens the request on a %s answer', async (_name, answer, status) => {
    const fetchMock = stubAssist({ [CREATE]: created(answer, status) });
    renderWithClient(<AskSethView />);
    await ask('  Deploy release 3 ');
    await waitFor(() => expect(nav.push).toHaveBeenCalledWith(`/o/acme/assist/${REQ_ID}`));
    expect(bodies(fetchMock)).toEqual([{ text: 'Deploy release 3' }]);
    expect(screen.getByRole('button', { name: 'Sending...' })).toBeDisabled();
  });

  it('sends the chosen specialist and the context', async () => {
    nav.query = CONTEXT;
    const fetchMock = stubAssist({ [CREATE]: created(request()) });
    renderWithClient(<AskSethView />);
    await userEvent.selectOptions(await screen.findByLabelText('Specialist (optional)'), 'cost');
    await ask('What did it cost?');
    await waitFor(() => expect(nav.push).toHaveBeenCalled());
    expect(bodyOf(fetchMock, CREATE)).toEqual({
      text: 'What did it cost?',
      specialist: 'cost',
      context: { kind: 'release', id: RELEASE_ID },
    });
  });

  it('refuses empty and over-long text without a call', async () => {
    const fetchMock = stubAssist();
    renderWithClient(<AskSethView />);
    await ask('   ');
    expect(await screen.findByText('The request must be 1 to 4000 characters.')).toBeVisible();
    await userEvent.clear(await box());
    await userEvent.click(await box());
    await userEvent.paste('x'.repeat(4001));
    await send();
    expect(await box()).toHaveAttribute('aria-invalid', 'true');
    expect(bodies(fetchMock)).toEqual([]);
  });

  it('counts 4000 characters as the server does', async () => {
    const fetchMock = stubAssist({ [CREATE]: created(request()) });
    renderWithClient(<AskSethView />);
    await userEvent.click(await box());
    await userEvent.paste('\u{1F600}'.repeat(4000));
    await send();
    await waitFor(() => expect(bodies(fetchMock)).toHaveLength(1));
  });

  it('sends one request on a double click', async () => {
    const fetchMock = stubAssist({ [CREATE]: () => new Promise(() => {}) });
    renderWithClient(<AskSethView />);
    await userEvent.type(await box(), 'Deploy');
    await userEvent.dblClick(screen.getByRole('button', { name: 'Send' }));
    expect(await screen.findByRole('button', { name: 'Sending...' })).toBeDisabled();
    expect(bodies(fetchMock)).toHaveLength(1);
  });

  it('marks specialists that are not available here, and keeps them selectable', async () => {
    stubAssist({
      [`GET ${PA}/specialists`]: () =>
        jsonResponse([specialist('build'), specialist('cost', { available: false })]),
    });
    renderWithClient(<AskSethView />);
    const select = await screen.findByLabelText('Specialist (optional)');
    const options = within(select).getAllByRole<HTMLOptionElement>('option');
    expect(options.map((o) => o.textContent)).toEqual([
      'Let SETH choose',
      'Research (not available in this project)',
      'Solution design (not available in this project)',
      'Build',
      'Evaluation (not available in this project)',
      'Cost (not available in this project)',
      'Deployment (not available in this project)',
    ]);
    expect(options.every((o) => !o.disabled)).toBe(true);
  });
});

describe('AskSethView context', () => {
  it('shows the context and Remove drops it from the URL and the request', async () => {
    nav.query = CONTEXT;
    const fetchMock = stubAssist({ [CREATE]: created(request()) });
    renderWithClient(<AskSethView />);
    const about = await screen.findByText(/^About: release/);
    expect(about).toHaveTextContent(RELEASE_ID);
    await userEvent.click(screen.getByRole('button', { name: 'Remove the release' }));
    expect(nav.replace).toHaveBeenCalledWith('/o/acme/w/eng/p/api/assist');
    expect(screen.queryByText(/^About:/)).not.toBeInTheDocument();
    await ask('Hello');
    await waitFor(() => expect(bodies(fetchMock)).toEqual([{ text: 'Hello' }]));
  });

  it('ignores a context that is not a known kind with a UUID', async () => {
    nav.query = 'context=release:r-1';
    stubAssist();
    renderWithClient(<AskSethView />);
    await box();
    expect(screen.queryByText(/^About:/)).not.toBeInTheDocument();
  });

  it('shows 404 context not found and keeps the text and Remove', async () => {
    nav.query = CONTEXT;
    stubAssist({
      [CREATE]: () => problemResponse(404, 'not_found', { detail: 'context not found' }),
    });
    renderWithClient(<AskSethView />);
    await ask('Explain this');
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Could not send the request');
    expect(alert).toHaveTextContent('context not found');
    expect(await box()).toHaveValue('Explain this');
    expect(screen.getByRole('button', { name: 'Remove the release' })).toBeEnabled();
    expect(nav.push).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole('button', { name: 'Remove the release' }));
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(await box()).toHaveValue('Explain this');
  });

  it('clears a failed send when the text is edited', async () => {
    stubAssist({ [CREATE]: () => problemResponse(404, 'not_found') });
    renderWithClient(<AskSethView />);
    await ask('Explain this');
    await screen.findByRole('alert');
    await userEvent.type(await box(), '!');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});

describe('AskSethView refusals', () => {
  it('offers the candidates of an ambiguous request and resends it to the chosen one', async () => {
    nav.query = CONTEXT;
    const answers = [
      refused('route_ambiguous', { candidates: ['cost', 'deployment'] }),
      request({ requested_specialist: 'cost' }),
    ];
    const fetchMock = stubAssist({
      [CREATE]: () => jsonResponse(answers.shift(), { status: 201 }),
    });
    renderWithClient(<AskSethView />);
    await ask('What will it cost to deploy?');
    const panel = await screen.findByRole('alert');
    expect(panel).toHaveTextContent('More than one specialist fits this request.');
    const buttons = within(panel).getAllByRole('button');
    expect(buttons.map((b) => b.textContent)).toEqual(['Cost', 'Deployment']);
    expect(nav.push).not.toHaveBeenCalled();
    expect(await box()).toHaveValue('What will it cost to deploy?');

    await userEvent.click(buttons[0]!);
    await waitFor(() => expect(nav.push).toHaveBeenCalledWith(`/o/acme/assist/${REQ_ID}`));
    const context = { kind: 'release', id: RELEASE_ID };
    expect(bodies(fetchMock)).toEqual([
      { text: 'What will it cost to deploy?', context },
      { text: 'What will it cost to deploy?', specialist: 'cost', context },
    ]);
  });

  it('shows the refusal of a resent request in place of the first', async () => {
    const answers = [
      refused('route_ambiguous', { candidates: ['cost', 'deployment'] }),
      refused('specialist_disabled', { id: 'req-2', specialist_key: 'cost' }),
    ];
    stubAssist({ [CREATE]: () => jsonResponse(answers.shift(), { status: 201 }) });
    renderWithClient(<AskSethView />);
    await ask('Cost of the deploy');
    await userEvent.click(await screen.findByRole('button', { name: 'Cost' }));
    const panel = await screen.findByText(/is turned off/);
    expect(within(panel.closest('[role=alert]') as HTMLElement).queryAllByRole('button')).toEqual(
      [],
    );
    expect(screen.queryByText(/More than one specialist/)).not.toBeInTheDocument();
    expect(nav.push).not.toHaveBeenCalled();
  });

  it('offers all six specialists for an unclear request', async () => {
    stubAssist({ [CREATE]: created(refused('route_unclear')) });
    renderWithClient(<AskSethView />);
    await ask('Hello');
    const panel = await screen.findByRole('alert');
    expect(within(panel).getAllByRole('button')).toHaveLength(6);
  });

  it('lists the missing permissions', async () => {
    stubAssist({
      [CREATE]: created(
        refused('specialist_not_permitted', {
          missing_permissions: ['agent:execute', 'budget:read'],
        }),
      ),
    });
    renderWithClient(<AskSethView />);
    await ask('Budget?');
    const panel = await screen.findByRole('alert');
    expect(
      within(panel)
        .getAllByRole('listitem')
        .map((li) => li.textContent),
    ).toEqual(['agent:execute', 'budget:read']);
    expect(within(panel).queryAllByRole('button')).toEqual([]);
  });

  it.each(['specialists_not_installed', 'specialist_modified'])(
    'links %s to the settings only for a holder of assist:install',
    async (code) => {
      const routes = { [CREATE]: created(refused(code)) };
      stubAssist(routes, { project: [...USER_PERMS, 'assist:install'] });
      const { unmount } = renderWithClient(<AskSethView />);
      await ask('Budget?');
      let panel = await screen.findByRole('alert');
      expect(within(panel).getByRole('link', { name: 'Open the assist settings' })).toHaveAttribute(
        'href',
        SETTINGS_HREF,
      );
      expect(within(panel).queryAllByRole('button')).toEqual([]);
      unmount();

      stubAssist(routes, { org: ['assist:admin'] });
      renderWithClient(<AskSethView />);
      await ask('Budget?');
      panel = await screen.findByRole('alert');
      expect(panel).toHaveTextContent('Ask an administrator.');
      expect(within(panel).queryByRole('link')).not.toBeInTheDocument();
    },
  );

  it('clears the panel when the text changes or the context is removed', async () => {
    nav.query = CONTEXT;
    stubAssist({ [CREATE]: created(refused('route_unclear')) });
    renderWithClient(<AskSethView />);
    await ask('Hello');
    await screen.findByRole('alert');
    await userEvent.type(await box(), '!');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    await send();
    await screen.findByRole('alert');
    await userEvent.click(screen.getByRole('button', { name: 'Remove the release' }));
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(await box()).toHaveValue('Hello!');
  });
});

describe('AskSethView access', () => {
  it('has no form without assist:request:write, and still lists the requests', async () => {
    const fetchMock = stubAssist({}, { project: ['assist:request:read'] });
    renderWithClient(<AskSethView />);
    expect(await screen.findByText(/can view this project's requests but not ask/)).toBeVisible();
    expect(await screen.findByRole('table', { name: 'Requests' })).toBeVisible();
    expect(screen.queryByRole('form')).not.toBeInTheDocument();
    const url = fetchMock.calls.find((c) => c.key.endsWith('/me/permissions'))!.url;
    expect(url).toContain('workspace=eng');
    expect(url).toContain('project=api');
  });

  it('shows only the no-access view without read or write', async () => {
    const fetchMock = stubAssist({}, { project: [] });
    renderWithClient(<AskSethView />);
    expect(await screen.findByText('Not found or no access')).toBeVisible();
    expect(screen.queryByText(/can view this project's requests/)).not.toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Requests' })).not.toBeInTheDocument();
    expect(fetchMock.calls.some((c) => c.key === LIST)).toBe(false);
  });

  it('replaces the form with a notice while assist is off', async () => {
    const off = { [`GET ${A}/catalog`]: () => jsonResponse(catalog({ assist_enabled: false })) };
    stubAssist(off, { org: ['assist:admin'] });
    const { unmount } = renderWithClient(<AskSethView />);
    expect(await screen.findByText(/turned off for this organization/)).toBeVisible();
    expect(await screen.findByRole('link', { name: 'Open the assist settings' })).toHaveAttribute(
      'href',
      SETTINGS_HREF,
    );
    expect(screen.queryByRole('form')).not.toBeInTheDocument();
    unmount();

    stubAssist(off);
    renderWithClient(<AskSethView />);
    expect(await screen.findByText(/turned off.*Ask an administrator\./)).toBeVisible();
    expect(screen.queryByRole('link', { name: /settings/ })).not.toBeInTheDocument();
  });

  it('shows a notice with the form when no specialist is available here', async () => {
    stubAssist(
      { [`GET ${PA}/specialists`]: () => jsonResponse([]) },
      { project: [...USER_PERMS, 'assist:install'] },
    );
    renderWithClient(<AskSethView />);
    expect(await screen.findByText(/No specialist is available in this project/)).toBeVisible();
    expect(await screen.findByRole('link', { name: 'Open the assist settings' })).toHaveAttribute(
      'href',
      SETTINGS_HREF,
    );
    expect(screen.getByRole('form', { name: 'New request' })).toBeVisible();
  });
});

describe('AskSethView requests', () => {
  it('lists the requests with a link to each', async () => {
    stubAssist({
      [LIST]: () =>
        list([
          summary(),
          summary({
            id: 'req-2',
            status: 'refused',
            status_reason: 'route_unclear',
            specialist_key: null,
            route_method: null,
          }),
          summary({ id: 'req-3', status: 'failed', status_reason: 'budget_denied' }),
        ]),
    });
    renderWithClient(<AskSethView />);
    const table = await screen.findByRole('table', { name: 'Requests' });
    const rows = within(table).getAllByRole('row').slice(1);
    expect(rows[0]).toHaveTextContent('Routed');
    await waitFor(() => expect(rows[0]).toHaveTextContent('Deployment'));
    expect(rows[0]).toHaveTextContent('Routing rule');
    expect(within(rows[0]!).getByRole('link')).toHaveAttribute('href', `/o/acme/assist/${REQ_ID}`);
    expect(rows[1]).toHaveTextContent('SETH could not tell which specialist fits this request.');
    expect(rows[2]).toHaveTextContent('budget_denied');
  });

  it('has an empty state', async () => {
    stubAssist({ [LIST]: () => list([]) });
    renderWithClient(<AskSethView />);
    expect(await screen.findByText('No requests in this project yet.')).toBeVisible();
  });

  it('filters by status and loads more', async () => {
    const fetchMock = stubAssist({
      [LIST]: ({ url }) =>
        url.includes('cursor=')
          ? list([summary({ id: 'req-2' })])
          : list([summary()], 'next-cursor'),
    });
    renderWithClient(<AskSethView />);
    await userEvent.selectOptions(await screen.findByLabelText('Status'), 'refused');
    await waitFor(() =>
      expect(fetchMock.calls.some((c) => c.url.endsWith('/requests?status=refused'))).toBe(true),
    );
    await userEvent.click(await screen.findByRole('button', { name: 'Load more' }));
    await waitFor(() =>
      expect(
        fetchMock.calls.some((c) => c.url.endsWith('/requests?status=refused&cursor=next-cursor')),
      ).toBe(true),
    );
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'Load more' })).not.toBeInTheDocument(),
    );
    expect(screen.getAllByRole('row')).toHaveLength(3);
  });

  it('refetches after a refused request', async () => {
    const fetchMock = stubAssist({ [CREATE]: created(refused('route_unclear')) });
    renderWithClient(<AskSethView />);
    await screen.findByRole('table', { name: 'Requests' });
    const before = fetchMock.calls.filter((c) => c.key === LIST).length;
    await ask('Hello');
    await waitFor(() =>
      expect(fetchMock.calls.filter((c) => c.key === LIST).length).toBe(before + 1),
    );
  });
});
