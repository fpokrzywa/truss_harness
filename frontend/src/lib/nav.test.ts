import { describe, expect, it } from 'vitest';
import { NAV_ITEMS, currentNavGroup, visibleNav as rawVisibleNav } from './nav';
import { connectionHref, connectionsHref, parseScope, scopeChangeTarget } from './scope';

const ALL_FEATURES = ['tenancy', 'security', 'integrate', 'build', 'operate', 'governance'];
/** Existing expectations assume every feature is enabled; feature gating is tested separately. */
const visibleNav: typeof rawVisibleNav = (scope, opts, items) =>
  rawVisibleNav(scope, { features: ALL_FEATURES, ...opts }, items);

const UNIMPLEMENTED: string[] = [];

describe('navigation config (FR-UI-003)', () => {
  it('contains only Phase 1-8 areas', () => {
    const ids = NAV_ITEMS.map((i) => i.id).sort();
    expect(ids).toEqual([
      'build-agents',
      'build-ontologies',
      'build-orchestrations',
      'build-pipelines',
      'build-prompts',
      'build-publish-requests',
      'build-skills',
      'build-tools',
      'command-center',
      'dashboard',
      'deliver-approvals',
      'deliver-deployments',
      'deliver-evaluation-datasets',
      'deliver-executors',
      'deliver-plans',
      'deliver-solutions',
      'deliver-targets',
      'govern-audit',
      'govern-budgets',
      'govern-costs',
      'govern-decisions',
      'govern-models',
      'govern-overview',
      'govern-performance',
      'govern-policies',
      'govern-pricing',
      'govern-retention',
      'govern-usage',
      'integrate-adapters',
      'integrate-connections',
      'integrate-egress',
      'integrate-models',
      'integrate-providers',
      'integrate-vendors',
      'operate-approvals',
      'operate-runs',
      'organizations',
      'platform-egress',
      'platform-idps',
      'platform-rate-cards',
      'platform-settings',
      'platform-status',
      'platform-vendors',
      'projects',
      'security-api-keys',
      'security-audit',
      'security-groups',
      'security-idps',
      'security-permissions',
      'security-roles',
      'security-secrets',
      'security-service-accounts',
      'security-users',
      'settings-mcp',
      'workspaces',
    ]);
    for (const bad of UNIMPLEMENTED) {
      expect(NAV_ITEMS.some((i) => i.id.includes(bad) || i.label.toLowerCase().includes(bad))).toBe(
        false,
      );
    }
  });

  it('shows Organizations to platform admins only', () => {
    const scope = parseScope('/o/acme');
    expect(visibleNav(scope, { isPlatformAdmin: false }).map((i) => i.label)).not.toContain(
      'Organizations',
    );
    expect(visibleNav(scope, { isPlatformAdmin: true }).map((i) => i.label)).toContain(
      'Organizations',
    );
  });

  it('resolves links from scope and hides Projects without a workspace', () => {
    const org = visibleNav(parseScope('/o/acme'), { isPlatformAdmin: false });
    expect(org.find((i) => i.id === 'dashboard')?.href).toBe('/o/acme');
    expect(org.find((i) => i.id === 'workspaces')?.href).toBe('/o/acme/workspaces');
    expect(org.some((i) => i.id === 'projects')).toBe(false);

    const ws = visibleNav(parseScope('/o/acme/w/eng/p/api'), { isPlatformAdmin: false });
    expect(ws.find((i) => i.id === 'dashboard')?.href).toBe('/o/acme/w/eng/p/api');
    expect(ws.find((i) => i.id === 'projects')?.href).toBe('/o/acme/w/eng/projects');

    const platform = visibleNav(parseScope('/platform/status'), { isPlatformAdmin: false });
    expect(platform.map((i) => i.id)).toEqual(['command-center', 'platform-status']);
  });
});

describe('scope parsing and selector navigation', () => {
  it('parses scope and area from the URL', () => {
    expect(parseScope('/o/acme/w/eng/p/api')).toEqual({
      org: 'acme',
      ws: 'eng',
      proj: 'api',
      area: 'dashboard',
    });
    expect(parseScope('/o/acme/workspaces')).toEqual({ org: 'acme', area: 'workspaces' });
    expect(parseScope('/o/acme/w/eng/projects')).toEqual({
      org: 'acme',
      ws: 'eng',
      area: 'projects',
    });
    expect(parseScope('/platform/status').area).toBe('platform');
  });

  it('keeps list areas when switching scope, else goes to the dashboard', () => {
    expect(scopeChangeTarget(parseScope('/o/a/workspaces'), 'org', 'b')).toBe('/o/b/workspaces');
    expect(scopeChangeTarget(parseScope('/o/a/command'), 'org', 'b')).toBe('/o/b/command');
    expect(scopeChangeTarget(parseScope('/o/a/w/x/p/y'), 'org', 'b')).toBe('/o/b');
    expect(scopeChangeTarget(parseScope('/o/a/w/x/projects'), 'ws', 'z')).toBe('/o/a/w/z/projects');
    expect(scopeChangeTarget(parseScope('/o/a/w/x/p/y'), 'ws', 'z')).toBe('/o/a/w/z');
    expect(scopeChangeTarget(parseScope('/o/a/w/x/p/y'), 'ws', '')).toBe('/o/a');
    expect(scopeChangeTarget(parseScope('/o/a/w/x'), 'proj', 'p1')).toBe('/o/a/w/x/p/p1');
    expect(scopeChangeTarget(parseScope('/o/a/w/x/p/y'), 'proj', '')).toBe('/o/a/w/x');
  });
});

describe('permission-aware navigation (FR-UI-004)', () => {
  const scope = parseScope('/o/acme');
  const ids = (perms?: string[]) =>
    visibleNav(scope, {
      isPlatformAdmin: false,
      permissions: perms ? new Set(perms) : undefined,
    })
      .filter((i) => i.group === 'Security')
      .map((i) => i.id);

  it('hides the whole Security group without permissions or before they load', () => {
    expect(ids(undefined)).toEqual([]);
    expect(ids([])).toEqual([]);
  });

  it('shows only the areas the permissions allow', () => {
    expect(ids(['member:read', 'audit:read'])).toEqual(['security-users', 'security-audit']);
    expect(ids(['role:read'])).toEqual(['security-roles', 'security-permissions']);
    expect(ids(['secret:manage'])).toEqual([]); // manage alone does not grant the read-level area
  });

  it('never links Security areas outside an organization scope', () => {
    const platform = visibleNav(parseScope('/platform/status'), {
      isPlatformAdmin: true,
      permissions: new Set(['member:read']),
    });
    expect(platform.some((i) => i.group === 'Security')).toBe(false);
    expect(platform.some((i) => i.id === 'platform-idps')).toBe(true);
  });

  describe('Platform Settings (P9, FR-UI-010)', () => {
    it('is shown to platform administrators only, linking to /platform/settings', () => {
      const scope = parseScope('/platform/status');
      const admin = visibleNav(scope, { isPlatformAdmin: true });
      expect(admin.find((i) => i.id === 'platform-settings')).toMatchObject({
        label: 'Platform Settings',
        group: 'Administer',
        href: '/platform/settings',
      });
      expect(visibleNav(scope, { isPlatformAdmin: false }).map((i) => i.id)).not.toContain(
        'platform-settings',
      );
    });
  });

  describe('Build group (P4)', () => {
    const scope = parseScope('/o/acme');
    const labels = (perms: string[]) =>
      visibleNav(scope, { isPlatformAdmin: false, permissions: new Set(perms) })
        .filter((i) => i.group === 'Build')
        .map((i) => i.label);

    it('shows nothing without build permissions or before they load', () => {
      expect(labels(['member:read'])).toEqual([]);
      expect(visibleNav(scope, { isPlatformAdmin: false }).some((i) => i.group === 'Build')).toBe(
        false,
      );
    });

    it('gates each item on its read permission', () => {
      expect(labels(['prompt:read'])).toEqual(['Prompts']);
      expect(labels(['agent:read'])).toEqual(['Agents']);
      expect(labels(['tool:read'])).toEqual(['Tools']);
      expect(labels(['skill:read'])).toEqual(['Skills']);
      expect(labels(['prompt:update'])).toEqual([]);
    });

    it('shows Publish Requests only to approvers and links into /build', () => {
      expect(labels(['approval:decide'])).toEqual(['Publish Requests']);
      const items = visibleNav(scope, {
        isPlatformAdmin: false,
        permissions: new Set(['prompt:read', 'approval:decide']),
      });
      expect(items.find((i) => i.label === 'Prompts')?.href).toBe('/o/acme/build/prompts');
      expect(items.find((i) => i.label === 'Publish Requests')?.href).toBe(
        '/o/acme/build/publish-requests',
      );
    });
  });

  describe('Operate group (P5)', () => {
    const labels = (perms: string[], path = '/o/acme') =>
      visibleNav(parseScope(path), { isPlatformAdmin: false, permissions: new Set(perms) })
        .filter((i) => i.group === 'Operate')
        .map((i) => `${i.label}=${i.href}`);

    it('hides the group without run/approval read permissions', () => {
      expect(labels(['member:read', 'agent:execute'])).toEqual([]);
      expect(
        visibleNav(parseScope('/o/acme'), { isPlatformAdmin: false }).some(
          (i) => i.group === 'Operate',
        ),
      ).toBe(false);
    });

    it('gates Runs on run:read and Approvals on approval:read', () => {
      expect(labels(['run:read'])).toEqual(['Runs=/o/acme/runs']);
      expect(labels(['approval:read'])).toEqual(['Approvals=/o/acme/approvals']);
    });

    it('points Runs at the project history inside a project', () => {
      expect(labels(['run:read'], '/o/acme/w/eng/p/api')).toEqual([
        'Runs=/o/acme/w/eng/p/api/runs',
      ]);
    });

    it('gates Orchestrations (Build) on orchestration:read', () => {
      const items = visibleNav(parseScope('/o/acme'), {
        isPlatformAdmin: false,
        permissions: new Set(['orchestration:read']),
      });
      expect(items.find((i) => i.id === 'build-orchestrations')?.href).toBe(
        '/o/acme/build/orchestrations',
      );
    });
  });

  describe('Pipelines (P6)', () => {
    const scope = parseScope('/o/acme');
    const find = (opts: Parameters<typeof rawVisibleNav>[1]) =>
      rawVisibleNav(scope, opts).find((i) => i.id === 'build-pipelines');

    it('needs both the pipelines feature and pipeline:read', () => {
      const perms = new Set(['pipeline:read']);
      expect(
        find({ isPlatformAdmin: false, permissions: perms, features: ['tenancy', 'build'] }),
      ).toBeUndefined();
      expect(
        find({
          isPlatformAdmin: false,
          permissions: new Set(['agent:read']),
          features: ['tenancy', 'build', 'pipelines'],
        }),
      ).toBeUndefined();
      expect(
        find({
          isPlatformAdmin: false,
          permissions: perms,
          features: ['tenancy', 'build', 'pipelines'],
        })?.href,
      ).toBe('/o/acme/build/pipelines');
    });

    it('is grouped under Build', () => {
      expect(NAV_ITEMS.find((i) => i.id === 'build-pipelines')?.group).toBe('Build');
    });
  });

  describe('Ontology (P7)', () => {
    const scope = parseScope('/o/acme');
    const find = (opts: Parameters<typeof rawVisibleNav>[1]) =>
      rawVisibleNav(scope, opts).find((i) => i.id === 'build-ontologies');

    it('needs the ontology feature and ontology:read', () => {
      const perms = new Set(['ontology:read']);
      expect(
        find({ isPlatformAdmin: false, permissions: perms, features: ['tenancy', 'build'] }),
      ).toBeUndefined();
      expect(
        find({
          isPlatformAdmin: false,
          permissions: new Set(['agent:read']),
          features: ['tenancy', 'build', 'ontology'],
        }),
      ).toBeUndefined();
      expect(
        find({
          isPlatformAdmin: false,
          permissions: perms,
          features: ['tenancy', 'build', 'ontology'],
        })?.href,
      ).toBe('/o/acme/build/ontologies');
    });

    it('is grouped under Build', () => {
      expect(NAV_ITEMS.find((i) => i.id === 'build-ontologies')?.group).toBe('Build');
    });
  });

  describe('Deliver group (SP2)', () => {
    const scope = parseScope('/o/acme');
    const items = (perms: string[]) =>
      visibleNav(scope, { isPlatformAdmin: false, permissions: new Set(perms) })
        .filter((i) => i.group === 'Deliver')
        .map((i) => `${i.label}=${i.href}`);

    it('hides the group without delivery read permissions', () => {
      expect(items(['member:read', 'delivery:target:write', 'delivery:executor:manage'])).toEqual(
        [],
      );
      expect(visibleNav(scope, { isPlatformAdmin: false }).some((i) => i.group === 'Deliver')).toBe(
        false,
      );
    });

    it('gates Targets on delivery:target:read and Executors on delivery:executor:read', () => {
      expect(items(['delivery:target:read'])).toEqual(['Targets=/o/acme/deliver/targets']);
      expect(items(['delivery:executor:read'])).toEqual(['Executors=/o/acme/deliver/executors']);
      expect(items(['delivery:target:read', 'delivery:executor:read'])).toEqual([
        'Targets=/o/acme/deliver/targets',
        'Executors=/o/acme/deliver/executors',
      ]);
    });

    it('gates Plans, Plan approvals and Deployments on their own permissions', () => {
      expect(items(['delivery:plan:read'])).toEqual(['Plans=/o/acme/deliver/plans']);
      expect(items(['delivery:plan:approve'])).toEqual([
        'Plan approvals=/o/acme/deliver/approvals',
      ]);
      expect(items(['delivery:deployment:read'])).toEqual([
        'Deployments=/o/acme/deliver/deployments',
      ]);
      expect(items(['delivery:plan:request', 'delivery:deployment:resolve'])).toEqual([]);
      expect(
        items([
          'delivery:target:read',
          'delivery:executor:read',
          'delivery:plan:read',
          'delivery:plan:approve',
          'delivery:deployment:read',
        ]),
      ).toEqual([
        'Targets=/o/acme/deliver/targets',
        'Executors=/o/acme/deliver/executors',
        'Plans=/o/acme/deliver/plans',
        'Plan approvals=/o/acme/deliver/approvals',
        'Deployments=/o/acme/deliver/deployments',
      ]);
    });

    it('rides on the tenancy feature, after Operate, and never links outside an organization', () => {
      const perms = new Set(['run:read', 'delivery:target:read', 'governance:read']);
      const groups = [
        ...new Set(
          rawVisibleNav(scope, {
            isPlatformAdmin: false,
            permissions: perms,
            features: ['tenancy', 'operate', 'governance'],
          }).map((i) => i.group),
        ),
      ];
      expect(groups).toEqual(['Home', 'Administer', 'Operate', 'Deliver', 'Govern']);
      expect(
        visibleNav(parseScope('/platform/status'), {
          isPlatformAdmin: true,
          permissions: perms,
        }).some((i) => i.group === 'Deliver'),
      ).toBe(false);
    });

    it('treats /o/{org}/deliver/... as an organization-level area', () => {
      expect(parseScope('/o/acme/deliver/targets')).toEqual({ org: 'acme', area: 'other' });
      expect(parseScope('/o/acme/deliver/executors')).toEqual({ org: 'acme', area: 'other' });
    });

    it('gates Solutions and Evaluation datasets (SP3-7) on their read permissions', () => {
      expect(items(['delivery:target:read'])).not.toContain('Solutions=/o/acme/solutions');
      expect(items(['packaging:solution:write'])).toEqual([]);
      expect(items(['packaging:solution:read'])).toEqual(['Solutions=/o/acme/solutions']);
      expect(items(['evaluation:dataset:read'])).toEqual([
        'Evaluation datasets=/o/acme/evaluations/datasets',
      ]);
    });

    it('treats /o/{org}/solutions, releases and evaluations as organization-level areas', () => {
      expect(parseScope('/o/acme/solutions')).toEqual({ org: 'acme', area: 'other' });
      expect(parseScope('/o/acme/solutions/s-1')).toEqual({ org: 'acme', area: 'other' });
      expect(parseScope('/o/acme/releases/x')).toEqual({ org: 'acme', area: 'other' });
      expect(parseScope('/o/acme/evaluations/datasets')).toEqual({ org: 'acme', area: 'other' });
      expect(parseScope('/o/acme/evaluations/r-1')).toEqual({ org: 'acme', area: 'other' });
    });
  });

  describe('Integrate group (P3)', () => {
    const scope = parseScope('/o/acme');
    const labels = (perms: string[], admin = false) =>
      visibleNav(scope, { isPlatformAdmin: admin, permissions: new Set(perms) })
        .filter((i) => i.group === 'Integrate')
        .map((i) => i.label);

    it('shows nothing without integrate permissions', () => {
      expect(labels(['member:read'])).toEqual([]);
      expect(
        visibleNav(scope, { isPlatformAdmin: false }).some((i) => i.group === 'Integrate'),
      ).toBe(false);
    });

    it('gates each item on its read permission', () => {
      expect(labels(['vendor:read'])).toEqual(['Vendors', 'Adapters']);
      expect(labels(['model:read'])).toEqual(['Connections', 'Models', 'Model Providers']);
      expect(labels(['connection:read'])).toEqual(['Connections']);
      expect(labels(['egress:manage'])).toEqual(['Egress Policies']);
    });

    it('lists global vendor and default egress for platform admins only', () => {
      expect(labels([], false)).toEqual([]);
      expect(labels([], true)).toEqual(['Global Vendors', 'Default Egress']);
    });

    it('links into /o/{org}/integrate', () => {
      const items = visibleNav(scope, {
        isPlatformAdmin: false,
        permissions: new Set(['model:read']),
      });
      expect(items.find((i) => i.label === 'Models')?.href).toBe('/o/acme/integrate/models');
    });
  });

  describe('Connections space (CM-6)', () => {
    const integrateItems = (perms: string[]) =>
      visibleNav(parseScope('/o/acme'), {
        isPlatformAdmin: false,
        permissions: new Set(perms),
      }).filter((i) => i.group === 'Integrate');

    it.each(['connection:read', 'model:read', 'delivery:target:read'])(
      'shows Connections first in Integrate with %s alone',
      (perm) => {
        const items = integrateItems([perm]);
        expect(items[0]).toEqual({
          id: 'integrate-connections',
          label: 'Connections',
          group: 'Integrate',
          href: '/o/acme/connections',
        });
        expect(items.filter((i) => i.label === 'Connections')).toHaveLength(1);
      },
    );

    it('is the first Integrate item with every Integrate permission', () => {
      const items = integrateItems([
        'vendor:read',
        'model:read',
        'connection:read',
        'egress:manage',
      ]);
      expect(items[0]?.id).toBe('integrate-connections');
    });

    it('parses /o/{org}/connections as an organization-level area', () => {
      expect(parseScope('/o/acme/connections')).toEqual({ org: 'acme', area: 'other' });
      expect(parseScope('/o/acme/connections/c1/tests')).toEqual({ org: 'acme', area: 'other' });
    });

    it('puts connection detail pages under the Integrate group', () => {
      const items = integrateItems(['connection:read', 'model:read']);
      expect(currentNavGroup(items, '/o/acme/connections/c1')).toBe('Integrate');
    });

    it('builds the space and connection hrefs', () => {
      expect(connectionsHref('acme')).toBe('/o/acme/connections');
      expect(connectionsHref('acme', 'eng', 'api')).toBe('/o/acme/connections?ws=eng&proj=api');
      expect(connectionHref('acme', 'c1')).toBe('/o/acme/connections/c1');
      expect(connectionHref('acme', 'c 1', 'tests')).toBe('/o/acme/connections/c%201/tests');
    });
  });
});

describe('feature gating', () => {
  const scope = parseScope('/o/acme');
  const perms = new Set(['member:read', 'prompt:read', 'run:read', 'model:read']);
  const groups = (features?: string[]) => [
    ...new Set(
      rawVisibleNav(scope, { isPlatformAdmin: true, permissions: perms, features }).map(
        (i) => i.group,
      ),
    ),
  ];

  it('shows only groups whose feature is enabled', () => {
    expect(groups(['tenancy', 'build'])).toEqual(['Home', 'Administer', 'Build']);
    expect(groups(['tenancy', 'operate', 'security'])).toEqual([
      'Home',
      'Administer',
      'Security',
      'Operate',
    ]);
  });

  it('still requires the permission when the feature is enabled', () => {
    const items = rawVisibleNav(scope, {
      isPlatformAdmin: false,
      permissions: new Set<string>(),
      features: ALL_FEATURES,
    });
    expect(items.some((i) => i.group === 'Operate' || i.group === 'Build')).toBe(false);
  });

  it('falls back to tenancy only when features are absent', () => {
    expect(groups(undefined)).toEqual(['Home', 'Administer']);
  });

  it('hides platform integrate pages when integrate is off', () => {
    const ids = rawVisibleNav(parseScope('/platform/status'), {
      isPlatformAdmin: true,
      features: ['tenancy'],
    }).map((i) => i.id);
    expect(ids).not.toContain('platform-vendors');
    expect(ids).toContain('platform-idps');
  });
});

describe('currentNavGroup', () => {
  const items = [
    { id: 'dashboard', label: 'Dashboard', group: 'Home', href: '/o/acme' },
    { id: 'r', label: 'Runs', group: 'Operate', href: '/o/acme/runs' },
    { id: 'n', label: 'Runs next', group: 'Other', href: '/o/acme/runs-next' },
  ];

  it('picks the most specific item, not the dashboard above everything', () => {
    expect(currentNavGroup(items, '/o/acme')).toBe('Home');
    expect(currentNavGroup(items, '/o/acme/runs/r1')).toBe('Operate');
    expect(currentNavGroup(items, '/o/acme/runs-next')).toBe('Other');
    expect(currentNavGroup(items, '/o/other')).toBeUndefined();
    expect(currentNavGroup(items, '/o/acme/not-in-nav')).toBeUndefined();
  });
});
