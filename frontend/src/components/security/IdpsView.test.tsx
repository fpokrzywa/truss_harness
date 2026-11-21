import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { bodyOf, list, stubApi, SESSION } from '@/test/security';
import { jsonResponse, problemResponse, renderWithClient } from '@/test/utils';
import { IdpsView, PlatformIdpsView } from './IdpsView';

vi.mock('next/navigation', () => ({ useParams: () => ({ org: 'acme' }) }));

const IDP = {
  id: 'i1',
  display_name: 'Corp SSO',
  issuer: 'https://idp.example',
  discovery_url: 'https://idp.example/.well-known/openid-configuration',
  client_id: 'client-1',
  client_secret_set: true,
  scopes: ['openid', 'email'],
  claim_mappings: { email: 'email', groups: 'groups' },
  jit: 'off',
  group_sync: 'off',
  status: 'active',
  row_version: 2,
};
const LIST = 'GET /api/v1/orgs/acme/idps';

afterEach(() => vi.unstubAllGlobals());

describe('IdpsView', () => {
  it('shows client secret state without ever showing the secret', async () => {
    stubApi({
      [LIST]: () =>
        list([IDP, { ...IDP, id: 'i2', display_name: 'Other', client_secret_set: false }]),
    });
    renderWithClient(<IdpsView />);
    expect(await screen.findByRole('cell', { name: 'Corp SSO' })).toBeInTheDocument();
    expect(screen.getByText('Set')).toBeInTheDocument();
    expect(screen.getByText('Not set')).toBeInTheDocument();
  });

  it('edits without resending the secret unless a new one is typed', async () => {
    const fetchMock = stubApi({
      [LIST]: () => list([IDP]),
      'PATCH /api/v1/orgs/acme/idps/i1': () => jsonResponse(IDP),
    });
    renderWithClient(<IdpsView />);
    await userEvent.click(await screen.findByRole('button', { name: /Edit Corp SSO/ }));
    const dialog = screen.getByRole('dialog');
    const secret = within(dialog).getByLabelText('Client secret');
    expect(secret).toHaveValue('');
    expect(within(dialog).getByText('set')).toBeInTheDocument();
    await userEvent.clear(within(dialog).getByLabelText('Display name'));
    await userEvent.type(within(dialog).getByLabelText('Display name'), 'Corp SSO 2');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Save' }));
    await waitFor(() => {
      const body = bodyOf(fetchMock, 'PATCH /api/v1/orgs/acme/idps/i1');
      expect(body.display_name).toBe('Corp SSO 2');
      expect(body.row_version).toBe(2);
      expect(body).not.toHaveProperty('client_secret');
    });
  });

  it('requires a client secret on create and sends it write-only', async () => {
    const fetchMock = stubApi({
      [LIST]: () => list([]),
      'POST /api/v1/orgs/acme/idps': () => jsonResponse(IDP, { status: 201 }),
    });
    renderWithClient(<IdpsView />);
    await userEvent.click(await screen.findByRole('button', { name: 'Add identity provider' }));
    const dialog = screen.getByRole('dialog');
    await userEvent.type(within(dialog).getByLabelText('Display name'), 'Corp SSO');
    await userEvent.type(within(dialog).getByLabelText('Issuer'), 'https://idp.example');
    await userEvent.type(within(dialog).getByLabelText('Discovery URL'), 'https://idp.example/d');
    await userEvent.type(within(dialog).getByLabelText('Client ID'), 'client-1');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Save' }));
    expect(await within(dialog).findByText('Client secret is required.')).toBeInTheDocument();
    expect(fetchMock.calls.some((c) => c.key.startsWith('POST'))).toBe(false);

    await userEvent.type(within(dialog).getByLabelText('Client secret'), 's3cret');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Save' }));
    await waitFor(() =>
      expect(bodyOf(fetchMock, 'POST /api/v1/orgs/acme/idps')).toMatchObject({
        client_secret: 's3cret',
        scopes: ['openid', 'email', 'profile'],
        claim_mappings: { email: 'email', groups: 'groups' },
      }),
    );
    expect(document.body.textContent).not.toContain('s3cret');
  });

  it('disables JIT for tenant IdPs, explains why and always sends jit off', async () => {
    const fetchMock = stubApi({
      [LIST]: () => list([{ ...IDP, jit: 'create_user' }]),
      'PATCH /api/v1/orgs/acme/idps/i1': () => jsonResponse(IDP),
    });
    renderWithClient(<IdpsView />);
    await userEvent.click(await screen.findByRole('button', { name: /Edit Corp SSO/ }));
    const dialog = screen.getByRole('dialog');
    const jit = within(dialog).getByLabelText('Just-in-time provisioning');
    expect(jit).toBeDisabled();
    expect(jit).toHaveValue('off');
    expect(
      within(dialog).getByText(
        'Just-in-time provisioning is only available for installation identity providers.',
      ),
    ).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole('button', { name: 'Save' }));
    await waitFor(() =>
      expect(bodyOf(fetchMock, 'PATCH /api/v1/orgs/acme/idps/i1').jit).toBe('off'),
    );
  });

  it('shows issuer_conflict on the issuer field', async () => {
    stubApi({
      [LIST]: () => list([IDP]),
      'PATCH /api/v1/orgs/acme/idps/i1': () => problemResponse(409, 'issuer_conflict'),
    });
    renderWithClient(<IdpsView />);
    await userEvent.click(await screen.findByRole('button', { name: /Edit Corp SSO/ }));
    const dialog = screen.getByRole('dialog');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Save' }));
    expect(
      await within(dialog).findByText(
        'This issuer is already registered to another identity provider',
      ),
    ).toBeInTheDocument();
    expect(within(dialog).getByLabelText('Issuer')).toBeInvalid();
  });

  it('shows the server 403 text when changing issuer needs secret:use, not the no-access page', async () => {
    const msg =
      'changing the issuer or discovery URL of an identity provider whose client secret stays bound needs secret:use on that secret';
    stubApi({
      [LIST]: () => list([IDP]),
      'PATCH /api/v1/orgs/acme/idps/i1': () => problemResponse(403, 'forbidden', { detail: msg }),
    });
    renderWithClient(<IdpsView />);
    await userEvent.click(await screen.findByRole('button', { name: /Edit Corp SSO/ }));
    const dialog = screen.getByRole('dialog');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Save' }));
    expect(await within(dialog).findByText(new RegExp(msg))).toBeInTheDocument();
    expect(screen.queryByText(/no access/i)).not.toBeInTheDocument();
  });

  it('shows test connection results', async () => {
    stubApi({
      [LIST]: () => list([IDP]),
      'POST /api/v1/orgs/acme/idps/i1/test': () =>
        jsonResponse({
          ok: false,
          checks: [
            { name: 'discovery', ok: true },
            { name: 'jwks', ok: false, detail: 'unreachable' },
          ],
        }),
    });
    renderWithClient(<IdpsView />);
    await userEvent.click(await screen.findByRole('button', { name: /Test connection/ }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('Connection test failed.')).toBeInTheDocument();
    expect(within(dialog).getByText('Passed: discovery')).toBeInTheDocument();
    expect(within(dialog).getByText('Failed: jwks: unreachable')).toBeInTheDocument();
  });

  it('hides management without idp:manage', async () => {
    stubApi({ [LIST]: () => list([IDP]) }, ['audit:read']);
    renderWithClient(<IdpsView />);
    expect(await screen.findByRole('cell', { name: 'Corp SSO' })).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'Add identity provider' })).toBeNull(),
    );
    expect(screen.queryByRole('button', { name: /Edit|Test connection/ })).toBeNull();
  });
});

describe('PlatformIdpsView', () => {
  it('is the neutral no-access page for non-platform-admins', async () => {
    stubApi();
    renderWithClient(<PlatformIdpsView />);
    expect(await screen.findByText('Not found or no access')).toBeInTheDocument();
  });

  it('manages installation IdPs for platform admins', async () => {
    stubApi({
      'GET /api/auth/session': () =>
        jsonResponse({ ...SESSION, user: { ...SESSION.user, is_platform_admin: true } }),
      'GET /api/v1/platform/idps': () => list([IDP]),
    });
    renderWithClient(<PlatformIdpsView />);
    expect(await screen.findByRole('cell', { name: 'Corp SSO' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Add identity provider' })).toBeInTheDocument();
  });

  it('keeps the JIT options for installation IdPs', async () => {
    stubApi({
      'GET /api/auth/session': () =>
        jsonResponse({ ...SESSION, user: { ...SESSION.user, is_platform_admin: true } }),
      'GET /api/v1/platform/idps': () => list([IDP]),
    });
    renderWithClient(<PlatformIdpsView />);
    await userEvent.click(await screen.findByRole('button', { name: /Edit Corp SSO/ }));
    const jit = within(screen.getByRole('dialog')).getByLabelText('Just-in-time provisioning');
    expect(jit).toBeEnabled();
    expect(within(jit).getAllByRole('option')).toHaveLength(3);
  });
});

const ADMIN_SESSION = {
  'GET /api/auth/session': () =>
    jsonResponse({ ...SESSION, user: { ...SESSION.user, is_platform_admin: true } }),
};
const TENANT = 'AAAAAAAA-BBBB-4CCC-8DDD-EEEEEEEEEEEE';
const TENANT_LOWER = TENANT.toLowerCase();
const ENTRA_ISSUER = `https://login.microsoftonline.com/${TENANT_LOWER}/v2.0`;
const ENTRA_DISCOVERY = `${ENTRA_ISSUER}/.well-known/openid-configuration`;
const ENTRA_IDP = {
  ...IDP,
  id: 'i3',
  display_name: 'Entra SSO',
  issuer: ENTRA_ISSUER,
  discovery_url: ENTRA_DISCOVERY,
  profile: 'entra',
  entra_tenant_id: TENANT_LOWER,
};
const PLATFORM_LIST = 'GET /api/v1/platform/idps';
const GROUPS = 'GET /api/v1/platform/groups';
const GROUP = {
  id: 'g-1',
  slug: 'data-team',
  name: 'Data team',
  description: null,
  source: 'idp',
  idp_config_id: 'i3',
  external_id: '11111111-2222-4333-8444-555555555555',
  status: 'active',
  row_version: 1,
  member_count: 0,
  created_at: '2026-11-20T10:00:00Z',
  updated_at: null,
};

async function openPlatformCreate() {
  await userEvent.click(await screen.findByRole('button', { name: 'Add identity provider' }));
  const dialog = screen.getByRole('dialog');
  await userEvent.type(within(dialog).getByLabelText('Display name'), 'Entra SSO');
  await userEvent.type(within(dialog).getByLabelText('Client ID'), 'client-1');
  await userEvent.type(within(dialog).getByLabelText('Client secret'), 's3cret');
  return dialog;
}

describe('IdpsView Entra profile', () => {
  it('shows no Profile select on org IdPs and sends no profile', async () => {
    const fetchMock = stubApi({
      [LIST]: () => list([IDP]),
      'PATCH /api/v1/orgs/acme/idps/i1': () => jsonResponse(IDP),
    });
    renderWithClient(<IdpsView />);
    await userEvent.click(await screen.findByRole('button', { name: /Edit Corp SSO/ }));
    const dialog = screen.getByRole('dialog');
    expect(within(dialog).queryByLabelText('Profile')).not.toBeInTheDocument();
    expect(within(dialog).queryByLabelText('Entra tenant id')).not.toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole('button', { name: 'Save' }));
    await waitFor(() => {
      const body = bodyOf(fetchMock, 'PATCH /api/v1/orgs/acme/idps/i1');
      expect(body).not.toHaveProperty('profile');
      expect(body).not.toHaveProperty('entra_tenant_id');
    });
  });

  it('offers a generic and an experimental Entra profile on platform IdPs', async () => {
    stubApi({ ...ADMIN_SESSION, [PLATFORM_LIST]: () => list([]) });
    renderWithClient(<PlatformIdpsView />);
    await userEvent.click(await screen.findByRole('button', { name: 'Add identity provider' }));
    const dialog = screen.getByRole('dialog');
    const profile = within(dialog).getByLabelText('Profile');
    expect(profile).toHaveValue('generic');
    expect(
      within(profile).getByRole('option', { name: 'Generic OpenID Connect' }),
    ).toBeInTheDocument();
    expect(
      within(profile).getByRole('option', { name: 'Microsoft Entra ID (experimental)' }),
    ).toBeInTheDocument();
    expect(within(dialog).queryByText('Experimental')).not.toBeInTheDocument();
    await userEvent.selectOptions(profile, 'entra');
    expect(within(dialog).getByText('Experimental')).toBeInTheDocument();
  });

  it('creates an Entra IdP with the derived issuer, discovery URL and groups claim', async () => {
    const fetchMock = stubApi({
      ...ADMIN_SESSION,
      [PLATFORM_LIST]: () => list([]),
      'POST /api/v1/platform/idps': () => jsonResponse(ENTRA_IDP, { status: 201 }),
    });
    renderWithClient(<PlatformIdpsView />);
    const dialog = await openPlatformCreate();
    await userEvent.selectOptions(within(dialog).getByLabelText('Profile'), 'entra');
    await userEvent.type(within(dialog).getByLabelText('Entra tenant id'), TENANT);
    const issuer = within(dialog).getByLabelText('Issuer');
    expect(issuer).toHaveValue(ENTRA_ISSUER);
    expect(issuer).toHaveAttribute('readonly');
    expect(within(dialog).getByLabelText('Discovery URL')).toHaveValue(ENTRA_DISCOVERY);
    expect(within(dialog).getByLabelText('Discovery URL')).toHaveAttribute('readonly');
    expect(within(dialog).getByLabelText('Groups claim')).toHaveValue('groups');
    expect(within(dialog).getByLabelText('Groups claim')).toHaveAttribute('readonly');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Save' }));
    await waitFor(() =>
      expect(bodyOf(fetchMock, 'POST /api/v1/platform/idps')).toMatchObject({
        profile: 'entra',
        entra_tenant_id: TENANT_LOWER,
        issuer: ENTRA_ISSUER,
        discovery_url: ENTRA_DISCOVERY,
        claim_mappings: { groups: 'groups' },
      }),
    );
  });

  it('requires a GUID tenant id for an Entra IdP', async () => {
    const fetchMock = stubApi({ ...ADMIN_SESSION, [PLATFORM_LIST]: () => list([]) });
    renderWithClient(<PlatformIdpsView />);
    const dialog = await openPlatformCreate();
    await userEvent.selectOptions(within(dialog).getByLabelText('Profile'), 'entra');
    await userEvent.type(within(dialog).getByLabelText('Entra tenant id'), 'not-a-guid');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Save' }));
    expect(
      await within(dialog).findByText('Enter the Entra tenant id (a GUID).'),
    ).toBeInTheDocument();
    expect(fetchMock.calls.some((c) => c.key.startsWith('POST'))).toBe(false);
  });

  it('sends profile generic and no tenant id for a generic platform IdP', async () => {
    const fetchMock = stubApi({
      ...ADMIN_SESSION,
      [PLATFORM_LIST]: () => list([]),
      'POST /api/v1/platform/idps': () => jsonResponse(IDP, { status: 201 }),
    });
    renderWithClient(<PlatformIdpsView />);
    const dialog = await openPlatformCreate();
    const issuer = within(dialog).getByLabelText('Issuer');
    expect(issuer).not.toHaveAttribute('readonly');
    expect(within(dialog).getByLabelText('Groups claim')).not.toHaveAttribute('readonly');
    await userEvent.type(issuer, 'https://idp.example');
    await userEvent.type(within(dialog).getByLabelText('Discovery URL'), 'https://idp.example/d');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Save' }));
    await waitFor(() => {
      const body = bodyOf(fetchMock, 'POST /api/v1/platform/idps');
      expect(body.profile).toBe('generic');
      expect(body.issuer).toBe('https://idp.example');
      expect(body).not.toHaveProperty('entra_tenant_id');
    });
  });

  it('prefills the profile and tenant id when editing an Entra IdP', async () => {
    stubApi({ ...ADMIN_SESSION, [PLATFORM_LIST]: () => list([ENTRA_IDP]) });
    renderWithClient(<PlatformIdpsView />);
    await userEvent.click(await screen.findByRole('button', { name: /Edit Entra SSO/ }));
    const dialog = screen.getByRole('dialog');
    expect(within(dialog).getByLabelText('Profile')).toHaveValue('entra');
    expect(within(dialog).getByLabelText('Entra tenant id')).toHaveValue(TENANT_LOWER);
  });

  it('badges Entra rows as experimental and leaves generic rows plain', async () => {
    stubApi({ ...ADMIN_SESSION, [PLATFORM_LIST]: () => list([IDP, ENTRA_IDP]) });
    renderWithClient(<PlatformIdpsView />);
    const entraCell = await screen.findByRole('cell', { name: /^Entra SSO/ });
    expect(within(entraCell).getByText('Entra (experimental)')).toBeInTheDocument();
    const genericCell = screen.getByRole('cell', { name: 'Corp SSO' });
    expect(within(genericCell).queryByText('Entra (experimental)')).toBeNull();
  });
});

describe('PlatformIdpsView Entra groups', () => {
  it('lists only the groups mapped to the IdP and has no button on generic rows', async () => {
    stubApi({
      ...ADMIN_SESSION,
      [PLATFORM_LIST]: () => list([IDP, ENTRA_IDP]),
      [GROUPS]: () =>
        list([
          GROUP,
          { ...GROUP, id: 'g-2', slug: 'other', name: 'Other team', idp_config_id: 'i9' },
        ]),
    });
    renderWithClient(<PlatformIdpsView />);
    await screen.findByRole('cell', { name: 'Corp SSO' });
    expect(screen.queryByRole('button', { name: 'Entra groups Corp SSO' })).toBeNull();
    await userEvent.click(screen.getByRole('button', { name: 'Entra groups Entra SSO' }));
    const dialog = screen.getByRole('dialog', { name: 'Entra groups: Entra SSO' });
    const table = await within(dialog).findByRole('table', { name: 'Mapped groups' });
    for (const h of ['Name', 'Slug', 'Entra object id']) {
      expect(within(table).getByRole('columnheader', { name: h })).toBeInTheDocument();
    }
    expect(within(table).getByText('Data team')).toBeInTheDocument();
    expect(within(table).getByText(GROUP.external_id)).toBeInTheDocument();
    expect(within(dialog).queryByText('Other team')).toBeNull();
  });

  it('shows an empty state when no group is mapped', async () => {
    stubApi({
      ...ADMIN_SESSION,
      [PLATFORM_LIST]: () => list([ENTRA_IDP]),
      [GROUPS]: () => list([{ ...GROUP, idp_config_id: null, external_id: null }]),
    });
    renderWithClient(<PlatformIdpsView />);
    await userEvent.click(await screen.findByRole('button', { name: 'Entra groups Entra SSO' }));
    const dialog = screen.getByRole('dialog');
    expect(
      await within(dialog).findByText('No platform groups are mapped to this identity provider.'),
    ).toBeInTheDocument();
  });

  it('maps a group by lowercased object id and refetches the list', async () => {
    const OBJECT = 'ABCDEF01-2345-4678-89AB-CDEF01234567';
    const fetchMock = stubApi({
      ...ADMIN_SESSION,
      [PLATFORM_LIST]: () => list([ENTRA_IDP]),
      [GROUPS]: () => list([GROUP]),
      'POST /api/v1/platform/groups': () => jsonResponse(GROUP, { status: 201 }),
    });
    renderWithClient(<PlatformIdpsView />);
    await userEvent.click(await screen.findByRole('button', { name: 'Entra groups Entra SSO' }));
    const dialog = screen.getByRole('dialog');
    await within(dialog).findByRole('table', { name: 'Mapped groups' });
    await userEvent.type(within(dialog).getByLabelText('Name'), 'Analysts');
    await userEvent.type(within(dialog).getByLabelText('Slug'), 'analysts');
    await userEvent.type(within(dialog).getByLabelText('Entra object id'), OBJECT);
    await userEvent.click(within(dialog).getByRole('button', { name: 'Map group' }));
    await waitFor(() =>
      expect(bodyOf(fetchMock, 'POST /api/v1/platform/groups')).toEqual({
        slug: 'analysts',
        name: 'Analysts',
        idp_config_id: 'i3',
        external_id: OBJECT.toLowerCase(),
      }),
    );
    await waitFor(() => expect(fetchMock.calls.filter((c) => c.key === GROUPS).length).toBe(2));
    await waitFor(() => expect(within(dialog).getByLabelText('Name')).toHaveValue(''));
    expect(within(dialog).getByLabelText('Entra object id')).toHaveValue('');
  });

  it('validates the name, slug and object id before calling the API', async () => {
    const fetchMock = stubApi({
      ...ADMIN_SESSION,
      [PLATFORM_LIST]: () => list([ENTRA_IDP]),
      [GROUPS]: () => list([]),
    });
    renderWithClient(<PlatformIdpsView />);
    await userEvent.click(await screen.findByRole('button', { name: 'Entra groups Entra SSO' }));
    const dialog = screen.getByRole('dialog');
    await userEvent.type(within(dialog).getByLabelText('Slug'), 'Bad_Slug');
    await userEvent.type(within(dialog).getByLabelText('Entra object id'), 'not-a-guid');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Map group' }));
    expect(await within(dialog).findByText('Name is 1-200 characters.')).toBeInTheDocument();
    expect(within(dialog).getByText(/lowercase letters, digits or hyphens/)).toBeInTheDocument();
    expect(within(dialog).getByText('Enter the Entra object id (a GUID).')).toBeInTheDocument();
    expect(within(dialog).getByLabelText('Entra object id')).toBeInvalid();
    expect(fetchMock.calls.some((c) => c.key.startsWith('POST'))).toBe(false);
  });

  it('shows a failed mapping as an alert', async () => {
    stubApi({
      ...ADMIN_SESSION,
      [PLATFORM_LIST]: () => list([ENTRA_IDP]),
      [GROUPS]: () => list([]),
      'POST /api/v1/platform/groups': () =>
        problemResponse(409, 'conflict', { detail: 'slug already exists' }),
    });
    renderWithClient(<PlatformIdpsView />);
    await userEvent.click(await screen.findByRole('button', { name: 'Entra groups Entra SSO' }));
    const dialog = screen.getByRole('dialog');
    await userEvent.type(within(dialog).getByLabelText('Name'), 'Analysts');
    await userEvent.type(within(dialog).getByLabelText('Slug'), 'analysts');
    await userEvent.type(
      within(dialog).getByLabelText('Entra object id'),
      '11111111-2222-4333-8444-555555555555',
    );
    await userEvent.click(within(dialog).getByRole('button', { name: 'Map group' }));
    const alert = await within(dialog).findByText(/slug already exists/);
    expect(alert.closest('[role="alert"]')).toHaveTextContent('Could not map the group');
  });
});
