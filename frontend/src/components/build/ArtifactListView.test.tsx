import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { artifact, BUILD_PERMS } from '@/test/build';
import { bodyOf, list, stubApi } from '@/test/security';
import { jsonResponse, renderWithClient } from '@/test/utils';
import { ArtifactListView } from './ArtifactListView';

const push = vi.fn();
vi.mock('next/navigation', () => ({
  useParams: () => ({ org: 'acme' }),
  useRouter: () => ({ push }),
}));
beforeEach(() => push.mockReset());
afterEach(() => vi.unstubAllGlobals());

const L = '/api/v1/orgs/acme/prompts';

describe('ArtifactListView', () => {
  it('lists artifacts with latest published and draft indicators', async () => {
    stubApi(
      {
        [`GET ${L}`]: () =>
          list([
            artifact({ id: 'a-1', name: 'Alpha', has_draft: true }),
            artifact({
              id: 'a-2',
              key: 'beta',
              name: 'Beta',
              latest_published_version: null,
              has_draft: false,
            }),
          ]),
      },
      BUILD_PERMS,
    );
    renderWithClient(<ArtifactListView kind="prompts" />);
    expect(await screen.findByRole('link', { name: 'Alpha' })).toHaveAttribute(
      'href',
      '/o/acme/build/prompts/a-1',
    );
    const rows = screen.getAllByRole('row');
    expect(within(rows[1] as HTMLElement).getByText('v1')).toBeInTheDocument();
    expect(within(rows[1] as HTMLElement).getByText('Draft')).toBeInTheDocument();
    expect(within(rows[2] as HTMLElement).getByText('Unpublished')).toBeInTheDocument();
  });

  it('passes search, status and scope filters to the server', async () => {
    const fetchMock = stubApi({ [`GET ${L}`]: () => list([]) }, BUILD_PERMS);
    renderWithClient(<ArtifactListView kind="prompts" />);
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText('Search'), 'refund');
    await user.selectOptions(screen.getByLabelText('Status'), 'disabled');
    await user.selectOptions(screen.getByLabelText('Scope'), 'tenant');
    await waitFor(() => {
      const url = [...fetchMock.calls].reverse().find((c) => c.key === `GET ${L}`)?.url ?? '';
      expect(url).toContain('q=refund');
      expect(url).toContain('status=disabled');
      expect(url).toContain('scope_type=tenant');
      expect(url).toContain('scope_id=org-1');
    });
  });

  it('hides Create without the create permission', async () => {
    stubApi({ [`GET ${L}`]: () => list([]) }, ['prompt:read']);
    renderWithClient(<ArtifactListView kind="prompts" />);
    expect(await screen.findByText('No prompts match.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Create prompt' })).not.toBeInTheDocument();
  });

  it('creates an artifact at the chosen scope and opens it', async () => {
    const fetchMock = stubApi(
      {
        [`GET ${L}`]: () => list([]),
        [`POST ${L}`]: () => jsonResponse(artifact({ id: 'new-1' }), { status: 201 }),
      },
      BUILD_PERMS,
    );
    renderWithClient(<ArtifactListView kind="prompts" />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Create prompt' }));
    const dlg = await screen.findByRole('dialog', { name: 'Create prompt' });
    const submit = within(dlg).getByRole('button', { name: 'Create' });
    expect(submit).toBeDisabled();
    await user.type(within(dlg).getByLabelText('Key'), 'Bad Key');
    expect(within(dlg).getByText(/Use lowercase letters/)).toBeInTheDocument();
    await user.clear(within(dlg).getByLabelText('Key'));
    await user.type(within(dlg).getByLabelText('Key'), 'support.reply');
    await user.type(within(dlg).getByLabelText('Name'), 'Support reply');
    await user.selectOptions(within(dlg).getByLabelText('Visibility'), 'tenant');
    await user.click(submit);
    await waitFor(() => expect(push).toHaveBeenCalledWith('/o/acme/build/prompts/new-1'));
    expect(bodyOf(fetchMock, `POST ${L}`)).toEqual({
      key: 'support.reply',
      name: 'Support reply',
      scope: { type: 'tenant', id: 'org-1' },
      visibility: 'tenant',
    });
  });
});
