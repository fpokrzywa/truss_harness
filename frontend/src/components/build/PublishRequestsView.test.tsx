import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { BUILD_PERMS } from '@/test/build';
import { bodyOf, list, stubApi } from '@/test/security';
import { jsonResponse, problemResponse, renderWithClient } from '@/test/utils';
import { PublishRequestsView } from './PublishRequestsView';

vi.mock('next/navigation', () => ({ useParams: () => ({ org: 'acme' }) }));
afterEach(() => vi.unstubAllGlobals());

const R = '/api/v1/orgs/acme/publish-requests';
const mine = {
  id: 'pr-mine',
  artifact: { kind: 'prompts', id: 'a-1', key: 'my-prompt', name: 'My prompt' },
  version_id: 'v-1',
  version_no: 2,
  change_note: 'mine',
  requested_by: { id: 'u-me', display: 'Me' },
  requested_at: '2026-11-03T10:00:00Z',
  status: 'pending',
};
const theirs = {
  id: 'pr-theirs',
  artifact: { kind: 'tools', id: 'a-2', key: 'their-tool', name: 'Their tool' },
  version_id: 'v-2',
  version_no: 1,
  change_note: 'theirs',
  requested_by: { id: 'u-other', display: 'Olive' },
  requested_at: '2026-11-03T11:00:00Z',
  status: 'pending',
};

describe('PublishRequestsView', () => {
  it('hides approve/reject on the requester’s own request', async () => {
    stubApi({ [`GET ${R}`]: () => list([mine, theirs]) }, BUILD_PERMS);
    renderWithClient(<PublishRequestsView />);
    expect(await screen.findByText('Their tool')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Approve Their tool' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Reject Their tool' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Approve My prompt' })).not.toBeInTheDocument();
    expect(screen.getByText('Your own request')).toBeInTheDocument();
  });

  it('approves with an optional comment', async () => {
    const fetchMock = stubApi(
      {
        [`GET ${R}`]: () => list([theirs]),
        [`POST ${R}/pr-theirs/decide`]: () => jsonResponse({ ...theirs, status: 'approved' }),
      },
      BUILD_PERMS,
    );
    renderWithClient(<PublishRequestsView />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Approve Their tool' }));
    const dlg = await screen.findByRole('dialog', { name: 'Approve publish request' });
    await user.type(within(dlg).getByLabelText('Comment (optional)'), 'lgtm');
    await user.click(within(dlg).getByRole('button', { name: 'Approve' }));
    await waitFor(() =>
      expect(bodyOf(fetchMock, `POST ${R}/pr-theirs/decide`)).toEqual({
        decision: 'approve',
        comment: 'lgtm',
      }),
    );
  });

  it('shows the server 403 (separation of duties) when a decision is refused', async () => {
    stubApi(
      {
        [`GET ${R}`]: () => list([theirs]),
        [`POST ${R}/pr-theirs/decide`]: () => problemResponse(403, 'forbidden'),
      },
      BUILD_PERMS,
    );
    renderWithClient(<PublishRequestsView />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Reject Their tool' }));
    const dlg = await screen.findByRole('dialog', { name: 'Reject publish request' });
    await user.click(within(dlg).getByRole('button', { name: 'Reject' }));
    expect(await within(dlg).findByRole('alert')).toHaveTextContent(/403/);
    expect(within(dlg).getByRole('alert')).toHaveTextContent(/different from the requester/);
  });

  it('shows the neutral no-access view without approval:decide', async () => {
    stubApi({ [`GET ${R}`]: () => list([theirs]) }, ['prompt:read']);
    renderWithClient(<PublishRequestsView />);
    expect(await screen.findByText('Not found or no access')).toBeInTheDocument();
  });
});
