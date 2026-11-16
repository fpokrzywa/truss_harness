import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/lib/api/errors';
import type { Workspace } from '@/lib/api/types';
import { ScopeListView } from './ScopeListView';

const items: Workspace[] = [
  { id: '1', slug: 'eng', name: 'Engineering', status: 'active', row_version: 3 },
  { id: '2', slug: 'old', name: 'Legacy', status: 'archived', row_version: 1 },
];

function setup(archive = vi.fn().mockResolvedValue({}), showArchived = false) {
  const onShow = vi.fn();
  render(
    <ScopeListView
      title="Workspaces"
      noun="workspace"
      items={showArchived ? items : items.filter((i) => i.status === 'active')}
      showArchived={showArchived}
      onShowArchivedChange={onShow}
      hrefFor={(w) => `/o/a/w/${w.slug}`}
      archive={archive}
      createForm={<div>form</div>}
    />,
  );
  return Object.assign(archive, { onShow });
}

describe('ScopeListView', () => {
  it('requests archived rows via the toggle and shows them when provided', async () => {
    const first = setup();
    expect(screen.queryByText('Legacy')).not.toBeInTheDocument();
    await userEvent.click(screen.getByLabelText('Show archived'));
    expect(first.onShow).toHaveBeenCalledWith(true);
    cleanup();
    setup(undefined, true);
    expect(screen.getByText('Legacy')).toBeInTheDocument();
    expect(screen.getByLabelText('Show archived')).toBeChecked();
  });

  it('requires confirmation before archiving, and Cancel does nothing', async () => {
    const archive = setup();
    await userEvent.click(screen.getByRole('button', { name: /Archive Engineering/ }));
    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveAccessibleName('Archive workspace?');
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(archive).not.toHaveBeenCalled();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('archives the chosen item after confirming', async () => {
    const archive = setup();
    await userEvent.click(screen.getByRole('button', { name: /Archive Engineering/ }));
    await userEvent.click(screen.getByRole('button', { name: 'Archive workspace' }));
    await waitFor(() => expect(archive).toHaveBeenCalledWith(items[0]));
  });

  it('surfaces archive failures (e.g. 403) with the request id', async () => {
    setup(
      vi.fn().mockRejectedValue(
        new ApiError({
          status: 403,
          code: 'forbidden',
          detail: 'Not allowed.',
          requestId: 'rid-9',
        }),
      ),
    );
    await userEvent.click(screen.getByRole('button', { name: /Archive Engineering/ }));
    await userEvent.click(screen.getByRole('button', { name: 'Archive workspace' }));
    expect(await screen.findByText('Not allowed.')).toBeInTheDocument();
    expect(screen.getByText('rid-9')).toBeInTheDocument();
  });
});
