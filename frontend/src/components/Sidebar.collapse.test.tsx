import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { stubApi } from '@/test/security';
import { renderWithClient } from '@/test/utils';
import { Sidebar } from './Sidebar';

let pathname = '/o/acme';
vi.mock('next/navigation', () => ({ usePathname: () => pathname }));

const section = (name: string) => screen.findByRole('button', { name });
const link = (name: string) => screen.queryByRole('link', { name });
const openSections = () =>
  screen
    .getAllByRole('button')
    .filter((b) => b.getAttribute('aria-expanded') === 'true')
    .map((b) => b.textContent);

beforeEach(() => {
  pathname = '/o/acme';
  stubApi({}, ['member:read', 'audit:read']);
});
afterEach(() => vi.unstubAllGlobals());

describe('Sidebar sections', () => {
  it('opens only Home on the dashboard', async () => {
    renderWithClient(<Sidebar isPlatformAdmin={false} />);
    await section('Security');
    expect(openSections()).toEqual(['Home']);
    expect(link('Dashboard')).toBeVisible();
    expect(link('Users')).toBeNull();
  });

  it('opens only the section holding the current page, including its detail pages', async () => {
    pathname = '/o/acme/security/users/u1';
    renderWithClient(<Sidebar isPlatformAdmin={false} />);
    await section('Security');
    expect(openSections()).toEqual(['Security']);
    expect(link('Users')).toBeVisible();
    expect(link('Dashboard')).toBeNull();
  });

  it('closes the open section when another is opened', async () => {
    const user = userEvent.setup();
    renderWithClient(<Sidebar isPlatformAdmin={false} />);
    await user.click(await section('Security'));
    expect(openSections()).toEqual(['Security']);
    expect(link('Users')).toBeVisible();
    expect(link('Dashboard')).toBeNull();

    await user.click(await section('Home'));
    expect(openSections()).toEqual(['Home']);
    expect(link('Users')).toBeNull();
  });

  it('closes the open section when it is clicked again', async () => {
    const user = userEvent.setup();
    renderWithClient(<Sidebar isPlatformAdmin={false} />);
    await section('Security');
    await user.click(await section('Home'));
    expect(openSections()).toEqual([]);
  });

  it('opens the new page section after navigating, replacing any manual choice', async () => {
    const user = userEvent.setup();
    renderWithClient(<Sidebar isPlatformAdmin={false} />);
    await section('Security');
    await user.click(await section('Home')); // closed by hand on the dashboard
    expect(openSections()).toEqual([]);

    pathname = '/o/acme/security/users';
    await user.click(await section('Home')); // any render picks up the new path
    expect(openSections()).toEqual(['Security']);
  });

  it('shows one named icon per section when closed, with no links', async () => {
    renderWithClient(<Sidebar isPlatformAdmin={false} collapsed />);
    expect(await section('Open Security')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Open Home' })).toHaveTextContent('Home');
    expect(screen.queryAllByRole('link')).toEqual([]);
  });

  it('reopens on the section whose icon was clicked', async () => {
    const user = userEvent.setup();
    function Harness() {
      const [collapsed, setCollapsed] = useState(true);
      return (
        <Sidebar
          isPlatformAdmin={false}
          collapsed={collapsed}
          onExpand={() => setCollapsed(false)}
        />
      );
    }
    renderWithClient(<Harness />);
    await user.click(await section('Open Security'));
    expect(openSections()).toEqual(['Security']);
    expect(link('Users')).toBeVisible();
    expect(link('Dashboard')).toBeNull();
  });

  it('keeps the organization sections on a platform page, with the page section open', async () => {
    pathname = '/platform/status';
    renderWithClient(<Sidebar isPlatformAdmin={false} />);
    expect(await section('Security')).toHaveAttribute('aria-expanded', 'false');
    expect(openSections()).toEqual(['Administer']);
    expect(link('Platform Status')).toBeVisible();
    expect(screen.getByRole('link', { name: 'Workspaces' })).toHaveAttribute(
      'href',
      '/o/acme/workspaces',
    );
  });

  it('falls back to Home for a page outside the navigation', async () => {
    pathname = '/o/acme/not-in-nav';
    renderWithClient(<Sidebar isPlatformAdmin={false} />);
    await section('Security');
    expect(openSections()).toEqual(['Home']);
  });
});
