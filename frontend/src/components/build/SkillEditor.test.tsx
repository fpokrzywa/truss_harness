import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { artifact, stubBuild } from '@/test/build';
import { jsonResponse, renderWithClient } from '@/test/utils';
import { ArtifactDetailView } from './ArtifactDetailView';

// These tests are about editor logic: stack the sections as before. Tabs: SectionTabs.test.tsx.
vi.mock('./SectionTabs', async (original) => ({
  ...(await original<typeof import('./SectionTabs')>()),
  SectionTabs: ({ children }: { children: React.ReactNode }) => children,
}));

vi.mock('next/navigation', () => ({
  useParams: () => ({ org: 'acme' }),
  useRouter: () => ({ push: vi.fn() }),
}));
afterEach(() => vi.unstubAllGlobals());

const S = '/api/v1/orgs/acme/skills/s-1';

function setup(md: string) {
  stubBuild('skills', 's-1', {
    [`GET ${S}`]: () => jsonResponse(artifact({ id: 's-1', name: 'Triage' })),
    [`GET ${S}/draft`]: () =>
      jsonResponse({
        id: 'd-1',
        version_no: 1,
        state: 'draft',
        row_version: 1,
        spec: {
          instructions_md: md,
          required_inputs: [],
          dependencies: [],
          applicable_tools: ['crm.lookup'],
          validation: { max_instruction_chars: 200 },
        },
      }),
  });
  return renderWithClient(<ArtifactDetailView kind="skills" id="s-1" />);
}

describe('skill markdown editor', () => {
  it('renders a markdown preview', async () => {
    setup('# Steps\n\n- check **status**');
    const preview = await screen.findByLabelText('Instructions preview');
    expect(within(preview).getByRole('heading', { name: 'Steps' })).toBeInTheDocument();
    expect(within(preview).getByText('status').tagName).toBe('STRONG');
  });

  it('does not render raw HTML or javascript: links in the preview (XSS)', async () => {
    const { container } = setup(
      'Hello <img src=x onerror="window.__pwned=1"> <script>window.__pwned=1</script>\n\n[click](javascript:window.__pwned=1)',
    );
    const preview = await screen.findByLabelText('Instructions preview');
    expect(preview.querySelector('img')).toBeNull();
    expect(preview.querySelector('script')).toBeNull();
    expect(preview.querySelector('a')).toBeNull();
    expect(preview.textContent).toContain('<img src=x onerror="window.__pwned=1">');
    expect(container.querySelector('[onerror]')).toBeNull();
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
  });

  it('updates the preview as the user types, as inert text', async () => {
    setup('start');
    const user = userEvent.setup();
    const area = await screen.findByLabelText(/Instructions \(markdown\)/);
    await user.click(area);
    await user.paste(' <b onmouseover=x>bold</b>');
    const preview = screen.getByLabelText('Instructions preview');
    expect(preview.querySelector('b')).toBeNull();
    expect(preview.textContent).toContain('<b onmouseover=x>bold</b>');
  });

  it('labels applicable tools as advisory and enforces the length limit', async () => {
    setup('x'.repeat(250));
    expect(
      await screen.findByText(/Applicable tools \(advisory — grants nothing\)/),
    ).toBeInTheDocument();
    expect(screen.getByLabelText('Applicable tool keys (one per line)')).toHaveValue('crm.lookup');
    expect(screen.getByRole('alert')).toHaveTextContent(/250 characters; the limit is 200/);
    expect(screen.getByRole('button', { name: 'Validate' })).toBeDisabled();
  });
});
