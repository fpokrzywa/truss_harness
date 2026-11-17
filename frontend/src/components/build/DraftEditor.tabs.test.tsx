import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { artifact, stubBuild } from '@/test/build';
import { jsonResponse, renderWithClient } from '@/test/utils';
import { ArtifactDetailView } from './ArtifactDetailView';

vi.mock('next/navigation', () => ({
  useParams: () => ({ org: 'acme' }),
  useRouter: () => ({ push: vi.fn() }),
}));
afterEach(() => vi.unstubAllGlobals());

const S = '/api/v1/orgs/acme/skills/s-1';

function setup() {
  stubBuild('skills', 's-1', {
    [`GET ${S}`]: () => jsonResponse(artifact({ id: 's-1', name: 'Triage' })),
    [`GET ${S}/draft`]: () =>
      jsonResponse({
        id: 'd-1',
        version_no: 1,
        state: 'draft',
        row_version: 1,
        spec: {
          instructions_md: 'Steps',
          required_inputs: [],
          dependencies: [],
          applicable_tools: [],
        },
      }),
  });
  return renderWithClient(<ArtifactDetailView kind="skills" id="s-1" />);
}

describe('Build editor section tabs', () => {
  it('shows the editor sections as tabs under the Editor tab', async () => {
    setup();
    const tablist = await screen.findByRole('tablist', { name: 'Editor sections' });
    const names = [...tablist.querySelectorAll('[role=tab]')].map((t) => t.textContent);
    expect(names.slice(0, 3)).toEqual(['Instructions', 'Required inputs', 'Dependencies']);
    expect(screen.getByRole('tab', { name: 'Editor' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('tab', { name: 'Instructions' })).toHaveAttribute(
      'aria-selected',
      'true',
    );
    expect(screen.getAllByRole('tabpanel', { name: 'Instructions' })).toHaveLength(1);
  });

  it('keeps Save draft reachable and an unsaved edit intact from any section', async () => {
    const user = userEvent.setup();
    setup();
    await screen.findByRole('tablist', { name: 'Editor sections' });
    const instructions = screen.getByRole('textbox', { name: /instructions/i });
    await user.type(instructions, ' more');
    expect(screen.getByText('Unsaved changes')).toBeVisible();

    await user.click(screen.getByRole('tab', { name: 'Dependencies' }));
    expect(screen.getByRole('button', { name: 'Save draft' })).toBeEnabled();
    expect(instructions).not.toBeVisible();

    await user.click(screen.getByRole('tab', { name: 'Instructions' }));
    expect(screen.getByRole('textbox', { name: /instructions/i })).toHaveValue('Steps more');
  });
});
