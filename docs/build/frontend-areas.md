# Front-end areas

Map of every file tracked under SETH `frontend/` at commit `3ba0e06` (389 files) to exactly one area. `shell` is built in `frontend/` now; the other areas are ported one per backend module after that module's API merges. `installation` is ported without branding (B-1101).

## How areas were assigned

1. Route pages (`src/app/**`) take the area of the backend module they call (see the section headings).
2. Components, hooks and libs took the area of their importers: a file used by one area goes with it; a file shared by several, and not generic, goes to the lowest module in the layer order core, adapters, audit|authz|observability|ledger, secrets, identity, tenancy, vendors, connections, admin|models_admin, governance, build, ontology, runtime (ties inside one layer broken alphabetically). Layouts take the lowest area of the pages beneath them.
3. Generic plumbing (root layout, theme CSS, API client and errors, session and scope hooks, navigation, generic UI primitives, test setup, config, proxy) is `shell`.
4. A test file goes with the file it tests (same stem; else its non-shell imports). Shared test fixtures go with the lowest importer.
5. Hand-chosen exceptions and the reason are in the Notes column. `UNSURE` marks a judgement call.

Status column: `ported` = in `frontend/` verbatim; `ported, modified` = in `frontend/` with the change stated; `not ported` = deliberately left out; `later` = arrives with its area.

## File count per area

| Area | Files | of which tests |
|---|---:|---:|
| shell | 67 | 16 |
| audit | 9 | 2 |
| authz | 19 | 4 |
| observability | 6 | 2 |
| ledger | 18 | 3 |
| secrets | 3 | 1 |
| identity | 23 | 6 |
| tenancy | 13 | 2 |
| installation | 8 | 3 |
| vendors | 14 | 2 |
| connections | 23 | 5 |
| models_admin | 12 | 3 |
| admin | 0 | 0 |
| governance | 31 | 7 |
| build | 83 | 22 |
| ontology | 28 | 6 |
| runtime | 32 | 7 |
| **total** | **389** | **91** |

The `shell` count includes 12 top-level config and tooling files. No SETH front-end file belongs to `admin` (its pages are the `integrate/models` and `integrate/providers` screens, assigned to `models_admin`) or to `core`/`adapters`.

New in `frontend/` (not in SETH): `src/lib/product.ts`, `src/app/page.tsx`, `src/app/page.test.tsx`; the Deliver screens (SP2-8, SP2-9): `src/app/(app)/o/[org]/deliver/**`, `src/components/deliver/**`, `src/lib/api/delivery-*.ts`, `src/lib/delivery-hooks.ts`, `src/lib/use-now.ts`, `src/test/delivery.ts`; the solutions, releases and evaluations screens (SP3-7): `src/app/(app)/o/[org]/solutions/**`, `src/app/(app)/o/[org]/releases/**`, `src/app/(app)/o/[org]/evaluations/**`, `src/components/releases/**`, `src/lib/api/packaging-*.ts`, `src/lib/api/evaluation-*.ts`, `src/lib/release-hooks.ts`, `src/lib/release-utils.ts`, `src/test/releases.ts`.

## shell

| File | Status | Notes |
|---|---|---|
| `.gitignore` | ported |  |
| `.prettierignore` | ported |  |
| `.prettierrc.json` | ported |  |
| `AGENTS.md` | ported |  |
| `README.md` | ported, modified | rewritten for the shell (SETH README documents SETH phases and contracts) |
| `eslint.config.js` | ported |  |
| `next.config.ts` | ported |  |
| `package.json` | ported |  |
| `pnpm-lock.yaml` | ported |  |
| `postcss.config.mjs` | ported |  |
| `src/app/(app)/error.tsx` | ported |  |
| `src/app/(app)/layout.tsx` | ported |  |
| `src/app/(app)/page.tsx` | not ported | `HomeRedirect` landing route; replaced by static `src/app/page.tsx` so the shell renders without a backend. Restore when identity/tenancy land. |
| `src/app/globals.css` | ported |  |
| `src/app/icon.svg` | ported |  |
| `src/app/layout.tsx` | ported, modified | product name read from `lib/product.ts` instead of `lib/branding.ts` |
| `src/app/not-found.tsx` | ported |  |
| `src/components/AppBrand.tsx` | ported, modified | lockup only; runtime branding (installation) not ported |
| `src/components/AppShell.tsx` | ported |  |
| `src/components/ConfirmDialog.tsx` | ported |  |
| `src/components/ErrorMessage.tsx` | ported |  |
| `src/components/FeatureGate.test.tsx` | ported |  |
| `src/components/FeatureGate.tsx` | ported | gates every area layout; shell. |
| `src/components/HomeRedirect.test.tsx` | ported |  |
| `src/components/HomeRedirect.tsx` | ported |  |
| `src/components/NavGroupIcon.tsx` | ported |  |
| `src/components/NoAccess.tsx` | ported |  |
| `src/components/Providers.tsx` | ported, modified | `BrandingApplier` removed (installation) |
| `src/components/QueryError.tsx` | ported |  |
| `src/components/ScopeSelectors.test.tsx` | ported |  |
| `src/components/ScopeSelectors.tsx` | ported | org/workspace/project switcher in the header; shell (uses the scope hooks in `lib/hooks.ts`). |
| `src/components/Sidebar.build.test.tsx` | ported |  |
| `src/components/Sidebar.collapse.test.tsx` | ported |  |
| `src/components/Sidebar.integrate.test.tsx` | ported |  |
| `src/components/Sidebar.test.tsx` | ported |  |
| `src/components/Sidebar.tsx` | ported |  |
| `src/components/SidebarFrame.test.tsx` | ported |  |
| `src/components/SidebarFrame.tsx` | ported |  |
| `src/components/UserMenu.tsx` | ported |  |
| `src/components/admin/ui.tsx` | ported, modified | generic form/page primitives used by 80+ files; shell. `TextField` takes `readOnly` (SP3-8) |
| `src/components/command/BrandLockup.tsx` | ported | logo lockup used by `AppBrand` and the sign-in page; shell. |
| `src/components/governance/GovernGate.test.tsx` | ported | tests `FeatureGate` and `Sidebar` only; shell. |
| `src/components/security/useOrg.ts` | ported | generic `/o/[org]` route param hook; shell. |
| `src/lib/api-origin.test.ts` | ported |  |
| `src/lib/api-origin.ts` | ported |  |
| `src/lib/api/client.test.ts` | ported |  |
| `src/lib/api/client.ts` | ported |  |
| `src/lib/api/endpoints.ts` | ported, modified | session/scope plumbing plus tenancy create/archive workspace and project; the security endpoint group is spread in; other per-area groups are spread back in as areas land; fe-areas-5 added `platformStatus` and spread the operate and pipeline endpoint groups |
| `src/lib/api/errors.ts` | ported |  |
| `src/lib/api/types.ts` | ported, modified | verbatim plus `MyPermissions` and `PermissionDef` copied from `security-types.ts`; `PlatformStatus` added (fe-areas-5) |
| `src/lib/hooks.ts` | ported | session, scope and permission hooks the shell chrome needs. |
| `src/lib/markdown.test.tsx` | ported |  |
| `src/lib/markdown.tsx` | ported | generic safe markdown renderer; shell. |
| `src/lib/nav.test.ts` | ported |  |
| `src/lib/nav.ts` | ported | data-driven navigation registry; keeps links to every area route (they resolve as areas land). |
| `src/lib/scope.ts` | ported | route helpers for every area (`securityHref`, `buildHref`, ...). |
| `src/lib/sidebar-layout.ts` | ported |  |
| `src/lib/validation.test.ts` | ported |  |
| `src/lib/validation.ts` | ported |  |
| `src/next-config.test.ts` | ported |  |
| `src/proxy.test.ts` | ported |  |
| `src/proxy.ts` | ported |  |
| `src/test/security.tsx` | ported, modified | `PermissionDef` import points at `lib/api/types` |
| `src/test/setup.ts` | ported |  |
| `src/test/utils.tsx` | ported |  |
| `tsconfig.json` | ported |  |
| `vitest.config.ts` | ported |  |


Files not in SETH: `src/lib/contrast.ts` (shell, new in fe-areas-5): `contrastRatio` extracted from SETH `branding.ts` for `lib/command.test.ts`.

## audit

| File | Status | Notes |
|---|---|---|
| `src/app/(app)/o/[org]/govern/audit/page.tsx` | ported |  |
| `src/app/(app)/o/[org]/govern/layout.tsx` | ported |  |
| `src/app/(app)/o/[org]/security/audit/page.tsx` | ported |  |
| `src/app/(app)/o/[org]/security/layout.tsx` | ported |  |
| `src/components/security/AuditExplorer.test.tsx` | ported |  |
| `src/components/security/AuditView.test.tsx` | ported |  |
| `src/components/security/AuditView.tsx` | ported |  |
| `src/lib/api/security-endpoints.ts` | ported, modified | `redeemSetup`, `myPermissions` (already in the shell endpoints), `createGroup`, `setGroupMembers` removed: the group create and member routes exist only under `/api/v1/platform/groups` (admin), not `/orgs/{org}/groups`; spread into `endpoints.ts`; `listPlatformGroups` and `createPlatformGroup` added (SP3-8) |
| `src/lib/api/security-types.ts` | ported, modified | `MyPermissions`, `PermissionDef`, `SetupRedeemResult` re-exported from `./types` (shell-owned copies) instead of redefined; `Idp`/`IdpInput` gain `profile` and `entra_tenant_id`, `PlatformGroup` and `PlatformGroupCreate` added (SP3-8) |

## authz

| File | Status | Notes |
|---|---|---|
| `src/app/(app)/o/[org]/security/groups/[id]/page.tsx` | later | blocked: `POST /api/v1/orgs/{org}/groups` and `PUT /api/v1/orgs/{org}/groups/{id}/members` do not exist (groups are created and edited only under `/api/v1/platform/groups`, admin); owner: admin / `app` port |
| `src/app/(app)/o/[org]/security/groups/page.tsx` | later | blocked: `POST /api/v1/orgs/{org}/groups` and `PUT /api/v1/orgs/{org}/groups/{id}/members` do not exist (groups are created and edited only under `/api/v1/platform/groups`, admin); owner: admin / `app` port |
| `src/app/(app)/o/[org]/security/page.tsx` | ported |  |
| `src/app/(app)/o/[org]/security/permissions/page.tsx` | ported |  |
| `src/app/(app)/o/[org]/security/roles/page.tsx` | ported |  |
| `src/components/CreateScopeForm.test.tsx` | ported |  |
| `src/components/CreateScopeForm.tsx` | ported |  |
| `src/components/admin/PermissionChecklist.tsx` | ported |  |
| `src/components/admin/RoleBindings.test.tsx` | ported |  |
| `src/components/admin/RoleBindings.tsx` | ported |  |
| `src/components/admin/ScopePicker.tsx` | ported |  |
| `src/components/security/GroupDetailView.test.tsx` | later | blocked: `POST /api/v1/orgs/{org}/groups` and `PUT /api/v1/orgs/{org}/groups/{id}/members` do not exist (groups are created and edited only under `/api/v1/platform/groups`, admin); owner: admin / `app` port |
| `src/components/security/GroupDetailView.tsx` | later | blocked: `POST /api/v1/orgs/{org}/groups` and `PUT /api/v1/orgs/{org}/groups/{id}/members` do not exist (groups are created and edited only under `/api/v1/platform/groups`, admin); owner: admin / `app` port |
| `src/components/security/GroupsView.tsx` | later | blocked: `POST /api/v1/orgs/{org}/groups` and `PUT /api/v1/orgs/{org}/groups/{id}/members` do not exist (groups are created and edited only under `/api/v1/platform/groups`, admin); owner: admin / `app` port |
| `src/components/security/PermissionsView.tsx` | ported |  |
| `src/components/security/RolesView.test.tsx` | ported |  |
| `src/components/security/RolesView.tsx` | ported |  |
| `src/components/security/SecurityHome.tsx` | ported |  |
| `src/lib/security-hooks.ts` | ported, modified | `usePlatformGroups` added (SP3-8) |

## observability

| File | Status | Notes |
|---|---|---|
| `src/app/(app)/o/[org]/govern/performance/page.tsx` | ported | |
| `src/app/(app)/platform/status/page.tsx` | ported |  |
| `src/components/PlatformStatusView.test.tsx` | ported, modified | verbatim except the `profile: null` line removed from the `installation` fixture (the unified `PlatformInstallation` drops `profile`, B-1101) |
| `src/components/PlatformStatusView.tsx` | ported |  |
| `src/components/governance/TelemetryPanel.test.tsx` | ported |  |
| `src/components/governance/TelemetryPanel.tsx` | ported |  |

## ledger

| File | Status | Notes |
|---|---|---|
| `src/app/(app)/o/[org]/govern/costs/page.tsx` | ported | |
| `src/app/(app)/o/[org]/govern/models/page.tsx` | ported | |
| `src/app/(app)/o/[org]/govern/runs/[id]/cost/page.tsx` | ported | |
| `src/app/(app)/o/[org]/govern/usage/page.tsx` | ported | |
| `src/components/governance/RunCostView.test.tsx` | ported | |
| `src/components/governance/RunCostView.tsx` | ported | |
| `src/components/governance/UsageFilterBar.tsx` | ported | |
| `src/components/governance/UsageViews.test.tsx` | ported | |
| `src/components/governance/UsageViews.tsx` | ported | |
| `src/components/governance/charts.tsx` | ported | |
| `src/components/governance/cost.tsx` | ported | |
| `src/components/integrate/SlugScopeSelect.tsx` | ported | |
| `src/lib/api/governance-endpoints.ts` | ported | |
| `src/lib/api/governance-types.ts` | ported | |
| `src/lib/governance-hooks.ts` | ported | |
| `src/lib/governance.test.ts` | ported | |
| `src/lib/governance.ts` | ported | |
| `src/test/governance.ts` | ported | |

## secrets

| File | Status | Notes |
|---|---|---|
| `src/app/(app)/o/[org]/security/secrets/page.tsx` | ported |  |
| `src/components/security/SecretsView.test.tsx` | ported |  |
| `src/components/security/SecretsView.tsx` | ported |  |

## identity

| File | Status | Notes |
|---|---|---|
| `src/app/(app)/o/[org]/security/api-keys/page.tsx` | ported |  |
| `src/app/(app)/o/[org]/security/idps/page.tsx` | ported |  |
| `src/app/(app)/o/[org]/security/service-accounts/[id]/page.tsx` | ported |  |
| `src/app/(app)/o/[org]/security/service-accounts/page.tsx` | ported |  |
| `src/app/(app)/o/[org]/security/users/page.tsx` | ported |  |
| `src/app/(app)/platform/idps/page.tsx` | ported |  |
| `src/app/login/page.tsx` | ported |  |
| `src/app/setup/page.tsx` | ported |  |
| `src/components/LoginForm.oidc.test.tsx` | ported |  |
| `src/components/LoginForm.test.tsx` | ported | one test added: no branding call, fixed heading |
| `src/components/LoginForm.tsx` | ported, modified | `useBranding` removed: heading uses `DEFAULT_PRODUCT_NAME`; no logo, login message or support link; no call to `/api/installation/branding` (B-1101) |
| `src/components/SetupForm.test.tsx` | ported |  |
| `src/components/SetupForm.tsx` | ported | UNSURE: first-admin setup redeem; identity (calls `redeemSetup`). |
| `src/components/admin/SecretOnce.tsx` | ported |  |
| `src/components/security/ApiKeysPanel.test.tsx` | ported |  |
| `src/components/security/ApiKeysPanel.tsx` | ported |  |
| `src/components/security/ApiKeysView.tsx` | ported |  |
| `src/components/security/IdpsView.test.tsx` | ported, modified | Entra profile, tenant id and Entra group mapping (SP3-8) |
| `src/components/security/IdpsView.tsx` | ported, modified | Entra profile, tenant id and Entra group mapping (SP3-8) |
| `src/components/security/ServiceAccountsView.tsx` | ported |  |
| `src/components/security/UsersView.test.tsx` | ported |  |
| `src/components/security/UsersView.tsx` | ported |  |
| `src/lib/oidc.ts` | ported |  |

## tenancy

| File | Status | Notes |
|---|---|---|
| `src/app/(app)/o/[org]/page.tsx` | ported |  |
| `src/app/(app)/o/[org]/w/[ws]/p/[proj]/page.tsx` | ported |  |
| `src/app/(app)/o/[org]/w/[ws]/page.tsx` | ported |  |
| `src/app/(app)/o/[org]/w/[ws]/projects/page.tsx` | ported |  |
| `src/app/(app)/o/[org]/workspaces/page.tsx` | ported |  |
| `src/app/(app)/platform/organizations/page.tsx` | ported |  |
| `src/components/DashboardView.test.tsx` | ported |  |
| `src/components/DashboardView.tsx` | ported |  |
| `src/components/OrganizationsView.tsx` | ported |  |
| `src/components/ProjectsView.tsx` | ported |  |
| `src/components/ScopeListView.test.tsx` | ported |  |
| `src/components/ScopeListView.tsx` | ported |  |
| `src/components/WorkspacesView.tsx` | ported |  |

## installation

| File | Status | Notes |
|---|---|---|
| `src/app/(app)/platform/settings/page.tsx` | ported |  |
| `src/components/BrandingApplier.test.tsx` | not ported | runtime branding: `/api/installation/branding` and `/branding/logo` dropped by the unified backend (B-1101); the login page and `AppBrand` use `lib/product.ts` |
| `src/components/BrandingApplier.tsx` | not ported | runtime branding: `/api/installation/branding` and `/branding/logo` dropped by the unified backend (B-1101); the login page and `AppBrand` use `lib/product.ts` |
| `src/components/PlatformSettingsView.test.tsx` | ported, modified | assertions on the dropped fields replaced: note, profile name and "No profile applied", retention enforcement, section status labels and the security profile wording now assert absence; the other tests are unchanged |
| `src/components/PlatformSettingsView.tsx` | ported, modified | sections and text driven by `profile`, `retention`, `governance_defaults`, `section_status` and the "managed by the installation profile" note removed (B-1101, B-1102, carry_forward); `isApplied`, `RECORDED_ONLY_TEXT`, `retentionEnforcementText` removed |
| `src/lib/branding-hooks.ts` | not ported | runtime branding: `/api/installation/branding` and `/branding/logo` dropped by the unified backend (B-1101); the login page and `AppBrand` use `lib/product.ts` |
| `src/lib/branding.test.ts` | not ported | runtime branding: `/api/installation/branding` and `/branding/logo` dropped by the unified backend (B-1101); the login page and `AppBrand` use `lib/product.ts` |
| `src/lib/branding.ts` | not ported | runtime branding: `/api/installation/branding` and `/branding/logo` dropped by the unified backend (B-1101); the login page and `AppBrand` use `lib/product.ts` |

## vendors

| File | Status | Notes |
|---|---|---|
| `src/app/(app)/o/[org]/integrate/layout.tsx` | ported | |
| `src/app/(app)/o/[org]/integrate/vendors/[id]/page.tsx` | ported | |
| `src/app/(app)/o/[org]/integrate/vendors/page.tsx` | ported | |
| `src/app/(app)/platform/vendors/layout.tsx` | ported | |
| `src/app/(app)/platform/vendors/page.tsx` | ported | |
| `src/components/integrate/Maturity.tsx` | ported | |
| `src/components/integrate/PlatformVendorsView.test.tsx` | ported | |
| `src/components/integrate/VendorDetailView.tsx` | ported | |
| `src/components/integrate/VendorsView.test.tsx` | ported | |
| `src/components/integrate/VendorsView.tsx` | ported | |
| `src/lib/api/integrate-endpoints.ts` | ported | |
| `src/lib/api/integrate-types.ts` | ported | |
| `src/lib/integrate-hooks.ts` | ported | |
| `src/test/integrate.ts` | ported | |

## connections

| File | Status | Notes |
|---|---|---|
| `src/app/(app)/o/[org]/integrate/adapters/page.tsx` | ported | |
| `src/app/(app)/o/[org]/integrate/connections/[id]/page.tsx` | ported, modified | redirects to `/o/[org]/connections...` (CM-6, #164) |
| `src/app/(app)/o/[org]/integrate/connections/[id]/tests/page.tsx` | ported, modified | redirects to `/o/[org]/connections...` (CM-6, #164) |
| `src/app/(app)/o/[org]/integrate/connections/page.tsx` | ported, modified | redirects to `/o/[org]/connections...` (CM-6, #164) |
| `src/app/(app)/o/[org]/integrate/egress/page.tsx` | ported | |
| `src/app/(app)/platform/egress/layout.tsx` | ported | |
| `src/app/(app)/platform/egress/page.tsx` | ported | |
| `src/components/integrate/AdaptersView.test.tsx` | ported | |
| `src/components/integrate/AdaptersView.tsx` | ported | |
| `src/components/integrate/ConnectionConsole.tsx` | ported | |
| `src/components/integrate/ConnectionDetailView.test.tsx` | ported | |
| `src/components/integrate/ConnectionDetailView.tsx` | ported | |
| `src/components/integrate/ConnectionTests.tsx` | ported | |
| `src/components/integrate/ConnectionWizard.test.tsx` | ported | |
| `src/components/integrate/ConnectionWizard.tsx` | ported | |
| `src/components/integrate/ConnectionsView.tsx` | ported | |
| `src/components/integrate/EgressView.tsx` | ported | |
| `src/components/integrate/SchemaForm.tsx` | ported | |
| `src/lib/discovery.test.ts` | ported | |
| `src/lib/discovery.ts` | ported | |
| `src/lib/maturity.ts` | ported | |
| `src/lib/schema-form.test.ts` | ported | |
| `src/lib/schema-form.ts` | ported | |

## models_admin

| File | Status | Notes |
|---|---|---|
| `src/app/(app)/o/[org]/integrate/models/[id]/page.tsx` | ported | |
| `src/app/(app)/o/[org]/integrate/models/page.tsx` | ported | |
| `src/app/(app)/o/[org]/integrate/providers/[id]/page.tsx` | ported | |
| `src/app/(app)/o/[org]/integrate/providers/page.tsx` | ported | |
| `src/components/integrate/ModelDetailView.test.tsx` | ported | |
| `src/components/integrate/ModelDetailView.tsx` | ported | |
| `src/components/integrate/ModelPolicyPanels.tsx` | ported | |
| `src/components/integrate/ModelsView.test.tsx` | ported | |
| `src/components/integrate/ModelsView.tsx` | ported | |
| `src/components/integrate/ProviderDetailView.tsx` | ported | |
| `src/components/integrate/ProvidersView.test.tsx` | ported | |
| `src/components/integrate/ProvidersView.tsx` | ported | |

## admin

| File | Status | Notes |
|---|---|---|

## governance

| File | Status | Notes |
|---|---|---|
| `src/app/(app)/o/[org]/approvals/layout.tsx` | ported |  |
| `src/app/(app)/o/[org]/approvals/page.tsx` | ported |  |
| `src/app/(app)/o/[org]/govern/budgets/page.tsx` | ported | |
| `src/app/(app)/o/[org]/govern/decisions/page.tsx` | ported | |
| `src/app/(app)/o/[org]/govern/overview/page.tsx` | ported | |
| `src/app/(app)/o/[org]/govern/policies/page.tsx` | ported | |
| `src/app/(app)/o/[org]/govern/pricing/page.tsx` | ported | |
| `src/app/(app)/o/[org]/govern/retention/page.tsx` | ported | |
| `src/app/(app)/platform/rate-cards/layout.tsx` | ported | |
| `src/app/(app)/platform/rate-cards/page.tsx` | ported | |
| `src/components/governance/BudgetsView.test.tsx` | ported | |
| `src/components/governance/BudgetsView.tsx` | ported | |
| `src/components/governance/GovScopePicker.tsx` | ported | |
| `src/components/governance/GovernanceViews.test.tsx` | ported | |
| `src/components/governance/GovernanceViews.tsx` | ported | |
| `src/components/governance/PoliciesView.test.tsx` | ported | |
| `src/components/governance/PoliciesView.tsx` | ported | |
| `src/components/governance/PricingView.test.tsx` | ported | |
| `src/components/governance/PricingView.tsx` | ported | |
| `src/components/operate/ApprovalsView.outcome.test.tsx` | ported |  |
| `src/components/operate/ApprovalsView.test.tsx` | ported |  |
| `src/components/operate/ApprovalsView.tsx` | ported |  |
| `src/lib/api/build-endpoints.ts` | ported, modified | full SETH file replaces the governance slice (B-2210) |
| `src/lib/api/build-types.ts` | ported, modified | full SETH file replaces the governance slice (B-2210) |
| `src/lib/api/operate-endpoints.ts` | ported |  |
| `src/lib/api/operate-types.ts` | ported | pulled forward by the build unit (types and test stub only: the orchestration spec types and `stubOperate`); the runtime unit adds the rest of its slice |
| `src/lib/build-hooks.ts` | ported, modified | full SETH file replaces the governance slice (B-2210) |
| `src/lib/operate-hooks.ts` | ported |  |
| `src/lib/run-utils.test.ts` | ported |  |
| `src/lib/run-utils.ts` | ported |  |
| `src/test/operate.ts` | ported | pulled forward by the build unit (types and test stub only: the orchestration spec types and `stubOperate`); the runtime unit adds the rest of its slice |

## build

| File | Status | Notes |
|---|---|---|
| `src/app/(app)/o/[org]/build/agents/[id]/page.tsx` | ported | |
| `src/app/(app)/o/[org]/build/agents/page.tsx` | ported | |
| `src/app/(app)/o/[org]/build/layout.tsx` | ported | |
| `src/app/(app)/o/[org]/build/orchestrations/[id]/page.tsx` | ported | |
| `src/app/(app)/o/[org]/build/orchestrations/page.tsx` | ported | |
| `src/app/(app)/o/[org]/build/pipelines/[id]/page.tsx` | ported |  |
| `src/app/(app)/o/[org]/build/pipelines/layout.tsx` | ported |  |
| `src/app/(app)/o/[org]/build/pipelines/page.tsx` | ported |  |
| `src/app/(app)/o/[org]/build/prompts/[id]/page.tsx` | ported | |
| `src/app/(app)/o/[org]/build/prompts/page.tsx` | ported | |
| `src/app/(app)/o/[org]/build/publish-requests/page.tsx` | ported | |
| `src/app/(app)/o/[org]/build/skills/[id]/page.tsx` | ported | |
| `src/app/(app)/o/[org]/build/skills/page.tsx` | ported | |
| `src/app/(app)/o/[org]/build/tools/[id]/page.tsx` | ported | |
| `src/app/(app)/o/[org]/build/tools/page.tsx` | ported | |
| `src/components/build/AgentEditor.test.tsx` | ported | |
| `src/components/build/AgentEditor.tsx` | ported | |
| `src/components/build/AgentTestPanel.tsx` | ported | |
| `src/components/build/ArtifactDetailView.tsx` | ported | pipeline tabs and the ontology Actions tab restored (fe-areas-5) |
| `src/components/build/ArtifactListView.test.tsx` | ported | |
| `src/components/build/ArtifactListView.tsx` | ported | |
| `src/components/build/DraftEditor.tabs.test.tsx` | ported | |
| `src/components/build/DraftEditor.tsx` | ported | |
| `src/components/build/OrchestrationCanvas.tsx` | ported | |
| `src/components/build/OrchestrationEditor.test.tsx` | ported | |
| `src/components/build/OrchestrationEditor.tsx` | ported | |
| `src/components/build/PromptBuilder.test.tsx` | ported | |
| `src/components/build/PromptEditor.tsx` | ported | |
| `src/components/build/PromptPanels.tsx` | ported | |
| `src/components/build/PublishRequestsView.test.tsx` | ported | |
| `src/components/build/PublishRequestsView.tsx` | ported | |
| `src/components/build/SectionTabs.test.tsx` | ported | |
| `src/components/build/SectionTabs.tsx` | ported | |
| `src/components/build/SkillEditor.test.tsx` | ported | |
| `src/components/build/SkillEditor.tsx` | ported | |
| `src/components/build/ToolEditor.test.tsx` | ported | |
| `src/components/build/ToolEditor.tsx` | ported | |
| `src/components/build/ToolTestPanel.tsx` | ported | |
| `src/components/build/VersionsPanel.tsx` | ported | |
| `src/components/build/pickers.tsx` | ported | |
| `src/components/build/shared.tsx` | ported | |
| `src/components/operate/CancelRunButton.tsx` | ported |  |
| `src/components/operate/RunAgentLink.test.tsx` | ported | hidden until the backend advertises `operate` |
| `src/components/operate/RunAgentLink.tsx` | ported | hidden until the backend advertises `operate` |
| `src/components/operate/parts.tsx` | ported |  |
| `src/components/pipelines/LazyPipelineCanvas.tsx` | ported |  |
| `src/components/pipelines/NodeConfigPanel.tsx` | ported |  |
| `src/components/pipelines/NodePalette.tsx` | ported |  |
| `src/components/pipelines/NodeStatusList.outcome.test.tsx` | ported |  |
| `src/components/pipelines/NodeStatusList.tsx` | ported |  |
| `src/components/pipelines/PipelineCanvas.tsx` | ported |  |
| `src/components/pipelines/PipelineEditor.outcome.test.tsx` | ported |  |
| `src/components/pipelines/PipelineEditor.test.tsx` | ported |  |
| `src/components/pipelines/PipelineEditor.tsx` | ported |  |
| `src/components/pipelines/PipelineHistory.tsx` | ported |  |
| `src/components/pipelines/PipelineRun.test.tsx` | ported |  |
| `src/components/pipelines/PipelineRunContext.tsx` | ported |  |
| `src/components/pipelines/PipelineRunPanels.tsx` | ported |  |
| `src/components/pipelines/PipelineScreens.test.tsx` | ported |  |
| `src/components/pipelines/PipelineValidationPanel.tsx` | ported |  |
| `src/components/pipelines/SchedulesPanel.tsx` | ported |  |
| `src/components/pipelines/TransformEditor.tsx` | ported |  |
| `src/lib/agent-spec.test.ts` | ported | |
| `src/lib/agent-spec.ts` | ported | |
| `src/lib/api/pipeline-endpoints.ts` | ported |  |
| `src/lib/api/pipeline-types.ts` | ported | |
| `src/lib/expr.test.ts` | ported | |
| `src/lib/expr.ts` | ported | |
| `src/lib/failure.ts` | ported | |
| `src/lib/graph.test.ts` | ported | |
| `src/lib/graph.ts` | ported | |
| `src/lib/pipeline-config.ts` | ported | |
| `src/lib/pipeline-hooks.ts` | ported |  |
| `src/lib/pipeline-params.test.ts` | ported | |
| `src/lib/pipeline-params.ts` | ported | |
| `src/lib/pipeline-spec.test.ts` | ported | |
| `src/lib/pipeline-spec.ts` | ported | |
| `src/lib/prompt-utils.test.ts` | ported | |
| `src/lib/prompt-utils.ts` | ported | |
| `src/lib/tool-rules.test.ts` | ported | |
| `src/lib/tool-rules.ts` | ported | |
| `src/test/build.ts` | ported | |
| `src/test/pipelines.ts` | ported | |

## ontology

| File | Status | Notes |
|---|---|---|
| `src/app/(app)/o/[org]/build/ontologies/[id]/lineage/page.tsx` | ported | |
| `src/app/(app)/o/[org]/build/ontologies/[id]/page.tsx` | ported | |
| `src/app/(app)/o/[org]/build/ontologies/layout.tsx` | ported | |
| `src/app/(app)/o/[org]/build/ontologies/page.tsx` | ported | |
| `src/components/ontology/ActionsEditor.test.tsx` | ported | |
| `src/components/ontology/ActionsEditor.tsx` | ported | |
| `src/components/ontology/ActionsPanel.tsx` | ported |  |
| `src/components/ontology/ExplorerPanel.tsx` | ported | |
| `src/components/ontology/LazyOntologyCanvas.tsx` | ported | |
| `src/components/ontology/LineagePanel.tsx` | ported | |
| `src/components/ontology/MappingPanel.tsx` | ported | |
| `src/components/ontology/ObjectTypeDialog.tsx` | ported | |
| `src/components/ontology/OntologyCanvas.tsx` | ported | |
| `src/components/ontology/OntologyEditor.test.tsx` | ported | |
| `src/components/ontology/OntologyEditor.tsx` | ported | |
| `src/components/ontology/OntologyGate.test.tsx` | ported | |
| `src/components/ontology/OntologyPanels.test.tsx` | ported | pipeline tabs and the ontology Actions tab restored (fe-areas-5) |
| `src/components/ontology/OntologyValidationPanel.tsx` | ported | |
| `src/components/ontology/PoliciesPanel.tsx` | ported | |
| `src/components/ontology/PrincipalAttributesDialog.tsx` | ported | |
| `src/components/ontology/RelationshipDialog.tsx` | ported | |
| `src/lib/api/ontology-endpoints.ts` | ported | |
| `src/lib/api/ontology-types.ts` | ported | |
| `src/lib/ontology-actions.test.ts` | ported | |
| `src/lib/ontology-hooks.ts` | ported | |
| `src/lib/ontology-spec.test.ts` | ported | |
| `src/lib/ontology-spec.ts` | ported | |
| `src/test/ontology.ts` | ported | |

## runtime

| File | Status | Notes |
|---|---|---|
| `src/app/(app)/o/[org]/command/command.css` | ported |  |
| `src/app/(app)/o/[org]/command/layout.tsx` | ported |  |
| `src/app/(app)/o/[org]/command/page.tsx` | ported |  |
| `src/app/(app)/o/[org]/runs/[id]/page.tsx` | ported |  |
| `src/app/(app)/o/[org]/runs/layout.tsx` | ported |  |
| `src/app/(app)/o/[org]/runs/page.tsx` | ported |  |
| `src/app/(app)/o/[org]/w/[ws]/p/[proj]/run/layout.tsx` | ported |  |
| `src/app/(app)/o/[org]/w/[ws]/p/[proj]/run/page.tsx` | ported |  |
| `src/app/(app)/o/[org]/w/[ws]/p/[proj]/runs/layout.tsx` | ported |  |
| `src/app/(app)/o/[org]/w/[ws]/p/[proj]/runs/page.tsx` | ported |  |
| `src/components/command/AdminPanel.tsx` | ported |  |
| `src/components/command/CommandCenter.test.tsx` | ported |  |
| `src/components/command/CommandCenter.tsx` | ported |  |
| `src/components/command/NetworkMatrix.tsx` | ported |  |
| `src/components/command/Reveal.tsx` | ported |  |
| `src/components/command/RunTelemetry.tsx` | ported |  |
| `src/components/command/StartupReadout.tsx` | ported |  |
| `src/components/command/Tabs.tsx` | ported |  |
| `src/components/command/ToneGlyph.tsx` | ported |  |
| `src/components/command/TrussHero.tsx` | ported |  |
| `src/components/operate/RunConsole.test.tsx` | ported |  |
| `src/components/operate/RunConsole.tsx` | ported |  |
| `src/components/operate/RunDetailView.outcome.test.tsx` | ported |  |
| `src/components/operate/RunDetailView.test.tsx` | ported |  |
| `src/components/operate/RunDetailView.tsx` | ported |  |
| `src/components/operate/RunHistoryView.outcome.test.tsx` | ported |  |
| `src/components/operate/RunHistoryView.test.tsx` | ported |  |
| `src/components/operate/RunHistoryView.tsx` | ported |  |
| `src/components/operate/Timeline.tsx` | ported |  |
| `src/components/pipelines/RunNodesTab.tsx` | ported |  |
| `src/lib/command.test.ts` | ported, modified | verbatim except the `contrastRatio` import now comes from `./contrast` (colour maths of `branding.ts`; runtime branding is not ported, B-1101) |
| `src/lib/command.ts` | ported |  |
