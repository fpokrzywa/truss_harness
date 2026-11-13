/** Sidebar size and open state: a per-browser preference, so bad or blocked storage just means defaults. */

export interface SidebarLayout {
  width: number;
  collapsed: boolean;
}

export const SIDEBAR_MIN_WIDTH = 180;
export const SIDEBAR_MAX_WIDTH = 420;
export const SIDEBAR_DEFAULT_WIDTH = 224;

const STORAGE_KEY = 'shifde.sidebar.layout';

export const clampSidebarWidth = (width: number): number =>
  Math.round(Math.min(SIDEBAR_MAX_WIDTH, Math.max(SIDEBAR_MIN_WIDTH, width)));

export function readSidebarLayout(): SidebarLayout {
  const defaults = { width: SIDEBAR_DEFAULT_WIDTH, collapsed: false };
  try {
    const parsed: unknown = JSON.parse(window.localStorage.getItem(STORAGE_KEY) ?? '{}');
    if (typeof parsed !== 'object' || parsed === null) return defaults;
    const { width, collapsed } = parsed as Partial<Record<keyof SidebarLayout, unknown>>;
    return {
      width:
        typeof width === 'number' && Number.isFinite(width)
          ? clampSidebarWidth(width)
          : defaults.width,
      collapsed: collapsed === true,
    };
  } catch {
    return defaults;
  }
}

export function saveSidebarLayout(layout: SidebarLayout): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(layout));
  } catch {
    // Storage may be blocked or full; the sidebar still works for this visit.
  }
}
