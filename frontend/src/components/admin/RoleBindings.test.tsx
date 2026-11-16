import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { bodyOf, list, stubApi } from '@/test/security';
import { jsonResponse, renderWithClient } from '@/test/utils';
import { RoleBindings } from './RoleBindings';

const ROLE = {
  id: 'r1',
  key: 'viewer',
  name: 'Viewer',
  is_system: true,
  permissions: [],
  row_version: 1,
};
const WS = { id: 'ws-1', slug: 'eng', name: 'Engineering', status: 'active', row_version: 1 };
const PROJ = { id: 'p-1', slug: 'api', name: 'API', status: 'active', row_version: 1 };

afterEach(() => vi.unstubAllGlobals());

function stubBindings(extra = {}, perms?: string[]) {
  return stubApi(
    {
      'GET /api/v1/orgs/acme/role-bindings': () => list([]),
      'GET /api/v1/orgs/acme/roles': () => list([ROLE]),
      'GET /api/v1/orgs/acme/workspaces': () => list([WS]),
      'GET /api/v1/orgs/acme/workspaces/eng/projects': () => list([PROJ]),
      'POST /api/v1/orgs/acme/role-bindings': () => jsonResponse({}, { status: 201 }),
      ...extra,
    },
    perms,
  );
}

async function openAdd() {
  await userEvent.click(await screen.findByRole('button', { name: 'Add role assignment' }));
  const dialog = screen.getByRole('dialog');
  await waitFor(() => expect(within(dialog).getByRole('option', { name: 'Viewer' })).toBeVisible());
  return dialog;
}

describe('RoleBindings', () => {
  it('binds a role at project scope with the contract payload shape', async () => {
    const fetchMock = stubBindings();
    renderWithClient(<RoleBindings org="acme" subjectType="group" subjectId="g1" canManage />);
    const dialog = await openAdd();
    await userEvent.selectOptions(within(dialog).getByLabelText('Role'), 'r1');
    await userEvent.selectOptions(within(dialog).getByLabelText('Scope'), 'project');
    await waitFor(() =>
      expect(within(dialog).getByRole('option', { name: 'Engineering / API' })).toBeInTheDocument(),
    );
    await userEvent.selectOptions(within(dialog).getByLabelText('Project'), 'p-1');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Add assignment' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(bodyOf(fetchMock, 'POST /api/v1/orgs/acme/role-bindings')).toEqual({
      subject_type: 'group',
      subject_id: 'g1',
      role_id: 'r1',
      scope: { type: 'project', id: 'p-1' },
    });
  });

  it('defaults to tenant scope using the organization id', async () => {
    const fetchMock = stubBindings();
    renderWithClient(<RoleBindings org="acme" subjectType="user" subjectId="u9" canManage />);
    const dialog = await openAdd();
    await userEvent.selectOptions(within(dialog).getByLabelText('Role'), 'r1');
    await waitFor(() =>
      expect(within(dialog).getByRole('button', { name: 'Add assignment' })).toBeEnabled(),
    );
    await userEvent.click(within(dialog).getByRole('button', { name: 'Add assignment' }));
    await waitFor(() =>
      expect(bodyOf(fetchMock, 'POST /api/v1/orgs/acme/role-bindings')).toEqual({
        subject_type: 'user',
        subject_id: 'u9',
        role_id: 'r1',
        scope: { type: 'tenant', id: 'org-1' },
      }),
    );
  });

  it('removes an assignment only after confirmation', async () => {
    const fetchMock = stubBindings({
      'GET /api/v1/orgs/acme/role-bindings': () =>
        list([
          {
            id: 'b1',
            subject_type: 'user',
            subject_id: 'u9',
            role_id: 'r1',
            scope: { type: 'workspace', id: 'ws-1' },
          },
        ]),
      'DELETE /api/v1/orgs/acme/role-bindings/b1': () => new Response(null, { status: 204 }),
    });
    renderWithClient(<RoleBindings org="acme" subjectType="user" subjectId="u9" canManage />);
    expect(await screen.findByText('Workspace: Engineering')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: /Remove Viewer assignment/ }));
    expect(fetchMock.calls.some((c) => c.key.startsWith('DELETE'))).toBe(false);
    await userEvent.click(screen.getByRole('button', { name: 'Remove assignment' }));
    await waitFor(() =>
      expect(
        fetchMock.calls.some((c) => c.key === 'DELETE /api/v1/orgs/acme/role-bindings/b1'),
      ).toBe(true),
    );
  });

  it('hides add and remove when the user cannot manage roles', async () => {
    stubBindings();
    renderWithClient(
      <RoleBindings org="acme" subjectType="user" subjectId="u9" canManage={false} />,
    );
    expect(await screen.findByText('No roles are assigned directly.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Add role assignment' })).not.toBeInTheDocument();
  });
});
