import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/lib/api/errors';
import { CreateScopeForm } from './CreateScopeForm';

describe('CreateScopeForm (workspace)', () => {
  it('derives the slug from the name until the user edits it', async () => {
    render(<CreateScopeForm noun="workspace" onSubmit={vi.fn()} />);
    await userEvent.type(screen.getByLabelText('Name'), 'Data Platform');
    expect(screen.getByLabelText('Slug')).toHaveValue('data-platform');
    await userEvent.clear(screen.getByLabelText('Slug'));
    await userEvent.type(screen.getByLabelText('Slug'), 'dp');
    await userEvent.type(screen.getByLabelText('Name'), ' 2');
    expect(screen.getByLabelText('Slug')).toHaveValue('dp');
  });

  it.each(['Bad Slug', '-lead', 'trail-', 'UPPER', 'under_score'])(
    'rejects invalid slug %j without calling the API',
    async (slug) => {
      const onSubmit = vi.fn();
      render(<CreateScopeForm noun="workspace" onSubmit={onSubmit} />);
      await userEvent.type(screen.getByLabelText('Name'), 'Valid Name');
      await userEvent.clear(screen.getByLabelText('Slug'));
      await userEvent.type(screen.getByLabelText('Slug'), slug);
      await userEvent.click(screen.getByRole('button', { name: 'Create workspace' }));
      expect(screen.getByLabelText('Slug')).toHaveAccessibleDescription(
        /lowercase letters, digits or hyphens/,
      );
      expect(onSubmit).not.toHaveBeenCalled();
    },
  );

  it('requires name and slug', async () => {
    const onSubmit = vi.fn();
    render(<CreateScopeForm noun="workspace" onSubmit={onSubmit} />);
    await userEvent.click(screen.getByRole('button', { name: 'Create workspace' }));
    expect(screen.getByText('Name is required.')).toBeInTheDocument();
    expect(screen.getByText('Slug is required.')).toBeInTheDocument();
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it('submits trimmed values and resets on success', async () => {
    const onSubmit = vi.fn().mockResolvedValue({});
    render(<CreateScopeForm noun="workspace" onSubmit={onSubmit} />);
    await userEvent.type(screen.getByLabelText('Name'), '  Engineering ');
    await userEvent.type(screen.getByLabelText('Description (optional)'), ' core team ');
    await userEvent.click(screen.getByRole('button', { name: 'Create workspace' }));
    expect(onSubmit).toHaveBeenCalledWith({
      slug: 'engineering',
      name: 'Engineering',
      description: 'core team',
    });
    await waitFor(() => expect(screen.getByLabelText('Name')).toHaveValue(''));
  });

  it('maps a 409 to a slug field error', async () => {
    const onSubmit = vi.fn().mockRejectedValue(new ApiError({ status: 409, code: 'conflict' }));
    render(<CreateScopeForm noun="workspace" onSubmit={onSubmit} />);
    await userEvent.type(screen.getByLabelText('Name'), 'Taken');
    await userEvent.click(screen.getByRole('button', { name: 'Create workspace' }));
    expect(await screen.findByText(/slug is already in use/)).toBeInTheDocument();
  });

  it('handles a 403 gracefully by explaining and disabling the button', async () => {
    const onSubmit = vi.fn().mockRejectedValue(new ApiError({ status: 403, code: 'forbidden' }));
    render(<CreateScopeForm noun="workspace" onSubmit={onSubmit} />);
    await userEvent.type(screen.getByLabelText('Name'), 'Nope');
    await userEvent.click(screen.getByRole('button', { name: 'Create workspace' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'do not have permission to create a workspace',
    );
    expect(screen.getByRole('button', { name: 'Create workspace' })).toBeDisabled();
  });
});
