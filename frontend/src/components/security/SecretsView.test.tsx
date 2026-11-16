import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { bodyOf, list, stubApi } from '@/test/security';
import { jsonResponse, renderWithClient } from '@/test/utils';
import { SecretsView } from './SecretsView';

vi.mock('next/navigation', () => ({ useParams: () => ({ org: 'acme' }) }));

const VALUE = 'super-sensitive-value-123';
const META = {
  id: 's1',
  name: 'db-password',
  scope: null,
  kind: 'generic',
  version: 1,
  status: 'active',
  row_version: 4,
  created_at: '2026-01-01T00:00:00Z',
};

afterEach(() => vi.unstubAllGlobals());

describe('SecretsView', () => {
  it('lists metadata only, with no value column', async () => {
    stubApi({ 'GET /api/v1/orgs/acme/secrets': () => list([META]) });
    renderWithClient(<SecretsView />);
    expect(await screen.findByRole('cell', { name: 'db-password' })).toBeInTheDocument();
    expect(screen.queryByRole('columnheader', { name: /value/i })).not.toBeInTheDocument();
  });

  it('creates a secret with a write-only value that is cleared on submit and never rendered', async () => {
    let release: () => void = () => undefined;
    const gate = new Promise<void>((r) => (release = r));
    const fetchMock = stubApi({
      'GET /api/v1/orgs/acme/secrets': () => list([]),
      'POST /api/v1/orgs/acme/secrets': async () => {
        await gate;
        return jsonResponse(META, { status: 201 });
      },
    });
    renderWithClient(<SecretsView />);
    await userEvent.click(await screen.findByRole('button', { name: 'Create secret' }));
    const dialog = screen.getByRole('dialog');
    await userEvent.type(within(dialog).getByLabelText('Name'), 'db-password');
    const field = within(dialog).getByLabelText('Secret value');
    expect(field).toHaveAttribute('type', 'password');
    await userEvent.type(field, VALUE);
    await userEvent.click(within(dialog).getByRole('button', { name: 'Create secret' }));

    // While the request is in flight the field is already empty and nothing shows the value.
    await waitFor(() => expect(field).toHaveValue(''));
    expect(document.body.textContent).not.toContain(VALUE);

    release();
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(bodyOf(fetchMock, 'POST /api/v1/orgs/acme/secrets')).toEqual({
      name: 'db-password',
      value: VALUE,
    });
    expect(document.body.textContent).not.toContain(VALUE);
  });

  it('rotates with a new write-only value and disables only after confirmation', async () => {
    const fetchMock = stubApi({
      'GET /api/v1/orgs/acme/secrets': () => list([META]),
      'POST /api/v1/orgs/acme/secrets/s1/rotate': () => jsonResponse({ ...META, version: 2 }),
      'PATCH /api/v1/orgs/acme/secrets/s1': () => jsonResponse({ ...META, status: 'disabled' }),
    });
    renderWithClient(<SecretsView />);
    await userEvent.click(await screen.findByRole('button', { name: /Rotate/ }));
    let dialog = screen.getByRole('dialog');
    await userEvent.type(within(dialog).getByLabelText('Secret value'), 'next-value');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Rotate secret' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(bodyOf(fetchMock, 'POST /api/v1/orgs/acme/secrets/s1/rotate')).toEqual({
      value: 'next-value',
    });

    await userEvent.click(screen.getByRole('button', { name: /Disable/ }));
    dialog = screen.getByRole('dialog');
    expect(fetchMock.calls.some((c) => c.key.startsWith('PATCH'))).toBe(false);
    await userEvent.click(within(dialog).getByRole('button', { name: 'Disable secret' }));
    await waitFor(() =>
      expect(bodyOf(fetchMock, 'PATCH /api/v1/orgs/acme/secrets/s1')).toEqual({
        status: 'disabled',
        row_version: 4,
      }),
    );
  });

  it('hides create, rotate and disable without secret:manage', async () => {
    stubApi({ 'GET /api/v1/orgs/acme/secrets': () => list([META]) }, ['secret:read']);
    renderWithClient(<SecretsView />);
    expect(await screen.findByRole('cell', { name: 'db-password' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Create secret|Rotate|Disable/ })).toBeNull();
  });
});
