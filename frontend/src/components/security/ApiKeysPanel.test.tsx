import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ALL_PERMS, bodyOf, CATALOG, list, stubApi } from '@/test/security';
import { jsonResponse, renderWithClient } from '@/test/utils';
import { ApiKeysPanel } from './ApiKeysPanel';

const SECRET = 'shf_dev_abcd1234_TOPSECRETVALUE';
const META = {
  id: 'k1',
  name: 'ci-key',
  prefix: 'abcd1234',
  principal_type: 'service_account',
  principal_id: 'sa-1',
  scopes: ['member:read'],
  expires_at: '2031-01-01T00:00:00Z',
};
const SA = { id: 'sa-1', name: 'CI bot', status: 'active', row_version: 1 };

afterEach(() => vi.unstubAllGlobals());

function stubKeys(extra = {}) {
  return stubApi({
    'GET /api/v1/orgs/acme/api-keys': () => list([]),
    'GET /api/v1/orgs/acme/service-accounts': () => list([SA]),
    'GET /api/v1/orgs/acme/users': () => list([]),
    ...extra,
  });
}

async function openCreate() {
  await userEvent.click(await screen.findByRole('button', { name: 'Create API key' }));
  return screen.getByRole('dialog');
}

describe('API key creation', () => {
  it('sends the chosen principal, scopes subset, restriction and expiry, then shows the secret once', async () => {
    const fetchMock = stubKeys({
      'GET /api/v1/orgs/acme/workspaces': () =>
        list([{ id: 'ws-1', slug: 'eng', name: 'Engineering', status: 'active', row_version: 1 }]),
      'GET /api/v1/orgs/acme/workspaces/eng/projects': () => list([]),
      'POST /api/v1/orgs/acme/api-keys': () =>
        jsonResponse({ api_key: META, secret: SECRET }, { status: 201 }),
    });
    renderWithClient(<ApiKeysPanel org="acme" />);
    const dialog = await openCreate();

    await userEvent.type(within(dialog).getByLabelText('Name'), 'ci-key');
    await waitFor(() =>
      expect(within(dialog).getByRole('option', { name: 'CI bot' })).toBeInTheDocument(),
    );
    await userEvent.selectOptions(within(dialog).getByLabelText('Principal'), 'sa-1');
    await userEvent.click(await within(dialog).findByRole('checkbox', { name: /member:read/ }));
    await userEvent.click(await within(dialog).findByRole('checkbox', { name: /Engineering/ }));
    await userEvent.click(within(dialog).getByRole('button', { name: 'Create key' }));

    await waitFor(() => expect(screen.getByText(SECRET)).toBeInTheDocument());
    expect(screen.getByText(/only time the api key secret is shown/i)).toBeInTheDocument();
    expect(bodyOf(fetchMock, 'POST /api/v1/orgs/acme/api-keys')).toEqual({
      name: 'ci-key',
      principal_type: 'service_account',
      principal_id: 'sa-1',
      scopes: ['member:read'],
      resource_scope: { workspace_ids: ['ws-1'] },
      expires_in_days: 90,
    });
  });

  it('offers mcp:access to a user who holds it and sends it in the scopes', async () => {
    const fetchMock = stubApi(
      {
        'GET /api/v1/orgs/acme/api-keys': () => list([]),
        'GET /api/v1/orgs/acme/service-accounts': () => list([SA]),
        'GET /api/v1/orgs/acme/users': () => list([]),
        'GET /api/v1/permissions': () =>
          list([
            ...CATALOG,
            { key: 'mcp:access', resource_type: 'mcp', action: 'access', is_privileged: false },
          ]),
        'POST /api/v1/orgs/acme/api-keys': () =>
          jsonResponse(
            { api_key: { ...META, scopes: ['mcp:access'] }, secret: SECRET },
            { status: 201 },
          ),
      },
      [...ALL_PERMS, 'mcp:access'],
    );
    renderWithClient(<ApiKeysPanel org="acme" />);
    const dialog = await openCreate();
    await userEvent.type(within(dialog).getByLabelText('Name'), 'mcp-key');
    await waitFor(() =>
      expect(within(dialog).getByRole('option', { name: 'CI bot' })).toBeInTheDocument(),
    );
    await userEvent.selectOptions(within(dialog).getByLabelText('Principal'), 'sa-1');
    await userEvent.click(await within(dialog).findByRole('checkbox', { name: /mcp:access/ }));
    await userEvent.click(within(dialog).getByRole('button', { name: 'Create key' }));
    await screen.findByText(SECRET);
    expect(bodyOf(fetchMock, 'POST /api/v1/orgs/acme/api-keys').scopes).toEqual(['mcp:access']);
  });

  it('clears the secret when the dialog closes and never brings it back', async () => {
    stubKeys({
      'POST /api/v1/orgs/acme/api-keys': () =>
        jsonResponse({ api_key: META, secret: SECRET }, { status: 201 }),
    });
    renderWithClient(<ApiKeysPanel org="acme" />);
    let dialog = await openCreate();
    await userEvent.type(within(dialog).getByLabelText('Name'), 'ci-key');
    await waitFor(() =>
      expect(within(dialog).getByRole('option', { name: 'CI bot' })).toBeInTheDocument(),
    );
    await userEvent.selectOptions(within(dialog).getByLabelText('Principal'), 'sa-1');
    await userEvent.click(await within(dialog).findByRole('checkbox', { name: /member:read/ }));
    await userEvent.click(within(dialog).getByRole('button', { name: 'Create key' }));
    await screen.findByText(SECRET);

    await userEvent.click(screen.getByRole('button', { name: 'I have copied the secret' }));
    expect(screen.queryByText(SECRET)).not.toBeInTheDocument();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();

    dialog = await openCreate();
    expect(within(dialog).queryByText(SECRET)).not.toBeInTheDocument();
    expect(document.body.textContent).not.toContain(SECRET);
    // The secret is never written to web storage either.
    expect(JSON.stringify({ ...localStorage })).not.toContain(SECRET);
    expect(JSON.stringify({ ...sessionStorage })).not.toContain(SECRET);
  });

  it('validates required fields and expiry range before calling the API', async () => {
    const fetchMock = stubKeys();
    renderWithClient(<ApiKeysPanel org="acme" />);
    const dialog = await openCreate();
    await userEvent.clear(within(dialog).getByLabelText('Expires in (days)'));
    await userEvent.type(within(dialog).getByLabelText('Expires in (days)'), '9999');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Create key' }));
    expect(await within(dialog).findByText('Name is required.')).toBeInTheDocument();
    expect(within(dialog).getByText('Select at least one scope.')).toBeInTheDocument();
    expect(within(dialog).getByText(/between 1 and 365/)).toBeInTheDocument();
    expect(fetchMock.calls.some((c) => c.key.startsWith('POST'))).toBe(false);
  });

  it('hides create, rotate and revoke without apikey:manage', async () => {
    stubApi(
      {
        'GET /api/v1/orgs/acme/api-keys': () => list([META]),
        'GET /api/v1/orgs/acme/service-accounts': () => list([SA]),
        'GET /api/v1/orgs/acme/users': () => list([]),
      },
      ['apikey:read'],
    );
    renderWithClient(<ApiKeysPanel org="acme" />);
    expect(await screen.findByText('ci-key')).toBeInTheDocument();
    expect(screen.getByText('CI bot')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Create API key' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Rotate/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Revoke/ })).not.toBeInTheDocument();
  });
});

describe('API key rotate and revoke', () => {
  it('rotates with a grace period and shows the new secret once', async () => {
    const fetchMock = stubKeys({
      'GET /api/v1/orgs/acme/api-keys': () => list([META]),
      'POST /api/v1/orgs/acme/api-keys/k1/rotate': () =>
        jsonResponse({ api_key: { ...META, id: 'k2' }, secret: SECRET }),
    });
    renderWithClient(<ApiKeysPanel org="acme" />);
    await userEvent.click(await screen.findByRole('button', { name: /Rotate/ }));
    const dialog = screen.getByRole('dialog');
    await userEvent.clear(within(dialog).getByLabelText('Grace period (hours)'));
    await userEvent.type(within(dialog).getByLabelText('Grace period (hours)'), '48');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Rotate key' }));
    expect(await screen.findByText(SECRET)).toBeInTheDocument();
    expect(bodyOf(fetchMock, 'POST /api/v1/orgs/acme/api-keys/k1/rotate')).toEqual({
      grace_hours: 48,
    });
    await userEvent.click(screen.getByRole('button', { name: 'I have copied the secret' }));
    expect(screen.queryByText(SECRET)).not.toBeInTheDocument();
  });

  it('revokes only after confirmation', async () => {
    const fetchMock = stubKeys({
      'GET /api/v1/orgs/acme/api-keys': () => list([META]),
      'POST /api/v1/orgs/acme/api-keys/k1/revoke': () =>
        jsonResponse({ ...META, revoked_at: '2026-01-01T00:00:00Z' }),
    });
    renderWithClient(<ApiKeysPanel org="acme" />);
    await userEvent.click(await screen.findByRole('button', { name: /Revoke/ }));
    expect(screen.getByRole('dialog')).toHaveAccessibleName('Revoke API key?');
    expect(fetchMock.calls.some((c) => c.key.endsWith('/revoke'))).toBe(false);
    await userEvent.click(screen.getByRole('button', { name: 'Revoke key' }));
    await waitFor(() =>
      expect(
        fetchMock.calls.some((c) => c.key === 'POST /api/v1/orgs/acme/api-keys/k1/revoke'),
      ).toBe(true),
    );
  });
});
