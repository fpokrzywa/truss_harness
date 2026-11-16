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
