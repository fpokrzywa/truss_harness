export type ScopeArea = 'dashboard' | 'command' | 'workspaces' | 'projects' | 'platform' | 'other';

export interface ScopePath {
  org?: string;
  ws?: string;
  proj?: string;
  area: ScopeArea;
}

/** Parse `/o/{org}/w/{ws}/p/{proj}` style URLs into the current scope and area. */
export function parseScope(pathname: string): ScopePath {
  const seg = pathname.split('/').filter(Boolean).map(decodeURIComponent);
  if (seg[0] === 'platform') return { area: 'platform' };
  if (seg[0] !== 'o' || !seg[1]) return { area: 'other' };
  const org = seg[1];
  if (seg[2] === 'workspaces') return { org, area: 'workspaces' };
  if (seg[2] === 'command') return { org, area: 'command' };
  if (
    seg[2] === 'security' ||
    seg[2] === 'integrate' ||
    seg[2] === 'build' ||
    seg[2] === 'govern' ||
    seg[2] === 'runs' ||
    seg[2] === 'approvals'
  )
    return { org, area: 'other' };
  if (seg[2] !== 'w' || !seg[3]) return { org, area: 'dashboard' };
  const ws = seg[3];
  if (seg[4] === 'projects') return { org, ws, area: 'projects' };
  if (seg[4] === 'p' && seg[5]) return { org, ws, proj: seg[5], area: 'dashboard' };
  return { org, ws, area: 'dashboard' };
}

const e = encodeURIComponent;

export const orgHref = (org: string) => `/o/${e(org)}`;
/** Where signing in and `/` land: the screen named Dashboard, on the `command` route. */
export const landingHref = (org: string) => `${orgHref(org)}/command`;
export const wsHref = (org: string, ws: string) => `${orgHref(org)}/w/${e(ws)}`;
export const projHref = (org: string, ws: string, proj: string) =>
  `${wsHref(org, ws)}/p/${e(proj)}`;
export const securityHref = (org: string, area = '') =>
  `${orgHref(org)}/security${area ? `/${area}` : ''}`;
export const integrateHref = (org: string, area = '') =>
  `${orgHref(org)}/integrate${area ? `/${area}` : ''}`;
export const buildHref = (org: string, area = '') =>
  `${orgHref(org)}/build${area ? `/${area}` : ''}`;
export const governHref = (org: string, area = '') =>
  `${orgHref(org)}/govern${area ? `/${area}` : ''}`;
export const workspacesHref = (org: string) => `${orgHref(org)}/workspaces`;
export const projectsHref = (org: string, ws: string) => `${wsHref(org, ws)}/projects`;

export type ScopeLevel = 'org' | 'ws' | 'proj';

/**
 * Where to navigate when a selector changes. Keeps the same area when it exists in the
 * new scope (list pages), otherwise lands on the new scope's dashboard. An empty value
 * means "clear this level" and returns to the parent scope's dashboard.
 */
export function scopeChangeTarget(
  current: ScopePath,
  level: ScopeLevel,
  value: string,
): string | null {
  if (level === 'org') {
    if (!value) return null;
    if (current.area === 'command') return landingHref(value);
    return current.area === 'workspaces' ? workspacesHref(value) : orgHref(value);
  }
  if (!current.org) return null;
  if (level === 'ws') {
    if (!value) return orgHref(current.org);
    return current.area === 'projects'
      ? projectsHref(current.org, value)
      : wsHref(current.org, value);
  }
  if (!current.ws) return null;
  return value ? projHref(current.org, current.ws, value) : wsHref(current.org, current.ws);
}
