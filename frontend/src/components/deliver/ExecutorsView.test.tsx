import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  CREDENTIAL,
  D,
  DELIVERY_READ_PERMS,
  EXECUTOR,
  EXECUTOR_SECRET,
  list,
  stubDelivery,
} from '@/test/delivery';
import { bodyOf } from '@/test/security';
import { jsonResponse, problemResponse, renderWithClient } from '@/test/utils';
import { formatDate } from '../admin/ui';
import { ExecutorsView } from './ExecutorsView';

vi.mock('next/navigation', () => ({ useParams: () => ({ org: 'acme' }) }));
afterEach(() => vi.unstubAllGlobals());

const ISSUED = {
  credential: { ...CREDENTIAL, id: 'c-2', prefix: 'shx_cd34' },
  secret: EXECUTOR_SECRET,
};

async function openCredentials() {
  await userEvent.click(await screen.findByRole('button', { name: /Credentials/ }));
  return screen.findByRole('table', { name: 'Credentials of edge-runner' });
}

describe('ExecutorsView list', () => {
  it('shows name, id, status, last seen, version and created', async () => {
    stubDelivery();
    renderWithClient(<ExecutorsView />);
    const table = await screen.findByRole('table', { name: 'Executors' });
    const row = within(table).getByRole('cell', { name: 'edge-runner' }).closest('tr')!;
    expect(within(row).getByText('e-1')).toHaveClass('font-mono');
    expect(within(row).getByText('active')).toBeInTheDocument();
    expect(within(row).getByText(formatDate(EXECUTOR.last_seen_at))).toBeInTheDocument();
    expect(within(row).getByText('2.4.1')).toBeInTheDocument();
    expect(within(row).getByText(formatDate(EXECUTOR.created_at))).toBeInTheDocument();
  });

  it('shows "-" for an executor never seen and without a version', async () => {
    stubDelivery({
      [`GET ${D}/executors`]: () => list([{ ...EXECUTOR, last_seen_at: null, version: null }]),
    });
    renderWithClient(<ExecutorsView />);
    const row = (await screen.findByRole('cell', { name: 'edge-runner' })).closest('tr')!;
    expect(within(row).getAllByText('-')).toHaveLength(2);
  });

  it('shows an empty state', async () => {
    stubDelivery({ [`GET ${D}/executors`]: () => list([]) });
    renderWithClient(<ExecutorsView />);
    expect(await screen.findByText(/No executors yet/)).toBeInTheDocument();
  });
});

describe('ExecutorsView executor actions', () => {
  it('creates an executor with its name and refreshes the list', async () => {
    const fetchMock = stubDelivery({
      [`POST ${D}/executors`]: () =>
        jsonResponse({ ...EXECUTOR, id: 'e-2', name: 'second' }, { status: 201 }),
    });
    renderWithClient(<ExecutorsView />);
    await userEvent.click(await screen.findByRole('button', { name: 'Create executor' }));
    const dialog = screen.getByRole('dialog');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Create executor' }));
    expect(await within(dialog).findByText('Name is required.')).toBeInTheDocument();
    await userEvent.type(within(dialog).getByLabelText('Name'), 'second');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Create executor' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(bodyOf(fetchMock, `POST ${D}/executors`)).toEqual({ name: 'second' });
    await waitFor(() =>
      expect(fetchMock.calls.filter((c) => c.key === `GET ${D}/executors`).length).toBe(2),
    );
  });

  it('shows a create error such as the one-active-executor 409', async () => {
    stubDelivery({
      [`POST ${D}/executors`]: () =>
        problemResponse(409, 'conflict', { detail: 'an active executor already exists' }),
    });
    renderWithClient(<ExecutorsView />);
    await userEvent.click(await screen.findByRole('button', { name: 'Create executor' }));
    const dialog = screen.getByRole('dialog');
    await userEvent.type(within(dialog).getByLabelText('Name'), 'second');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Create executor' }));
    expect(
      await within(dialog).findByText('an active executor already exists'),
    ).toBeInTheDocument();
    expect(within(dialog).getByText('Could not create the executor')).toBeInTheDocument();
  });

  it('disables only after a confirmation that says credentials are revoked', async () => {
    const fetchMock = stubDelivery({
      [`PATCH ${D}/executors/e-1`]: () => jsonResponse({ ...EXECUTOR, status: 'disabled' }),
    });
    renderWithClient(<ExecutorsView />);
    await userEvent.click(await screen.findByRole('button', { name: /Disable/ }));
    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveAccessibleName('Disable executor?');
    expect(dialog).toHaveTextContent(/all of its credentials are revoked/);
    expect(fetchMock.calls.some((c) => c.key.startsWith('PATCH'))).toBe(false);
    await userEvent.click(screen.getByRole('button', { name: 'Disable executor' }));
    await waitFor(() =>
      expect(bodyOf(fetchMock, `PATCH ${D}/executors/e-1`)).toEqual({ status: 'disabled' }),
    );
  });
});

describe('ExecutorsView credentials', () => {
  it('lists the credentials of an executor', async () => {
    stubDelivery({
      [`GET ${D}/executors/e-1/credentials`]: () =>
        jsonResponse([
          CREDENTIAL,
          {
            ...CREDENTIAL,
            id: 'c-0',
            prefix: 'shx_old0',
            status: 'revoked',
            revoked_at: '2026-11-03T10:00:00Z',
          },
        ]),
    });
    renderWithClient(<ExecutorsView />);
    const table = await openCredentials();
    const live = within(table).getByRole('cell', { name: 'shx_ab12' }).closest('tr')!;
    expect(within(live).getByRole('cell', { name: 'shx_ab12' })).toHaveClass('font-mono');
    expect(within(live).getByText('live')).toBeInTheDocument();
    expect(within(live).getByText(formatDate(CREDENTIAL.created_at))).toBeInTheDocument();
    expect(within(live).getByText(formatDate(CREDENTIAL.expires_at))).toBeInTheDocument();
    const revoked = within(table).getByRole('cell', { name: 'shx_old0' }).closest('tr')!;
    expect(within(revoked).getByText('revoked')).toBeInTheDocument();
    expect(within(revoked).getByText(formatDate('2026-11-03T10:00:00Z'))).toBeInTheDocument();
    expect(
      within(revoked).queryByRole('button', { name: /Rotate|Revoke/ }),
    ).not.toBeInTheDocument();
  });

  it('issues a credential and shows the secret once with a Copy button, then discards it', async () => {
    const fetchMock = stubDelivery({
      [`POST ${D}/executors/e-1/credentials`]: () => jsonResponse(ISSUED, { status: 201 }),
    });
    renderWithClient(<ExecutorsView />);
    await openCredentials();
    await userEvent.click(screen.getByRole('button', { name: 'Issue credential' }));
    const dialog = screen.getByRole('dialog');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Issue credential' }));

    expect(await screen.findByText(EXECUTOR_SECRET)).toBeInTheDocument();
    expect(screen.getByText(/only time the executor credential is shown/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Copy/ })).toBeInTheDocument();
    await waitFor(() =>
      expect(
        fetchMock.calls.filter((c) => c.key === `GET ${D}/executors/e-1/credentials`).length,
      ).toBe(2),
    );

    await userEvent.click(screen.getByRole('button', { name: 'I have copied the credential' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.queryByText(EXECUTOR_SECRET)).not.toBeInTheDocument();
    expect(document.body.textContent).not.toContain(EXECUTOR_SECRET);
    expect(fetchMock.calls.some((c) => c.url.includes(EXECUTOR_SECRET))).toBe(false);
    expect(JSON.stringify({ ...localStorage })).not.toContain(EXECUTOR_SECRET);
    expect(JSON.stringify({ ...sessionStorage })).not.toContain(EXECUTOR_SECRET);
  });

  it('shows an issue error such as the two-live-credentials 409', async () => {
    stubDelivery({
      [`POST ${D}/executors/e-1/credentials`]: () =>
        problemResponse(409, 'conflict', { detail: 'at most two live credentials' }),
    });
    renderWithClient(<ExecutorsView />);
    await openCredentials();
    await userEvent.click(screen.getByRole('button', { name: 'Issue credential' }));
    const dialog = screen.getByRole('dialog');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Issue credential' }));
    expect(await within(dialog).findByText('at most two live credentials')).toBeInTheDocument();
    expect(within(dialog).getByText('Could not issue the credential')).toBeInTheDocument();
  });

  it('rotates a live credential and shows the new secret', async () => {
    const fetchMock = stubDelivery({
      [`POST ${D}/executors/e-1/credentials/c-1/rotate`]: () =>
        jsonResponse(ISSUED, { status: 201 }),
    });
    renderWithClient(<ExecutorsView />);
    await openCredentials();
    await userEvent.click(screen.getByRole('button', { name: /Rotate/ }));
    const dialog = screen.getByRole('dialog');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Rotate credential' }));
    expect(await screen.findByText(EXECUTOR_SECRET)).toBeInTheDocument();
    expect(screen.getByRole('dialog')).toHaveAccessibleName('Credential rotated');
    expect(
      fetchMock.calls.some((c) => c.key === `POST ${D}/executors/e-1/credentials/c-1/rotate`),
    ).toBe(true);
    await userEvent.click(screen.getByRole('button', { name: 'I have copied the credential' }));
    expect(screen.queryByText(EXECUTOR_SECRET)).not.toBeInTheDocument();
  });

  it('revokes a credential only after confirmation', async () => {
    const fetchMock = stubDelivery({
      [`POST ${D}/executors/e-1/credentials/c-1/revoke`]: () =>
        jsonResponse({ ...CREDENTIAL, status: 'revoked', revoked_at: '2026-11-19T10:00:00Z' }),
    });
    renderWithClient(<ExecutorsView />);
    await openCredentials();
    await userEvent.click(screen.getByRole('button', { name: /Revoke/ }));
    expect(screen.getByRole('dialog')).toHaveAccessibleName('Revoke credential?');
    expect(fetchMock.calls.some((c) => c.key.endsWith('/revoke'))).toBe(false);
    await userEvent.click(screen.getByRole('button', { name: 'Revoke credential' }));
    await waitFor(() =>
      expect(
        fetchMock.calls.some((c) => c.key === `POST ${D}/executors/e-1/credentials/c-1/revoke`),
      ).toBe(true),
    );
  });

  it('has no create, disable, issue, rotate or revoke controls without delivery:executor:manage', async () => {
    stubDelivery({}, DELIVERY_READ_PERMS);
    renderWithClient(<ExecutorsView />);
    const table = await openCredentials();
    expect(within(table).getByRole('cell', { name: 'shx_ab12' })).toBeInTheDocument();
    for (const name of [/Create executor/, /Disable/, /Issue credential/, /Rotate/, /Revoke/]) {
      expect(screen.queryByRole('button', { name })).not.toBeInTheDocument();
    }
  });
});

describe('ExecutorsView plain text', () => {
  it('renders markup and URLs in executor values as literal text', async () => {
    const evil = '<img src=x onerror=alert(1)> https://evil.example/';
    stubDelivery({
      [`GET ${D}/executors`]: () => list([{ ...EXECUTOR, name: evil, version: evil }]),
      [`GET ${D}/executors/e-1/credentials`]: () =>
        jsonResponse([{ ...CREDENTIAL, prefix: 'https://evil.example/' }]),
    });
    const { container } = renderWithClient(<ExecutorsView />);
    const row = (await screen.findAllByText(evil))[0]!.closest('tr')!;
    const cells = within(row)
      .getAllByRole('cell')
      .filter((c) => c.textContent === evil);
    expect(cells).toHaveLength(2); // name and version, as text
    await userEvent.click(within(row).getByRole('button', { name: /Credentials/ }));
    expect(await screen.findByRole('cell', { name: 'https://evil.example/' })).toBeInTheDocument();
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('a[href="https://evil.example/"]')).toBeNull();
    expect(container.querySelectorAll('a')).toHaveLength(0);
  });
});
