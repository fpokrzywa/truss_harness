import { screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { stubApi } from '@/test/security';
import { renderWithClient } from '@/test/utils';
import { Sidebar } from './Sidebar';

vi.mock('next/navigation', () => ({ usePathname: () => '/o/acme' }));
afterEach(() => vi.unstubAllGlobals());

describe('Sidebar Build group (P4)', () => {
  it('lists only the Build areas the user may read', async () => {
    stubApi({}, ['prompt:read', 'tool:read']);
    renderWithClient(<Sidebar isPlatformAdmin={false} />);
    expect(await screen.findByRole('link', { name: 'Prompts', hidden: true })).toHaveAttribute(
      'href',
      '/o/acme/build/prompts',
    );
    expect(screen.getByRole('link', { name: 'Tools', hidden: true })).toHaveAttribute(
      'href',
      '/o/acme/build/tools',
    );
    expect(screen.getByRole('heading', { name: 'Build' })).toBeInTheDocument();
    for (const hidden of ['Agents', 'Skills', 'Publish Requests']) {
      expect(screen.queryByRole('link', { name: hidden, hidden: true })).not.toBeInTheDocument();
    }
  });

  it('shows Publish Requests only with approval:decide', async () => {
    stubApi({}, ['agent:read', 'skill:read', 'approval:decide']);
    renderWithClient(<Sidebar isPlatformAdmin={false} />);
    expect(
      await screen.findByRole('link', { name: 'Publish Requests', hidden: true }),
    ).toHaveAttribute('href', '/o/acme/build/publish-requests');
    expect(screen.getByRole('link', { name: 'Agents', hidden: true })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Skills', hidden: true })).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Prompts', hidden: true })).not.toBeInTheDocument();
  });

  it('hides the group without build permissions', async () => {
    const fetchMock = stubApi({}, ['member:read']);
    renderWithClient(<Sidebar isPlatformAdmin={false} />);
    await screen.findByRole('link', { name: 'Users', hidden: true });
    expect(fetchMock.calls.length).toBeGreaterThan(0);
    expect(screen.queryByRole('heading', { name: 'Build' })).not.toBeInTheDocument();
  });
});
