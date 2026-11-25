import {
  assistHref,
  assistSettingsHref,
  boardHref,
  buildHref,
  connectionsHref,
  datasetsHref,
  deliverHref,
  generateHref,
  governHref,
  integrateHref,
  landingHref,
  orgHref,
  projHref,
  projectsHref,
  securityHref,
  settingsHref,
  solutionsHref,
  workspacesHref,
  wsHref,
  type ScopePath,
} from './scope';

export interface NavItem {
  id: string;
  label: string;
  group: string;
  /** Resolve the link for the current scope; null hides the item (not applicable here). */
  href: (scope: ScopePath) => string | null;
  /** Feature area this item belongs to; hidden unless the session enables it. */
  feature: string;
  platformAdminOnly?: boolean;
  /** Shown only when the user holds at least one of these permissions at the current scope (FR-UI-004). */
  requiresAny?: readonly string[];
}

/**
 * Phases 1-3 navigation. Data-driven: later phases append entries here. Only areas that
 * work in this release may appear (FR-UI-003); do not add placeholders.
 */
const security = (id: string, label: string, requiresAny: string[]): NavItem => ({
  id: `security-${id}`,
  label,
  group: 'Security',
  feature: 'security',
  href: (s) => (s.org ? securityHref(s.org, id) : null),
  requiresAny,
});

const integrate = (id: string, label: string, requiresAny: string[]): NavItem => ({
  id: `integrate-${id}`,
  label,
  group: 'Integrate',
  feature: 'integrate',
  href: (s) => (s.org ? integrateHref(s.org, id) : null),
  requiresAny,
});

const build = (id: string, label: string, requiresAny: string[]): NavItem => ({
  id: `build-${id}`,
  label,
  group: 'Build',
  feature: 'build',
  href: (s) => (s.org ? buildHref(s.org, id) : null),
  requiresAny,
});

const govern = (id: string, label: string, requiresAny: string[]): NavItem => ({
  id: `govern-${id}`,
  label,
  group: 'Govern',
  feature: 'governance',
  href: (s) => (s.org ? governHref(s.org, id) : null),
  requiresAny,
});

const operate = (
  id: string,
  label: string,
  requiresAny: string[],
  href: NavItem['href'],
): NavItem => ({
  id: `operate-${id}`,
  label,
  group: 'Operate',
  feature: 'operate',
  href,
  requiresAny,
});

/** Deliver (SP2): no `delivery` feature is registered, so the items ride on `tenancy`. */
const deliver = (id: string, label: string, requiresAny: string[]): NavItem => ({
  id: `deliver-${id}`,
  label,
  group: 'Deliver',
  feature: 'tenancy',
  href: (s) => (s.org ? deliverHref(s.org, id) : null),
  requiresAny,
});

export const NAV_ITEMS: readonly NavItem[] = [
  // Ids name the routes; the screens' display names were swapped at the user's request.
  {
    id: 'command-center',
    feature: 'tenancy',
    label: 'Dashboard',
    group: 'Home',
    href: (s) => (s.org ? landingHref(s.org) : '/'),
  },
  {
    id: 'dashboard',
    feature: 'tenancy',
    label: 'Command Center',
    group: 'Home',
    href: (s) =>
      s.org && s.ws && s.proj
        ? projHref(s.org, s.ws, s.proj)
        : s.org && s.ws
          ? wsHref(s.org, s.ws)
          : s.org
            ? orgHref(s.org)
            : null,
  },
  {
    id: 'workspaces',
    feature: 'tenancy',
    label: 'Workspaces',
    group: 'Administer',
    href: (s) => (s.org ? workspacesHref(s.org) : null),
  },
  {
    id: 'projects',
    feature: 'tenancy',
    label: 'Projects',
    group: 'Administer',
    href: (s) => (s.org && s.ws ? projectsHref(s.org, s.ws) : null),
  },
  {
    id: 'settings-mcp',
    feature: 'tenancy',
    label: 'MCP Access',
    group: 'Administer',
    href: (s) => (s.org ? settingsHref(s.org, 'mcp') : null),
    requiresAny: ['mcp:settings:read'],
  },
  {
    id: 'settings-assist',
    feature: 'tenancy',
    label: 'Assist settings',
    group: 'Administer',
    href: (s) => (s.org ? assistSettingsHref(s.org) : null),
    requiresAny: ['assist:admin', 'assist:install'],
  },
  {
    id: 'organizations',
    feature: 'tenancy',
    label: 'Organizations',
    group: 'Administer',
    href: () => '/platform/organizations',
    platformAdminOnly: true,
  },
  {
    id: 'platform-idps',
    feature: 'tenancy',
    label: 'Platform IdPs',
    group: 'Administer',
    href: () => '/platform/idps',
    platformAdminOnly: true,
  },
  {
    id: 'platform-settings',
    feature: 'tenancy',
    label: 'Platform Settings',
    group: 'Administer',
    href: () => '/platform/settings',
    platformAdminOnly: true,
  },
  {
    id: 'platform-status',
    feature: 'tenancy',
    label: 'Platform Status',
    group: 'Administer',
    href: () => '/platform/status',
  },
  security('users', 'Users', ['member:read']),
  security('groups', 'Groups', ['group:read']),
  security('roles', 'Roles', ['role:read']),
  security('permissions', 'Permissions', ['role:read']),
  security('service-accounts', 'Service Accounts', ['serviceaccount:read']),
  security('api-keys', 'API Keys', ['apikey:read']),
  security('idps', 'Identity Providers', ['idp:manage']),
  security('secrets', 'Secrets', ['secret:read']),
  security('audit', 'Security Audit', ['audit:read']),
  // CM-6: the Connections space lists connections, model providers and delivery targets.
  {
    id: 'integrate-connections',
    label: 'Connections',
    group: 'Integrate',
    feature: 'integrate',
    href: (s) => (s.org ? connectionsHref(s.org) : null),
    requiresAny: ['connection:read', 'model:read', 'delivery:target:read'],
  },
  integrate('models', 'Models', ['model:read']),
  integrate('providers', 'Model Providers', ['model:read']),
  integrate('vendors', 'Vendors', ['vendor:read']),
  integrate('adapters', 'Adapters', ['vendor:read']),
  integrate('egress', 'Egress Policies', ['egress:manage']),
  build('prompts', 'Prompts', ['prompt:read']),
  build('agents', 'Agents', ['agent:read']),
  build('tools', 'Tools', ['tool:read']),
  build('skills', 'Skills', ['skill:read']),
  build('orchestrations', 'Orchestrations', ['orchestration:read']),
  {
    ...build('pipelines', 'Pipelines', ['pipeline:read']),
    feature: 'pipelines',
  },
  {
    ...build('ontologies', 'Ontology', ['ontology:read']),
    feature: 'ontology',
  },
  build('publish-requests', 'Publish Requests', ['approval:decide']),
  // Operate (P5): inside a project the Runs item opens that project's history.
  operate('runs', 'Runs', ['run:read'], (s) =>
    s.org
      ? s.ws && s.proj
        ? `${projHref(s.org, s.ws, s.proj)}/runs`
        : `${orgHref(s.org)}/runs`
      : null,
  ),
  operate('approvals', 'Approvals', ['approval:read'], (s) =>
    s.org ? `${orgHref(s.org)}/approvals` : null,
  ),
  // SP7-7: a project screen; one request and the settings are organization-level routes.
  {
    id: 'assist',
    feature: 'tenancy',
    label: 'Ask SETH',
    group: 'Operate',
    href: (s) => (s.org && s.ws && s.proj ? assistHref(s.org, s.ws, s.proj) : null),
    requiresAny: ['assist:request:read'],
  },
  // SP7-6: the release board, an organization-level screen filtered by `?ws=&proj=`.
  {
    ...deliver('board', 'Board', ['packaging:solution:read']),
    href: (s) => (s.org ? boardHref(s.org) : null),
  },
  deliver('targets', 'Targets', ['delivery:target:read']),
  deliver('executors', 'Executors', ['delivery:executor:read']),
  deliver('plans', 'Plans', ['delivery:plan:read']),
  deliver('approvals', 'Plan approvals', ['delivery:plan:approve']),
  deliver('deployments', 'Deployments', ['delivery:deployment:read']),
  // SP3-7: organization-level screens outside /deliver; the project comes from `?ws=&proj=`.
  {
    ...deliver('solutions', 'Solutions', ['packaging:solution:read']),
    href: (s) => (s.org ? solutionsHref(s.org) : null),
  },
  {
    ...deliver('generate', 'Generate solution', ['appgen:request:read']),
    href: (s) => (s.org ? generateHref(s.org) : null),
  },
  {
    ...deliver('evaluation-datasets', 'Evaluation datasets', ['evaluation:dataset:read']),
    href: (s) => (s.org ? datasetsHref(s.org) : null),
  },
  govern('overview', 'Governance Overview', ['governance:read']),
  govern('usage', 'Token Usage', ['usage:read']),
  govern('costs', 'Cost Explorer', ['usage:read']),
  govern('models', 'Model Consumption', ['usage:read']),
  govern('budgets', 'Budgets & Quotas', ['budget:read']),
  govern('performance', 'Execution Performance', ['ops:read']),
  govern('audit', 'Audit Explorer', ['audit:read']),
  govern('pricing', 'Pricing', ['pricing:read']),
  govern('retention', 'Retention', ['governance:read']),
  govern('decisions', 'Policy Decisions', ['governance:read']),
  govern('policies', 'Governance Policies', ['governance:read']),
  {
    id: 'platform-rate-cards',
    feature: 'governance',
    label: 'Global Rate Cards',
    group: 'Govern',
    href: () => '/platform/rate-cards',
    platformAdminOnly: true,
  },
  {
    id: 'platform-vendors',
    feature: 'integrate',
    label: 'Global Vendors',
    group: 'Integrate',
    href: () => '/platform/vendors',
    platformAdminOnly: true,
  },
  {
    id: 'platform-egress',
    feature: 'integrate',
    label: 'Default Egress',
    group: 'Integrate',
    href: () => '/platform/egress',
    platformAdminOnly: true,
  },
];

export const DEFAULT_FEATURES: readonly string[] = ['tenancy'];

export interface ResolvedNavItem {
  id: string;
  label: string;
  group: string;
  href: string;
}

export function visibleNav(
  scope: ScopePath,
  opts: {
    isPlatformAdmin: boolean;
    permissions?: ReadonlySet<string>;
    /** Session features; undefined (older backend) means tenancy only. */
    features?: readonly string[];
  },
  items: readonly NavItem[] = NAV_ITEMS,
): ResolvedNavItem[] {
  const out: ResolvedNavItem[] = [];
  const features = opts.features ?? DEFAULT_FEATURES;
  for (const item of items) {
    if (!features.includes(item.feature)) continue;
    if (item.platformAdminOnly && !opts.isPlatformAdmin) continue;
    if (item.requiresAny && !item.requiresAny.some((p) => opts.permissions?.has(p))) continue;
    const href = item.href(scope);
    if (href === null) continue;
    out.push({ id: item.id, label: item.label, group: item.group, href });
  }
  return out;
}

/** Group of the nav item the path sits under: the longest match, so a detail page finds its area. */
export function currentNavGroup(
  items: readonly ResolvedNavItem[],
  pathname: string,
): string | undefined {
  // The dashboard's path is the prefix of its whole scope, so only an exact match means that page.
  const matches = items.filter(
    (i) => pathname === i.href || (i.id !== 'dashboard' && pathname.startsWith(`${i.href}/`)),
  );
  return matches.reduce<ResolvedNavItem | undefined>(
    (best, i) => (!best || i.href.length > best.href.length ? i : best),
    undefined,
  )?.group;
}
