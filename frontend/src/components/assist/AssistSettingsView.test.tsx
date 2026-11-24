import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AssistSettingsOut } from '@/lib/api/assist-types';
import {
  A,
  bodyOf,
  catalog,
  INSTALL_PERMS,
  PA,
  settings,
  specialist,
  stubAssist,
} from '@/test/assist';
import { jsonResponse, problemResponse, renderWithClient } from '@/test/utils';
import { AssistSettingsView } from './AssistSettingsView';

const nav = vi.hoisted(() => ({ query: '' }));
vi.mock('next/navigation', () => ({
  useParams: () => ({ org: 'acme' }),
  useSearchParams: () => new URLSearchParams(nav.query),
  usePathname: () => '/o/acme/assist/settings',
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));
beforeEach(() => {
  nav.query = '';
});
afterEach(() => vi.unstubAllGlobals());

const ADMIN = { org: ['assist:admin', 'assist:request:read'] };
const READ = ['assist:request:read'];
const SETTINGS = `GET ${A}/settings`;
const PUT = `PUT ${A}/settings`;
const INSTALL = `POST ${PA}/install`;
const SPECIALISTS = `GET ${PA}/specialists`;
const called = (fetchMock: ReturnType<typeof stubAssist>, key: string) =>
  fetchMock.calls.filter((c) => c.key === key).length;

describe('AssistSettingsView organization', () => {
  /** A server that keeps what it is sent. */
  function stubServer(initial: Partial<AssistSettingsOut> = {}, perms = ADMIN) {
    let stored = settings(initial);
    const answer = (next: Partial<AssistSettingsOut>) => () => {
      stored = { ...stored, ...next };
      return jsonResponse(stored);
    };
    return stubAssist(
      {
        [SETTINGS]: () => jsonResponse(stored),
        [PUT]: ({ init }) => answer(JSON.parse(String(init.body)))(),
        [`POST ${A}/enable`]: answer({ assist_enabled: true, catalog_version: 'assist.catalog/1' }),
        [`POST ${A}/disable`]: answer({ assist_enabled: false }),
      },
      perms,
    );
  }
  const form = () => screen.findByRole('form', { name: 'Routing and cost' });
  const save = async () =>
    userEvent.click(within(await form()).getByRole('button', { name: 'Save' }));

  it('is not shown or read without assist:admin, and the project section still works', async () => {
    nav.query = 'ws=eng&proj=api';
    const fetchMock = stubAssist();
    renderWithClient(<AssistSettingsView />);
    expect(await screen.findByRole('table', { name: 'Specialists' })).toBeVisible();
    expect(screen.queryByRole('region', { name: 'Organization' })).not.toBeInTheDocument();
    expect(called(fetchMock, SETTINGS)).toBe(0);
  });

  it('turns assist off only after confirmation', async () => {
    const fetchMock = stubServer();
    renderWithClient(<AssistSettingsView />);
    const toggle = await screen.findByRole('switch', { name: 'Ask SETH' });
    expect(toggle).toHaveAttribute('aria-checked', 'true');
    await userEvent.click(toggle);
    let dialog = await screen.findByRole('dialog', { name: 'Turn Ask SETH off?' });
    expect(dialog).toHaveTextContent("Every project's specialists of this organization stop.");
    await userEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    expect(called(fetchMock, `POST ${A}/disable`)).toBe(0);

    await userEvent.click(toggle);
    dialog = await screen.findByRole('dialog');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Turn off' }));
    await waitFor(() =>
      expect(screen.getByRole('switch', { name: 'Ask SETH' })).toHaveAttribute(
        'aria-checked',
        'false',
      ),
    );
    expect(called(fetchMock, `POST ${A}/disable`)).toBe(1);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('turns assist on only after confirmation', async () => {
    const fetchMock = stubServer({ assist_enabled: false, catalog_version: null });
    renderWithClient(<AssistSettingsView />);
    await userEvent.click(await screen.findByRole('switch', { name: 'Ask SETH' }));
    expect(called(fetchMock, `POST ${A}/enable`)).toBe(0);
    const dialog = await screen.findByRole('dialog', { name: 'Turn Ask SETH on?' });
    await userEvent.click(within(dialog).getByRole('button', { name: 'Turn on' }));
    await waitFor(() => expect(called(fetchMock, `POST ${A}/enable`)).toBe(1));
    expect(await screen.findByText('On')).toBeVisible();
  });

  it('warns about an outdated pin and moves it', async () => {
    const fetchMock = stubServer({ catalog_version: 'assist.catalog/0' });
    renderWithClient(<AssistSettingsView />);
    expect(await screen.findByText(/pinned to catalog version assist\.catalog\/0/)).toBeVisible();
    await userEvent.click(screen.getByRole('button', { name: 'Move to assist.catalog/1' }));
    await waitFor(() =>
      expect(screen.queryByText(/pinned to catalog version/)).not.toBeInTheDocument(),
    );
    expect(called(fetchMock, `POST ${A}/enable`)).toBe(1);
  });

  it('saves all three fields and shows what the server stored', async () => {
    const fetchMock = stubServer();
    renderWithClient(<AssistSettingsView />);
    await userEvent.click(within(await form()).getByRole('checkbox'));
    const model = within(await form()).getByLabelText('Router model');
    await waitFor(() => expect(model).toBeEnabled());
    await userEvent.selectOptions(model, 'm-1');
    const cost = within(await form()).getByLabelText('Cost cap of one request (USD)');
    expect(cost).toHaveAccessibleDescription(/run install again after changing it/);
    await userEvent.clear(cost);
    await userEvent.type(cost, '2.5');
    await save();
    expect(await screen.findByText('Settings saved.')).toBeVisible();
    expect(bodyOf(fetchMock, PUT)).toEqual({
      model_routing_enabled: true,
      router_model_id: 'm-1',
      specialist_max_cost: 2.5,
    });
    expect(within(await form()).getByRole('checkbox')).toBeChecked();
    expect(within(await form()).getByLabelText('Cost cap of one request (USD)')).toHaveValue(2.5);
  });

  describe('after a save', () => {
    /** Saves once; `later` answers the saves after it. */
    async function saved(later: () => Response | Promise<Response>) {
      const answers = [() => jsonResponse(settings())];
      stubAssist({ [PUT]: () => (answers.pop() ?? later)() }, ADMIN);
      renderWithClient(<AssistSettingsView />);
      await save();
      expect(await screen.findByText('Settings saved.')).toBeVisible();
    }
    const gone = () => expect(screen.queryByText('Settings saved.')).not.toBeInTheDocument();

    it('drops the message when the next save is refused by the server', async () => {
      await saved(() => problemResponse(422, 'router_model_not_allowed'));
      await save();
      expect(await screen.findByRole('alert')).toHaveTextContent('router_model_not_allowed');
      gone();
    });

    it('drops the message on an edit, and the next save is refused before any call', async () => {
      await saved(() => jsonResponse(settings()));
      await userEvent.click(within(await form()).getByRole('checkbox'));
      gone();
      await save();
      expect(await screen.findByText(/Choose a router model/)).toBeVisible();
      gone();
    });

    it('says it again once a second save is stored', async () => {
      let answer: (r: Response) => void = () => {};
      await saved(() => new Promise<Response>((resolve) => (answer = resolve)));
      await save();
      gone();
      answer(jsonResponse(settings()));
      expect(await screen.findByText('Settings saved.')).toBeVisible();
    });

    it('drops the message when the switch is refused', async () => {
      await saved(() => jsonResponse(settings()));
      await userEvent.click(screen.getByRole('switch', { name: 'Ask SETH' }));
      const dialog = await screen.findByRole('dialog');
      await userEvent.click(within(dialog).getByRole('button', { name: 'Turn off' }));
      expect(await screen.findByRole('alert')).toBeVisible();
      gone();
    });
  });

  it('refuses routing without a model before any call', async () => {
    const fetchMock = stubServer();
    renderWithClient(<AssistSettingsView />);
    await userEvent.click(within(await form()).getByRole('checkbox'));
    await save();
    expect(await screen.findByText(/Choose a router model/)).toBeVisible();
    expect(called(fetchMock, PUT)).toBe(0);
  });

  it.each(['0', '0.12345', '-1', '123456789'])(
    'refuses a cost of %s before any call',
    async (v) => {
      const fetchMock = stubServer();
      renderWithClient(<AssistSettingsView />);
      const cost = within(await form()).getByLabelText('Cost cap of one request (USD)');
      await userEvent.clear(cost);
      await userEvent.type(cost, v);
      await save();
      expect(await screen.findByText(/above 0, with at most 4 decimals/)).toBeVisible();
      expect(called(fetchMock, PUT)).toBe(0);
    },
  );

  it.each(['router_model_not_allowed', 'router_model_required'])(
    'shows the 422 %s',
    async (code) => {
      stubAssist({ [PUT]: () => problemResponse(422, code, { detail: 'refused here' }) }, ADMIN);
      renderWithClient(<AssistSettingsView />);
      await form();
      await save();
      const alert = await screen.findByRole('alert');
      expect(alert).toHaveTextContent('Could not save the assist settings');
      expect(alert).toHaveTextContent(code);
    },
  );
});

describe('AssistSettingsView project', () => {
  beforeEach(() => {
    nav.query = 'ws=eng&proj=api';
  });
  const rows = [
    specialist('research'),
    specialist('solution_design', { status: 'disabled', available: false }),
    specialist('build', { status: 'installing', available: false, agent_id: null }),
    specialist('evaluation', { available: false }),
  ];
  const mixed = { [SPECIALISTS]: () => jsonResponse(rows) };
  const row = async (title: string) =>
    (await screen.findByRole('cell', { name: title })).closest('tr')!;
  const install = () => screen.findByRole('form', { name: 'Install specialists' });
  const chooseModel = async () => {
    const select = within(await install()).getByLabelText('Specialist model');
    await waitFor(() => expect(select).toBeEnabled());
    await userEvent.selectOptions(select, 'm-1');
  };
  const button = async () => within(await install()).getByRole('button', { name: 'Install' });
  const ALL = { project: [...READ, ...INSTALL_PERMS] };

  it('asks for a project first', async () => {
    nav.query = '';
    stubAssist();
    renderWithClient(<AssistSettingsView />);
    expect(await screen.findByText(/Choose a workspace and a project/)).toBeVisible();
    expect(screen.getByText(/governance approval rule can hold/)).toBeVisible();
  });

  it('shows each specialist with its status', async () => {
    stubAssist(mixed);
    renderWithClient(<AssistSettingsView />);
    expect(await row('Research')).toHaveTextContent('Enabled');
    expect(await row('Research')).toHaveTextContent('What Research does.');
    expect(await row('Solution design')).toHaveTextContent('Disabled');
    expect(await row('Build')).toHaveTextContent('Installing');
    expect(await row('Build')).toHaveTextContent('run install again');
    expect(await row('Build')).toHaveTextContent('App request');
    expect(await row('Evaluation')).toHaveTextContent('Off for the organization, or outdated.');
    expect(await row('Cost')).toHaveTextContent('Not installed');
    expect(screen.getByText(/governance approval rule can hold/)).toBeVisible();
  });

  it('enables and disables installed specialists', async () => {
    const fetchMock = stubAssist(
      {
        ...mixed,
        [`POST ${PA}/specialists/research/disable`]: () => jsonResponse(rows[0]),
        [`POST ${PA}/specialists/solution_design/enable`]: () => jsonResponse(rows[1]),
      },
      { project: [...READ, 'assist:install', 'agent:activate'] },
    );
    renderWithClient(<AssistSettingsView />);
    await userEvent.click(await screen.findByRole('button', { name: 'Disable Research' }));
    await waitFor(() => expect(called(fetchMock, SPECIALISTS)).toBe(2));
    expect(called(fetchMock, `POST ${PA}/specialists/research/disable`)).toBe(1);
    await userEvent.click(screen.getByRole('button', { name: 'Enable Solution design' }));
    await waitFor(() =>
      expect(called(fetchMock, `POST ${PA}/specialists/solution_design/enable`)).toBe(1),
    );
    expect(within(await row('Build')).queryByRole('button')).not.toBeInTheDocument();
    expect(within(await row('Cost')).queryByRole('button')).not.toBeInTheDocument();
  });

  it.each(['assist:install', 'agent:activate'])(
    'has no Enable or Disable with %s alone',
    async (permission) => {
      stubAssist(mixed, { project: [...READ, permission] });
      renderWithClient(<AssistSettingsView />);
      await row('Research');
      expect(screen.queryByRole('button', { name: /^(Enable|Disable)/ })).not.toBeInTheDocument();
    },
  );

  it('shows a refused toggle', async () => {
    stubAssist(
      {
        ...mixed,
        [`POST ${PA}/specialists/research/disable`]: () =>
          problemResponse(409, 'conflict', { detail: 'the specialist changed meanwhile' }),
      },
      ALL,
    );
    renderWithClient(<AssistSettingsView />);
    await userEvent.click(await screen.findByRole('button', { name: 'Disable Research' }));
    expect(await screen.findByText('the specialist changed meanwhile')).toBeVisible();
  });

  it('installs every key that is not disabled', async () => {
    const fetchMock = stubAssist({ ...mixed, [INSTALL]: () => jsonResponse(rows) }, ALL);
    renderWithClient(<AssistSettingsView />);
    expect(await button()).toBeDisabled();
    expect(await install()).toHaveTextContent(
      'Disabled specialists are left as they are: Solution design.',
    );
    await chooseModel();
    await userEvent.click(await button());
    expect(await screen.findByText('Specialists installed.')).toBeVisible();
    expect(bodyOf(fetchMock, INSTALL)).toEqual({
      model_id: 'm-1',
      keys: ['research', 'build', 'evaluation', 'cost', 'deployment'],
    });
    expect(called(fetchMock, SPECIALISTS)).toBe(2);
    const url = fetchMock.calls.find((c) => c.key.endsWith('/models/effective'))!.url;
    expect(url).toContain('workspace=eng');
    expect(url).toContain('project=api');
  });

  it('sends one install for a double click', async () => {
    const fetchMock = stubAssist({ [INSTALL]: () => new Promise(() => {}) }, ALL);
    renderWithClient(<AssistSettingsView />);
    await chooseModel();
    await userEvent.dblClick(await button());
    expect(await screen.findByRole('button', { name: 'Installing...' })).toBeDisabled();
    expect(called(fetchMock, INSTALL)).toBe(1);
  });

  it('shows a 409 publish_pending and reads the specialists and the catalog again', async () => {
    const fetchMock = stubAssist(
      {
        [INSTALL]: () =>
          problemResponse(409, 'publish_pending', { detail: 'the publish waits for approval' }),
      },
      ALL,
    );
    renderWithClient(<AssistSettingsView />);
    await chooseModel();
    await userEvent.click(await button());
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('the publish waits for approval');
    expect(alert).toHaveTextContent('publish_pending');
    await waitFor(() => expect(called(fetchMock, SPECIALISTS)).toBe(2));
    expect(called(fetchMock, `GET ${A}/catalog`)).toBe(2);
    expect(screen.queryByText('Specialists installed.')).not.toBeInTheDocument();
  });

  it.each(INSTALL_PERMS)('hides Install and names %s when it is missing', async (missing) => {
    stubAssist({}, { project: [...READ, ...INSTALL_PERMS.filter((p) => p !== missing)] });
    renderWithClient(<AssistSettingsView />);
    expect(await screen.findByText(/permissions you do not have/)).toHaveTextContent(
      `on this project: ${missing}.`,
    );
    expect(screen.queryByRole('form', { name: 'Install specialists' })).not.toBeInTheDocument();
  });

  it('cannot install while assist is off', async () => {
    stubAssist(
      { [`GET ${A}/catalog`]: () => jsonResponse(catalog({ assist_enabled: false })) },
      ALL,
    );
    renderWithClient(<AssistSettingsView />);
    await chooseModel();
    expect(await button()).toBeDisabled();
    expect(await install()).toHaveTextContent('Ask SETH is off for this organization');
  });

  it('cannot install when every specialist is disabled', async () => {
    stubAssist(
      {
        [SPECIALISTS]: () =>
          jsonResponse(catalog().entries.map((e) => specialist(e.key, { status: 'disabled' }))),
      },
      ALL,
    );
    renderWithClient(<AssistSettingsView />);
    await chooseModel();
    expect(await button()).toBeDisabled();
    expect(await install()).toHaveTextContent('Every specialist is disabled in this project.');
  });
});
