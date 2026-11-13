import { fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it } from 'vitest';
import { SIDEBAR_DEFAULT_WIDTH, SIDEBAR_MAX_WIDTH, SIDEBAR_MIN_WIDTH } from '@/lib/sidebar-layout';
import { SidebarFrame } from './SidebarFrame';

const KEY = 'shifde.sidebar.layout';
const show = () =>
  render(
    <SidebarFrame>
      {(rail) =>
        rail.collapsed ? (
          <button type="button" onClick={rail.expand}>
            Icon
          </button>
        ) : (
          <a href="/x">Item</a>
        )
      }
    </SidebarFrame>,
  );
const handle = () => screen.getByRole('separator', { name: 'Resize navigation' });

beforeEach(() => window.localStorage.clear());

describe('SidebarFrame', () => {
  it('closes and reopens the navigation, and remembers it', async () => {
    const user = userEvent.setup();
    const first = show();
    expect(screen.getByRole('link', { name: 'Item' })).toBeVisible();

    await user.click(screen.getByRole('button', { name: 'Close navigation' }));
    expect(screen.queryByRole('link', { name: 'Item' })).toBeNull();
    expect(screen.queryByRole('separator')).toBeNull();
    first.unmount();

    show();
    const reopen = screen.getByRole('button', { name: 'Open navigation' });
    expect(reopen).toHaveAttribute('aria-expanded', 'false');
    await user.click(reopen);
    expect(screen.getByRole('link', { name: 'Item' })).toBeVisible();
  });

  it('lets the closed rail reopen the navigation', async () => {
    const user = userEvent.setup();
    show();
    await user.click(screen.getByRole('button', { name: 'Close navigation' }));
    await user.click(screen.getByRole('button', { name: 'Icon' }));
    expect(screen.getByRole('link', { name: 'Item' })).toBeVisible();
  });

  it('resizes from the keyboard within its limits, and remembers it', () => {
    const first = show();
    expect(handle()).toHaveAttribute('aria-valuenow', String(SIDEBAR_DEFAULT_WIDTH));
    fireEvent.keyDown(handle(), { key: 'ArrowRight' });
    expect(handle()).toHaveAttribute('aria-valuenow', String(SIDEBAR_DEFAULT_WIDTH + 16));
    fireEvent.keyDown(handle(), { key: 'ArrowLeft' });
    fireEvent.keyDown(handle(), { key: 'End' });
    expect(handle()).toHaveAttribute('aria-valuenow', String(SIDEBAR_MAX_WIDTH));
    fireEvent.keyDown(handle(), { key: 'ArrowRight' });
    expect(handle()).toHaveAttribute('aria-valuenow', String(SIDEBAR_MAX_WIDTH));
    first.unmount();

    show();
    expect(handle()).toHaveAttribute('aria-valuenow', String(SIDEBAR_MAX_WIDTH));
    fireEvent.keyDown(handle(), { key: 'Home' });
    expect(handle()).toHaveAttribute('aria-valuenow', String(SIDEBAR_MIN_WIDTH));
  });

  it('resets the width on double-click', () => {
    window.localStorage.setItem(KEY, JSON.stringify({ width: 300, collapsed: false }));
    show();
    fireEvent.doubleClick(handle());
    expect(handle()).toHaveAttribute('aria-valuenow', String(SIDEBAR_DEFAULT_WIDTH));
  });

  it.each([
    ['not json', SIDEBAR_DEFAULT_WIDTH],
    ['null', SIDEBAR_DEFAULT_WIDTH],
    ['{"width":"wide"}', SIDEBAR_DEFAULT_WIDTH],
    ['{"width":99999}', SIDEBAR_MAX_WIDTH],
    ['{"width":1}', SIDEBAR_MIN_WIDTH],
  ])('survives a stored value of %s', (stored, expected) => {
    window.localStorage.setItem(KEY, stored);
    show();
    expect(handle()).toHaveAttribute('aria-valuenow', String(expected));
  });
});
