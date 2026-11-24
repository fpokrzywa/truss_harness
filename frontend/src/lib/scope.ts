import type { AssistContext } from './api/assist-types';

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
    seg[2] === 'deliver' ||
    seg[2] === 'board' ||
    seg[2] === 'solutions' ||
    seg[2] === 'releases' ||
    seg[2] === 'evaluations' ||
    seg[2] === 'runs' ||
    seg[2] === 'approvals' ||
    seg[2] === 'assist' ||
    seg[2] === 'connections'
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
export const deliverHref = (org: string, area = '') =>
  `${orgHref(org)}/deliver${area ? `/${area}` : ''}`;
/** The release board (SP7); the filter comes from `?ws=&proj=`. */
export const boardHref = (org: string) => `${orgHref(org)}/board`;
/** `?ws=&proj=` query naming the project of an organization-level release screen. */
const projQuery = (ws?: string, proj?: string) =>
  ws && proj ? `?ws=${e(ws)}&proj=${e(proj)}` : '';
export const solutionsHref = (org: string, ws?: string, proj?: string) =>
  `${orgHref(org)}/solutions${projQuery(ws, proj)}`;
export const solutionHref = (org: string, id: string, ws: string, proj: string) =>
  `${orgHref(org)}/solutions/${e(id)}${projQuery(ws, proj)}`;
/** The solution generator (SP5): the request form and list, and one app request. */
export const generateHref = (org: string, ws?: string, proj?: string) =>
  `${orgHref(org)}/solutions/generate${projQuery(ws, proj)}`;
export const appRequestHref = (org: string, id: string, ws?: string, proj?: string) =>
  `${orgHref(org)}/solutions/generate/${e(id)}${projQuery(ws, proj)}`;
export const releaseHref = (org: string, id: string) => `${orgHref(org)}/releases/${e(id)}`;
/** `evaluations/datasets` or one evaluation run (`evaluations/{id}`). */
export const evaluationsHref = (org: string, area = '') =>
  `${orgHref(org)}/evaluations${area ? `/${area}` : ''}`;
export const datasetsHref = (org: string, ws?: string, proj?: string) =>
  `${evaluationsHref(org, 'datasets')}${projQuery(ws, proj)}`;
/** The project's Ask SETH screen (SP7), about one object when a context is given. */
export const assistHref = (org: string, ws: string, proj: string, context?: AssistContext | null) =>
  `${projHref(org, ws, proj)}/assist${context ? `?context=${e(context.kind)}:${e(context.id)}` : ''}`;
export const assistRequestHref = (org: string, id: string) => `${orgHref(org)}/assist/${e(id)}`;
export const assistSettingsHref = (org: string, ws?: string, proj?: string) =>
  `${orgHref(org)}/assist/settings${projQuery(ws, proj)}`;
/** The Connections space (CM-6); the scope comes from `?ws=&proj=`. */
export const connectionsHref = (org: string, ws?: string, proj?: string) =>
  `${orgHref(org)}/connections${projQuery(ws, proj)}`;
export const connectionHref = (org: string, id: string, area = '') =>
  `${orgHref(org)}/connections/${e(id)}${area ? `/${area}` : ''}`;
export const workspacesHref = (org: string) => `${orgHref(org)}/workspaces`;
export const settingsHref = (org: string, area = '') =>
  `${orgHref(org)}/settings${area ? `/${area}` : ''}`;
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
