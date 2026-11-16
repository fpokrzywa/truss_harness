import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { bodyOf, list, stubApi } from '@/test/security';
import { jsonResponse, problemResponse, renderWithClient } from '@/test/utils';
import { UsersView } from './UsersView';

vi.mock('next/navigation', () => ({ useParams: () => ({ org: 'acme' }) }));

const ALICE = {
  user: { id: 'u1', email: 'alice@acme.test', display_name: 'Alice' },
  status: 'active',
  row_version: 3,
  groups: [{ id: 'g1', name: 'Platform' }],
  role_bindings: [],
};
const USERS = 'GET /api/v1/orgs/acme/users';

afterEach(() => vi.unstubAllGlobals());

describe('UsersView', () => {
  it('shows the neutral no-access view when the API answers 403', async () => {
    stubApi({ [USERS]: () => problemResponse(403, 'forbidden') });
    renderWithClient(<UsersView />);
    expect(await screen.findByText('Not found or no access')).toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
  });

  it('surfaces non-permission errors with the request id', async () => {
    stubApi({ [USERS]: () => problemResponse(500, 'internal_error', { detail: 'Boom.' }) });
    renderWithClient(<UsersView />);
    expect(await screen.findByText('Boom.')).toBeInTheDocument();
    expect(screen.getByText('req-123')).toBeInTheDocument();
  });

  it('hides mutations the user lacks permission for', async () => {
    stubApi({ [USERS]: () => list([ALICE]) }, ['member:read']);
    renderWithClient(<UsersView />);
    expect(await screen.findByText('Alice')).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Add user' })).toBeNull());
    expect(screen.queryByRole('button', { name: /Suspend/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Roles/ })).not.toBeInTheDocument();
  });

  it('adds a user and shows the setup token once', async () => {
    const fetchMock = stubApi({
      [USERS]: () => list([ALICE]),
      'POST /api/v1/orgs/acme/users': () =>
        jsonResponse(
          {
            user: { id: 'u2', email: 'bob@acme.test', display_name: 'Bob' },
            setup_token: 'tok_ONCE_123',
          },
          { status: 201 },
        ),
      'GET /api/v1/orgs/acme/roles': () => list([]),
    });
    renderWithClient(<UsersView />);
    await userEvent.click(await screen.findByRole('button', { name: 'Add user' }));
    const dialog = screen.getByRole('dialog');
    await userEvent.type(within(dialog).getByLabelText('Email'), 'bob@acme.test');
    await userEvent.type(within(dialog).getByLabelText('Display name'), 'Bob');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Add user' }));

    expect(await screen.findByText('tok_ONCE_123')).toBeInTheDocument();
    expect(screen.getByText(/only time the setup token is shown/i)).toBeInTheDocument();
    expect(bodyOf(fetchMock, 'POST /api/v1/orgs/acme/users')).toEqual({
      email: 'bob@acme.test',
      display_name: 'Bob',
    });
    await userEvent.click(screen.getByRole('button', { name: 'Done' }));
    expect(screen.queryByText('tok_ONCE_123')).not.toBeInTheDocument();
  });

  it('validates the email before submitting', async () => {
    const fetchMock = stubApi({
      [USERS]: () => list([]),
      'GET /api/v1/orgs/acme/roles': () => list([]),
    });
    renderWithClient(<UsersView />);
    await userEvent.click(await screen.findByRole('button', { name: 'Add user' }));
    const dialog = screen.getByRole('dialog');
    await userEvent.type(within(dialog).getByLabelText('Email'), 'not-an-email');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Add user' }));
    expect(await within(dialog).findByText('Enter a valid email address.')).toBeInTheDocument();
    expect(within(dialog).getByText('Name is required.')).toBeInTheDocument();
    expect(fetchMock.calls.some((c) => c.key.startsWith('POST'))).toBe(false);
  });

  it('suspends after confirmation, sending the row_version', async () => {
    const fetchMock = stubApi({
      [USERS]: () => list([ALICE]),
      'PATCH /api/v1/orgs/acme/users/u1': () => jsonResponse({ ...ALICE, status: 'suspended' }),
    });
    renderWithClient(<UsersView />);
    await userEvent.click(await screen.findByRole('button', { name: /Suspend Alice/ }));
    expect(fetchMock.calls.some((c) => c.key.startsWith('PATCH'))).toBe(false);
    await userEvent.click(screen.getByRole('button', { name: 'Suspend user' }));
    await waitFor(() =>
      expect(bodyOf(fetchMock, 'PATCH /api/v1/orgs/acme/users/u1')).toEqual({
        status: 'suspended',
        row_version: 3,
      }),
    );
  });

  it('shows a 403 from a mutation instead of failing silently', async () => {
    stubApi({
      [USERS]: () => list([ALICE]),
      'PATCH /api/v1/orgs/acme/users/u1': () =>
        problemResponse(403, 'forbidden', { detail: 'Not allowed to suspend.' }),
    });
    renderWithClient(<UsersView />);
    await userEvent.click(await screen.findByRole('button', { name: /Suspend Alice/ }));
    await userEvent.click(screen.getByRole('button', { name: 'Suspend user' }));
    expect(await screen.findByText('Not allowed to suspend.')).toBeInTheDocument();
  });
});
