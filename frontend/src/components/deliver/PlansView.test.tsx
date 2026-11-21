import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  D,
  DELIVERY_PERMS,
  list,
  PLAN,
  RELEASE_CASCADE,
  stubDelivery,
  TARGET,
  WITHDRAWN_RELEASE,
} from '@/test/delivery';
import { RELEASE, RELEASE_DIGEST, S } from '@/test/releases';
import { bodyOf } from '@/test/security';
import { jsonResponse, problemResponse, renderWithClient } from '@/test/utils';
import { formatDate } from '../admin/ui';
import { PlansView } from './PlansView';

vi.mock('next/navigation', () => ({ useParams: () => ({ org: 'acme' }) }));
afterEach(() => vi.unstubAllGlobals());

const UNRESOLVED = 'The target has an unresolved unknown outcome.';
const REQUIRED = 'This release has no passing evaluation.';
const RELEASE_HELP =
  'Releases are sealed on the Solutions screen and need a passing evaluation before a plan can be requested.';
const SEALED = `${RELEASE_DIGEST.slice(0, 12)}, sealed ${formatDate(RELEASE.sealed_at)}`;
const LATER = ['Project', 'Solution', 'Version', 'Release'];
const TARGETS = {
  ...RELEASE_CASCADE,
  [`GET ${D}/targets`]: () =>
    list([TARGET, { ...TARGET, id: 't-2', name: 'Old target', status: 'archived' }]),
};

/** Waits for the option to load, then selects it. */
async function pick(dialog: HTMLElement, label: string, option: string) {
  const select = within(dialog).getByLabelText(label);
  const opt = await within(select).findByRole('option', { name: option });
  await userEvent.selectOptions(select, opt);
}

/** Workspace, project, solution and version; the Release select is left to the caller. */
async function pickVersion(dialog: HTMLElement) {
  await pick(dialog, 'Workspace', 'Engineering');
  await pick(dialog, 'Project', 'API');
  await pick(dialog, 'Solution', 'Sales assistant');
  await pick(dialog, 'Version', 'Version 1');
}

async function openDialog() {
  await userEvent.click(await screen.findByRole('button', { name: 'Request plan' }));
  return screen.getByRole('dialog');
}

async function fillRequest(applicationKey = 'sales-app', chooseRelease = true) {
  const dialog = await openDialog();
  await waitFor(() =>
    expect(within(dialog).getByRole('option', { name: 'Analytics prod' })).toBeInTheDocument(),
  );
  expect(within(dialog).queryByRole('option', { name: 'Old target' })).not.toBeInTheDocument();
  await userEvent.selectOptions(within(dialog).getByLabelText('Target'), 't-1');
  if (chooseRelease) {
    await pickVersion(dialog);
    await pick(dialog, 'Release', SEALED);
  }
  await userEvent.type(within(dialog).getByLabelText('Application key'), applicationKey);
  await userEvent.type(within(dialog).getByLabelText('Display name'), 'Sales app');
  return dialog;
}

describe('PlansView list', () => {
  it('renders the plans with the target name and a link to each plan', async () => {
    stubDelivery();
    renderWithClient(<PlansView />);
    const table = await screen.findByRole('table', { name: 'Deployment plans' });
    const link = within(table).getByRole('link', { name: 'Sales app' });
    expect(link).toHaveAttribute('href', '/o/acme/deliver/plans/p-1');
    const row = link.closest('tr')!;
    expect(within(row).getByText('sales-app')).toBeInTheDocument();
    expect(await within(row).findByText('Analytics prod')).toBeInTheDocument();
    expect(within(row).getByText('sealed')).toBeInTheDocument();
  });

  it('falls back to the target id when the target is not listed', async () => {
    stubDelivery({ [`GET ${D}/targets`]: () => list([]) });
    renderWithClient(<PlansView />);
    expect(await screen.findByText('t-1')).toBeInTheDocument();
  });

  it('shows an empty state', async () => {
    stubDelivery({ [`GET ${D}/plans`]: () => list([]) });
    renderWithClient(<PlansView />);
    expect(await screen.findByText(/No deployment plans yet/)).toBeInTheDocument();
  });

  it('has no Request plan button without delivery:plan:request', async () => {
    stubDelivery(
      {},
      DELIVERY_PERMS.filter((p) => p !== 'delivery:plan:request'),
    );
    renderWithClient(<PlansView />);
    expect(await screen.findByRole('link', { name: 'Sales app' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Request plan' })).not.toBeInTheDocument();
  });
});

describe('PlansView request', () => {
  it('posts the request body for an active target and refreshes the list', async () => {
    const fetchMock = stubDelivery({
      ...TARGETS,
      [`POST ${D}/plans`]: () => jsonResponse({ ...PLAN, status: 'requested' }, { status: 201 }),
    });
    renderWithClient(<PlansView />);
    const dialog = await fillRequest();
    await userEvent.click(within(dialog).getByRole('button', { name: 'Request plan' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(bodyOf(fetchMock, `POST ${D}/plans`)).toEqual({
      target_id: 't-1',
      release_id: RELEASE.id,
      application_key: 'sales-app',
      display_name: 'Sales app',
    });
    await waitFor(() =>
      expect(fetchMock.calls.filter((c) => c.key === `GET ${D}/plans`).length).toBe(2),
    );
  });

  it('shows the five release selects in order and no release id field', async () => {
    stubDelivery(TARGETS);
    renderWithClient(<PlansView />);
    const dialog = await openDialog();
    const selects = ['Workspace', ...LATER].map((l) => within(dialog).getByLabelText(l));
    for (const select of selects) {
      expect(select.tagName).toBe('SELECT');
      expect(select).toHaveValue('');
      expect(within(select).getByRole('option', { name: 'Select...' })).toBeInTheDocument();
    }
    for (let i = 1; i < selects.length; i++) {
      const order = selects[i - 1]!.compareDocumentPosition(selects[i]!);
      expect(order & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    }
    expect(within(dialog).queryByLabelText('Release id')).not.toBeInTheDocument();
  });

  it('shows the release help line under the Release select', async () => {
    stubDelivery(TARGETS);
    renderWithClient(<PlansView />);
    const dialog = await openDialog();
    const help = within(dialog).getByText(RELEASE_HELP);
    const release = within(dialog).getByLabelText('Release');
    expect(release.compareDocumentPosition(help) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(help.compareDocumentPosition(within(dialog).getByLabelText('Application key'))).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    );
  });

  it('enables each select in turn, offers only sealed releases and sends the chosen id', async () => {
    const fetchMock = stubDelivery({
      ...TARGETS,
      [`POST ${D}/plans`]: () => jsonResponse({ ...PLAN, status: 'requested' }, { status: 201 }),
    });
    renderWithClient(<PlansView />);
    const dialog = await openDialog();
    const get = (l: string) => within(dialog).getByLabelText(l);
    await waitFor(() =>
      expect(within(dialog).getByRole('option', { name: 'Analytics prod' })).toBeInTheDocument(),
    );
    await userEvent.selectOptions(get('Target'), 't-1');
    expect(get('Workspace')).toBeEnabled();
    for (const l of LATER) expect(get(l)).toBeDisabled();

    await within(get('Workspace')).findByRole('option', { name: 'Engineering' });
    expect(within(get('Workspace')).queryByRole('option', { name: 'Old workspace' })).toBeNull();
    await pick(dialog, 'Workspace', 'Engineering');
    expect(get('Project')).toBeEnabled();
    for (const l of LATER.slice(1)) expect(get(l)).toBeDisabled();

    await within(get('Project')).findByRole('option', { name: 'API' });
    expect(within(get('Project')).queryByRole('option', { name: 'Legacy project' })).toBeNull();
    await pick(dialog, 'Project', 'API');
    expect(get('Solution')).toBeEnabled();
    for (const l of LATER.slice(2)) expect(get(l)).toBeDisabled();

    await pick(dialog, 'Solution', 'Sales assistant');
    expect(get('Version')).toBeEnabled();
    expect(get('Release')).toBeDisabled();

    await pick(dialog, 'Version', 'Version 1');
    expect(get('Release')).toBeEnabled();
    await within(get('Release')).findByRole('option', { name: SEALED });
    // "Select..." plus the sealed release; the withdrawn one (digest 0123456789ab...) is absent.
    expect(within(get('Release')).getAllByRole('option')).toHaveLength(2);
    expect(within(get('Release')).queryByRole('option', { name: /^0123456789ab/ })).toBeNull();
    await pick(dialog, 'Release', SEALED);

    await userEvent.type(get('Application key'), 'sales-app');
    await userEvent.type(get('Display name'), 'Sales app');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Request plan' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(bodyOf(fetchMock, `POST ${D}/plans`)).toEqual({
      target_id: 't-1',
      release_id: RELEASE.id,
      application_key: 'sales-app',
      display_name: 'Sales app',
    });
  });

  it('clears every later select when an earlier one changes', async () => {
    stubDelivery(TARGETS);
    renderWithClient(<PlansView />);
    const dialog = await openDialog();
    const get = (l: string) => within(dialog).getByLabelText(l);
    await pickVersion(dialog);
    await pick(dialog, 'Release', SEALED);

    await userEvent.selectOptions(get('Solution'), '');
    for (const l of ['Version', 'Release']) {
      expect(get(l)).toHaveValue('');
      expect(get(l)).toBeDisabled();
    }

    await pick(dialog, 'Solution', 'Sales assistant');
    await pick(dialog, 'Version', 'Version 1');
    await userEvent.selectOptions(get('Workspace'), '');
    for (const l of LATER) {
      expect(get(l)).toHaveValue('');
      expect(get(l)).toBeDisabled();
    }
  });

  it('says so when a version has no sealed release', async () => {
    stubDelivery({
      ...TARGETS,
      [`GET ${S}/s-1/versions/sv-1/releases`]: () => list([WITHDRAWN_RELEASE]),
    });
    renderWithClient(<PlansView />);
    const dialog = await openDialog();
    expect(within(dialog).queryByText('No sealed releases for this version.')).toBeNull();
    await pickVersion(dialog);
    expect(
      await within(dialog).findByText('No sealed releases for this version.'),
    ).toBeInTheDocument();
    expect(within(within(dialog).getByLabelText('Release')).getAllByRole('option')).toHaveLength(1);
  });

  it('says so when a project has no solutions', async () => {
    stubDelivery({ ...TARGETS, [`GET ${S}`]: () => list([]) });
    renderWithClient(<PlansView />);
    const dialog = await openDialog();
    await pick(dialog, 'Workspace', 'Engineering');
    await pick(dialog, 'Project', 'API');
    expect(await within(dialog).findByText('No solutions in this project.')).toBeInTheDocument();
  });

  it('announces a pending list load under its select as a status', async () => {
    stubDelivery({ ...TARGETS, [`GET ${S}`]: () => new Promise<Response>(() => {}) });
    renderWithClient(<PlansView />);
    const dialog = await openDialog();
    await pick(dialog, 'Workspace', 'Engineering');
    await pick(dialog, 'Project', 'API');
    expect(await within(dialog).findByRole('status')).toHaveTextContent('Loading solutions...');
  });

  it('shows a failed list load under its select', async () => {
    stubDelivery({
      ...TARGETS,
      [`GET ${S}`]: () => problemResponse(403, 'forbidden', { detail: 'missing permission' }),
    });
    renderWithClient(<PlansView />);
    const dialog = await openDialog();
    await pick(dialog, 'Workspace', 'Engineering');
    await pick(dialog, 'Project', 'API');
    const detail = await within(dialog).findByText('missing permission');
    expect(detail.closest('[role="alert"]')).toHaveTextContent('Could not load the solutions');
  });

  it('keeps the release link on the requested release after the selects change', async () => {
    stubDelivery({
      ...TARGETS,
      [`POST ${D}/plans`]: () => problemResponse(409, 'evaluation_required', { detail: REQUIRED }),
    });
    renderWithClient(<PlansView />);
    const dialog = await fillRequest();
    await userEvent.click(within(dialog).getByRole('button', { name: 'Request plan' }));
    await within(dialog).findByText(REQUIRED);
    await userEvent.selectOptions(within(dialog).getByLabelText('Workspace'), '');
    expect(within(dialog).getByRole('link', { name: 'Open the release' })).toHaveAttribute(
      'href',
      `/o/acme/releases/${RELEASE.id}`,
    );
  });

  it('blocks submit without a chosen release', async () => {
    const fetchMock = stubDelivery(TARGETS);
    renderWithClient(<PlansView />);
    const dialog = await fillRequest('sales-app', false);
    await userEvent.click(within(dialog).getByRole('button', { name: 'Request plan' }));
    expect(await within(dialog).findByText('Choose a release.')).toBeInTheDocument();
    await pickVersion(dialog);
    await userEvent.click(within(dialog).getByRole('button', { name: 'Request plan' }));
    expect(within(dialog).getByText('Choose a release.')).toBeInTheDocument();
    expect(fetchMock.calls.some((c) => c.key.startsWith('POST'))).toBe(false);
  });

  it('shows a 409 evaluation_required problem with a link to the release', async () => {
    stubDelivery({
      ...TARGETS,
      [`POST ${D}/plans`]: () => problemResponse(409, 'evaluation_required', { detail: REQUIRED }),
    });
    renderWithClient(<PlansView />);
    const dialog = await fillRequest();
    await userEvent.click(within(dialog).getByRole('button', { name: 'Request plan' }));
    const alert = await within(dialog).findByText(REQUIRED);
    expect(alert.closest('[role="alert"]')).toHaveTextContent('Could not request the plan');
    expect(within(dialog).getByText('evaluation_required')).toBeInTheDocument();
    expect(within(dialog).getByRole('link', { name: 'Open the release' })).toHaveAttribute(
      'href',
      `/o/acme/releases/${RELEASE.id}`,
    );
  });

  it('validates the application key before calling the API', async () => {
    const fetchMock = stubDelivery(TARGETS);
    renderWithClient(<PlansView />);
    const dialog = await fillRequest('Sales_App');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Request plan' }));
    expect(await within(dialog).findByText(/lowercase letters/)).toBeInTheDocument();
    expect(fetchMock.calls.some((c) => c.key.startsWith('POST'))).toBe(false);
  });

  it('shows a 409 target_unresolved problem inside the dialog, with no release link', async () => {
    stubDelivery({
      ...TARGETS,
      [`POST ${D}/plans`]: () => problemResponse(409, 'target_unresolved', { detail: UNRESOLVED }),
    });
    renderWithClient(<PlansView />);
    const dialog = await fillRequest();
    await userEvent.click(within(dialog).getByRole('button', { name: 'Request plan' }));
    const alert = await within(dialog).findByText(UNRESOLVED);
    expect(alert.closest('[role="alert"]')).toHaveTextContent('Could not request the plan');
    expect(within(dialog).getByText('target_unresolved')).toBeInTheDocument();
    expect(within(dialog).queryByRole('link', { name: 'Open the release' })).toBeNull();
  });
});
