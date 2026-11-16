import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { bodyOf, list, stubApi } from '@/test/security';
import { jsonResponse, renderWithClient } from '@/test/utils';
import { RolesView } from './RolesView';

vi.mock('next/navigation', () => ({ useParams: () => ({ org: 'acme' }) }));

const SYSTEM = {
  id: 'r-sys',
  key: 'viewer',
  name: 'Viewer',
  is_system: true,
  permissions: ['member:read'],
  row_version: 1,
};
const CUSTOM = {
  id: 'r-1',
  key: 'auditor-plus',
  name: 'Auditor Plus',
  is_system: false,
  permissions: ['secret:read'],
  row_version: 7,
};

afterEach(() => vi.unstubAllGlobals());

describe('RolesView', () => {
  it('creates a custom role sending exactly the selected permission keys', async () => {
    const fetchMock = stubApi({
      'GET /api/v1/orgs/acme/roles': () => list([SYSTEM]),
      'POST /api/v1/orgs/acme/roles': () =>
        jsonResponse({ ...CUSTOM, id: 'r-new' }, { status: 201 }),
    });
    renderWithClient(<RolesView />);
    await userEvent.click(await screen.findByRole('button', { name: 'Create custom role' }));
    const dialog = screen.getByRole('dialog');
    await userEvent.type(within(dialog).getByLabelText('Key'), 'release-manager');
    await userEvent.type(within(dialog).getByLabelText('Name'), 'Release Manager');
    await userEvent.click(await within(dialog).findByRole('checkbox', { name: /role:read/ }));
    await userEvent.click(within(dialog).getByRole('checkbox', { name: /member:read/ }));
    // Privileged permissions are flagged in the checklist.
    expect(within(dialog).getAllByText('Privileged').length).toBeGreaterThan(0);
    await userEvent.click(within(dialog).getByRole('button', { name: 'Create role' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(bodyOf(fetchMock, 'POST /api/v1/orgs/acme/roles')).toEqual({
      key: 'release-manager',
      name: 'Release Manager',
      permissions: ['member:read', 'role:read'],
    });
  });

  it('edits a custom role with its row_version and updated permissions', async () => {
    const fetchMock = stubApi({
      'GET /api/v1/orgs/acme/roles': () => list([SYSTEM, CUSTOM]),
      'PATCH /api/v1/orgs/acme/roles/r-1': () => jsonResponse(CUSTOM),
    });
    renderWithClient(<RolesView />);
    await userEvent.click(await screen.findByRole('button', { name: /Edit Auditor Plus/ }));
    const dialog = screen.getByRole('dialog');
    const secretRead = await within(dialog).findByRole('checkbox', { name: /secret:read/ });
    expect(secretRead).toBeChecked();
    await userEvent.click(secretRead);
    await userEvent.click(within(dialog).getByRole('checkbox', { name: /member:read/ }));
    await userEvent.click(within(dialog).getByRole('button', { name: 'Save role' }));
    await waitFor(() =>
      expect(bodyOf(fetchMock, 'PATCH /api/v1/orgs/acme/roles/r-1')).toEqual({
        name: 'Auditor Plus',
        permissions: ['member:read'],
        row_version: 7,
      }),
    );
  });

  it('shows system roles read-only', async () => {
    stubApi({ 'GET /api/v1/orgs/acme/roles': () => list([SYSTEM]) });
    renderWithClient(<RolesView />);
    expect(await screen.findByText('System template')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: /View Viewer/ }));
    const dialog = screen.getByRole('dialog');
    expect(await within(dialog).findByRole('checkbox', { name: /member:read/ })).toBeDisabled();
    expect(within(dialog).queryByRole('button', { name: /Save role/ })).not.toBeInTheDocument();
    expect(within(dialog).getByLabelText('Name')).toBeDisabled();
  });

  it('hides role creation without role:manage', async () => {
    stubApi({ 'GET /api/v1/orgs/acme/roles': () => list([SYSTEM, CUSTOM]) }, ['role:read']);
    renderWithClient(<RolesView />);
    expect(await screen.findByText('auditor-plus')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Create custom role' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Edit/ })).not.toBeInTheDocument();
  });
});
