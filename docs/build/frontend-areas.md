# Front-end areas

Map of every file tracked under SETH `frontend/` at commit `3ba0e06` (389 files) to exactly one area. `shell` is built in `frontend/` now; the other areas are ported one per backend module after that module's API merges. `installation` is not ported in sub-project 1.

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

New in `frontend/` (not in SETH): `src/lib/product.ts`, `src/app/page.tsx`, `src/app/page.test.tsx`.

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
| `src/components/admin/ui.tsx` | ported | generic form/page primitives used by 80+ files; shell. |
| `src/components/command/BrandLockup.tsx` | ported | logo lockup used by `AppBrand` and the sign-in page; shell. |
| `src/components/governance/GovernGate.test.tsx` | ported | tests `FeatureGate` and `Sidebar` only; shell. |
| `src/components/security/useOrg.ts` | ported | generic `/o/[org]` route param hook; shell. |
| `src/lib/api-origin.test.ts` | ported |  |
| `src/lib/api-origin.ts` | ported |  |
| `src/lib/api/client.test.ts` | ported |  |
| `src/lib/api/client.ts` | ported |  |
| `src/lib/api/endpoints.ts` | ported, modified | trimmed to session/scope plumbing (plus `createOrg`, `dashboard`, which the shell client tests call); per-area endpoint groups are spread back in as areas land |
| `src/lib/api/errors.ts` | ported |  |
| `src/lib/api/types.ts` | ported, modified | verbatim plus `MyPermissions` and `PermissionDef` copied from `security-types.ts` |
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

## audit

| File | Status | Notes |
|---|---|---|
| `src/app/(app)/o/[org]/govern/audit/page.tsx` | later |  |
| `src/app/(app)/o/[org]/govern/layout.tsx` | later |  |
| `src/app/(app)/o/[org]/security/audit/page.tsx` | later |  |
| `src/app/(app)/o/[org]/security/layout.tsx` | later |  |
| `src/components/security/AuditExplorer.test.tsx` | later |  |
| `src/components/security/AuditView.test.tsx` | later |  |
| `src/components/security/AuditView.tsx` | later |  |
| `src/lib/api/security-endpoints.ts` | later | as security-types; `myPermissions` is already in the shell endpoints. |
| `src/lib/api/security-types.ts` | later | spans identity, authz, secrets and audit; audit|authz tie broken alphabetically. Split by area when ported. |

## authz

| File | Status | Notes |
|---|---|---|
| `src/app/(app)/o/[org]/security/groups/[id]/page.tsx` | later |  |
| `src/app/(app)/o/[org]/security/groups/page.tsx` | later |  |
| `src/app/(app)/o/[org]/security/page.tsx` | later | UNSURE: as SecurityHome. |
| `src/app/(app)/o/[org]/security/permissions/page.tsx` | later |  |
| `src/app/(app)/o/[org]/security/roles/page.tsx` | later |  |
| `src/components/CreateScopeForm.test.tsx` | later |  |
| `src/components/CreateScopeForm.tsx` | later | UNSURE: scope (org/workspace/project) creation form, but `GroupsView` (authz) also uses it, so the layer rule gives authz. |
| `src/components/admin/PermissionChecklist.tsx` | later |  |
| `src/components/admin/RoleBindings.test.tsx` | later |  |
| `src/components/admin/RoleBindings.tsx` | later |  |
| `src/components/admin/ScopePicker.tsx` | later |  |
| `src/components/security/GroupDetailView.test.tsx` | later |  |
| `src/components/security/GroupDetailView.tsx` | later |  |
| `src/components/security/GroupsView.tsx` | later |  |
| `src/components/security/PermissionsView.tsx` | later |  |
| `src/components/security/RolesView.test.tsx` | later |  |
| `src/components/security/RolesView.tsx` | later |  |
| `src/components/security/SecurityHome.tsx` | later | UNSURE: security landing page linking to all security areas; placed with groups/roles (authz). |
| `src/lib/security-hooks.ts` | later |  |

## observability

| File | Status | Notes |
|---|---|---|
| `src/app/(app)/o/[org]/govern/performance/page.tsx` | later | UNSURE: execution performance; the view lives in `GovernanceViews`. |
| `src/app/(app)/platform/status/page.tsx` | later | UNSURE: as PlatformStatusView. |
| `src/components/PlatformStatusView.test.tsx` | later |  |
| `src/components/PlatformStatusView.tsx` | later | UNSURE: platform status page; no area named for it, observability chosen. |
| `src/components/governance/TelemetryPanel.test.tsx` | later |  |
| `src/components/governance/TelemetryPanel.tsx` | later | UNSURE: telemetry shown on platform status. |

## ledger

| File | Status | Notes |
|---|---|---|
| `src/app/(app)/o/[org]/govern/costs/page.tsx` | later |  |
| `src/app/(app)/o/[org]/govern/models/page.tsx` | later |  |
| `src/app/(app)/o/[org]/govern/runs/[id]/cost/page.tsx` | later |  |
| `src/app/(app)/o/[org]/govern/usage/page.tsx` | later |  |
| `src/components/governance/RunCostView.test.tsx` | later |  |
| `src/components/governance/RunCostView.tsx` | later |  |
| `src/components/governance/UsageFilterBar.tsx` | later |  |
| `src/components/governance/UsageViews.test.tsx` | later |  |
| `src/components/governance/UsageViews.tsx` | later |  |
| `src/components/governance/charts.tsx` | later |  |
| `src/components/governance/cost.tsx` | later |  |
| `src/components/integrate/SlugScopeSelect.tsx` | later |  |
| `src/lib/api/governance-endpoints.ts` | later | area follows its types file. |
| `src/lib/api/governance-types.ts` | later | shared by usage/cost (ledger) and governance views; the layer rule gives ledger. |
| `src/lib/governance-hooks.ts` | later |  |
| `src/lib/governance.test.ts` | later |  |
| `src/lib/governance.ts` | later |  |
| `src/test/governance.ts` | later |  |

## secrets

| File | Status | Notes |
|---|---|---|
| `src/app/(app)/o/[org]/security/secrets/page.tsx` | later |  |
| `src/components/security/SecretsView.test.tsx` | later |  |
| `src/components/security/SecretsView.tsx` | later |  |

## identity

| File | Status | Notes |
|---|---|---|
| `src/app/(app)/o/[org]/security/api-keys/page.tsx` | later |  |
| `src/app/(app)/o/[org]/security/idps/page.tsx` | later |  |
| `src/app/(app)/o/[org]/security/service-accounts/[id]/page.tsx` | later |  |
| `src/app/(app)/o/[org]/security/service-accounts/page.tsx` | later |  |
| `src/app/(app)/o/[org]/security/users/page.tsx` | later |  |
| `src/app/(app)/platform/idps/page.tsx` | later |  |
| `src/app/login/page.tsx` | later |  |
| `src/app/setup/page.tsx` | later | UNSURE: as SetupForm. |
| `src/components/LoginForm.oidc.test.tsx` | later |  |
| `src/components/LoginForm.test.tsx` | later |  |
| `src/components/LoginForm.tsx` | later |  |
| `src/components/SetupForm.test.tsx` | later |  |
| `src/components/SetupForm.tsx` | later | UNSURE: first-admin setup redeem; identity (calls `redeemSetup`). |
| `src/components/admin/SecretOnce.tsx` | later |  |
| `src/components/security/ApiKeysPanel.test.tsx` | later |  |
| `src/components/security/ApiKeysPanel.tsx` | later |  |
| `src/components/security/ApiKeysView.tsx` | later |  |
| `src/components/security/IdpsView.test.tsx` | later |  |
| `src/components/security/IdpsView.tsx` | later |  |
| `src/components/security/ServiceAccountsView.tsx` | later |  |
| `src/components/security/UsersView.test.tsx` | later |  |
| `src/components/security/UsersView.tsx` | later |  |
| `src/lib/oidc.ts` | later |  |

## tenancy

| File | Status | Notes |
|---|---|---|
| `src/app/(app)/o/[org]/page.tsx` | later |  |
| `src/app/(app)/o/[org]/w/[ws]/p/[proj]/page.tsx` | later |  |
| `src/app/(app)/o/[org]/w/[ws]/page.tsx` | later |  |
| `src/app/(app)/o/[org]/w/[ws]/projects/page.tsx` | later |  |
| `src/app/(app)/o/[org]/workspaces/page.tsx` | later |  |
| `src/app/(app)/platform/organizations/page.tsx` | later |  |
| `src/components/DashboardView.test.tsx` | later |  |
| `src/components/DashboardView.tsx` | later |  |
| `src/components/OrganizationsView.tsx` | later |  |
| `src/components/ProjectsView.tsx` | later |  |
| `src/components/ScopeListView.test.tsx` | later |  |
| `src/components/ScopeListView.tsx` | later |  |
| `src/components/WorkspacesView.tsx` | later |  |

## installation (not ported in sub-project 1)

| File | Status | Notes |
|---|---|---|
| `src/app/(app)/platform/settings/page.tsx` | not ported |  |
| `src/components/BrandingApplier.test.tsx` | not ported |  |
| `src/components/BrandingApplier.tsx` | not ported | as branding.ts. |
| `src/components/PlatformSettingsView.test.tsx` | not ported |  |
| `src/components/PlatformSettingsView.tsx` | not ported | platform settings; installation. |
| `src/lib/branding-hooks.ts` | not ported | as branding.ts. |
| `src/lib/branding.test.ts` | not ported |  |
| `src/lib/branding.ts` | not ported | runtime branding; installation is not ported in sub-project 1. |

## vendors

| File | Status | Notes |
|---|---|---|
| `src/app/(app)/o/[org]/integrate/layout.tsx` | later |  |
| `src/app/(app)/o/[org]/integrate/vendors/[id]/page.tsx` | later |  |
| `src/app/(app)/o/[org]/integrate/vendors/page.tsx` | later |  |
| `src/app/(app)/platform/vendors/layout.tsx` | later |  |
| `src/app/(app)/platform/vendors/page.tsx` | later |  |
| `src/components/integrate/Maturity.tsx` | later |  |
| `src/components/integrate/PlatformVendorsView.test.tsx` | later |  |
| `src/components/integrate/VendorDetailView.tsx` | later |  |
| `src/components/integrate/VendorsView.test.tsx` | later |  |
| `src/components/integrate/VendorsView.tsx` | later |  |
| `src/lib/api/integrate-endpoints.ts` | later | area follows its types file. |
| `src/lib/api/integrate-types.ts` | later | shared by vendors, connections and models; the layer rule gives vendors. |
| `src/lib/integrate-hooks.ts` | later |  |
| `src/test/integrate.ts` | later |  |

## connections

| File | Status | Notes |
|---|---|---|
| `src/app/(app)/o/[org]/integrate/adapters/page.tsx` | later | UNSURE: as AdaptersView. |
| `src/app/(app)/o/[org]/integrate/connections/[id]/page.tsx` | later |  |
| `src/app/(app)/o/[org]/integrate/connections/[id]/tests/page.tsx` | later |  |
| `src/app/(app)/o/[org]/integrate/connections/page.tsx` | later |  |
| `src/app/(app)/o/[org]/integrate/egress/page.tsx` | later |  |
| `src/app/(app)/platform/egress/layout.tsx` | later |  |
| `src/app/(app)/platform/egress/page.tsx` | later |  |
| `src/components/integrate/AdaptersView.test.tsx` | later |  |
| `src/components/integrate/AdaptersView.tsx` | later | UNSURE: adapter catalogue; no area named for it, grouped with connections. |
| `src/components/integrate/ConnectionConsole.tsx` | later |  |
| `src/components/integrate/ConnectionDetailView.test.tsx` | later |  |
| `src/components/integrate/ConnectionDetailView.tsx` | later |  |
| `src/components/integrate/ConnectionTests.tsx` | later |  |
| `src/components/integrate/ConnectionWizard.test.tsx` | later |  |
| `src/components/integrate/ConnectionWizard.tsx` | later |  |
| `src/components/integrate/ConnectionsView.tsx` | later |  |
| `src/components/integrate/EgressView.tsx` | later |  |
| `src/components/integrate/SchemaForm.tsx` | later |  |
| `src/lib/discovery.test.ts` | later |  |
| `src/lib/discovery.ts` | later |  |
| `src/lib/maturity.ts` | later |  |
| `src/lib/schema-form.test.ts` | later |  |
| `src/lib/schema-form.ts` | later |  |

## models_admin

| File | Status | Notes |
|---|---|---|
| `src/app/(app)/o/[org]/integrate/models/[id]/page.tsx` | later |  |
| `src/app/(app)/o/[org]/integrate/models/page.tsx` | later |  |
| `src/app/(app)/o/[org]/integrate/providers/[id]/page.tsx` | later |  |
| `src/app/(app)/o/[org]/integrate/providers/page.tsx` | later |  |
| `src/components/integrate/ModelDetailView.test.tsx` | later |  |
| `src/components/integrate/ModelDetailView.tsx` | later |  |
| `src/components/integrate/ModelPolicyPanels.tsx` | later |  |
| `src/components/integrate/ModelsView.test.tsx` | later |  |
| `src/components/integrate/ModelsView.tsx` | later |  |
| `src/components/integrate/ProviderDetailView.tsx` | later |  |
| `src/components/integrate/ProvidersView.test.tsx` | later |  |
| `src/components/integrate/ProvidersView.tsx` | later |  |

## admin

| File | Status | Notes |
|---|---|---|

## governance

| File | Status | Notes |
|---|---|---|
| `src/app/(app)/o/[org]/approvals/layout.tsx` | later |  |
| `src/app/(app)/o/[org]/approvals/page.tsx` | later |  |
| `src/app/(app)/o/[org]/govern/budgets/page.tsx` | later |  |
| `src/app/(app)/o/[org]/govern/decisions/page.tsx` | later |  |
| `src/app/(app)/o/[org]/govern/overview/page.tsx` | later |  |
| `src/app/(app)/o/[org]/govern/policies/page.tsx` | later |  |
| `src/app/(app)/o/[org]/govern/pricing/page.tsx` | later |  |
| `src/app/(app)/o/[org]/govern/retention/page.tsx` | later |  |
| `src/app/(app)/platform/rate-cards/layout.tsx` | later |  |
| `src/app/(app)/platform/rate-cards/page.tsx` | later |  |
| `src/components/governance/BudgetsView.test.tsx` | later |  |
| `src/components/governance/BudgetsView.tsx` | later |  |
| `src/components/governance/GovScopePicker.tsx` | later |  |
| `src/components/governance/GovernanceViews.test.tsx` | later |  |
| `src/components/governance/GovernanceViews.tsx` | later | UNSURE: holds the overview, decisions, performance (observability?) and retention views; governance by majority. |
| `src/components/governance/PoliciesView.test.tsx` | later |  |
| `src/components/governance/PoliciesView.tsx` | later |  |
| `src/components/governance/PricingView.test.tsx` | later |  |
| `src/components/governance/PricingView.tsx` | later |  |
| `src/components/operate/ApprovalsView.outcome.test.tsx` | later |  |
| `src/components/operate/ApprovalsView.test.tsx` | later |  |
| `src/components/operate/ApprovalsView.tsx` | later |  |
| `src/lib/api/build-endpoints.ts` | later | area follows its types file. |
| `src/lib/api/build-types.ts` | later | shared by build and operate files; the layer rule gives governance (approvals). Split when ported. |
| `src/lib/api/operate-endpoints.ts` | later | area follows its types file. |
| `src/lib/api/operate-types.ts` | later | shared by runs, approvals and pipelines; the layer rule gives governance (approvals). |
| `src/lib/build-hooks.ts` | later |  |
| `src/lib/operate-hooks.ts` | later |  |
| `src/lib/run-utils.test.ts` | later |  |
| `src/lib/run-utils.ts` | later |  |
| `src/test/operate.ts` | later |  |

## build

| File | Status | Notes |
|---|---|---|
| `src/app/(app)/o/[org]/build/agents/[id]/page.tsx` | later |  |
| `src/app/(app)/o/[org]/build/agents/page.tsx` | later |  |
| `src/app/(app)/o/[org]/build/layout.tsx` | later |  |
| `src/app/(app)/o/[org]/build/orchestrations/[id]/page.tsx` | later |  |
| `src/app/(app)/o/[org]/build/orchestrations/page.tsx` | later |  |
| `src/app/(app)/o/[org]/build/pipelines/[id]/page.tsx` | later |  |
| `src/app/(app)/o/[org]/build/pipelines/layout.tsx` | later |  |
| `src/app/(app)/o/[org]/build/pipelines/page.tsx` | later |  |
| `src/app/(app)/o/[org]/build/prompts/[id]/page.tsx` | later |  |
| `src/app/(app)/o/[org]/build/prompts/page.tsx` | later |  |
| `src/app/(app)/o/[org]/build/publish-requests/page.tsx` | later |  |
| `src/app/(app)/o/[org]/build/skills/[id]/page.tsx` | later |  |
| `src/app/(app)/o/[org]/build/skills/page.tsx` | later |  |
| `src/app/(app)/o/[org]/build/tools/[id]/page.tsx` | later |  |
| `src/app/(app)/o/[org]/build/tools/page.tsx` | later |  |
| `src/components/build/AgentEditor.test.tsx` | later |  |
| `src/components/build/AgentEditor.tsx` | later |  |
| `src/components/build/AgentTestPanel.tsx` | later |  |
| `src/components/build/ArtifactDetailView.tsx` | later | imports ontology editors (`components/ontology/*`) for the ontology kind; the ontology port must wire these in. |
| `src/components/build/ArtifactListView.test.tsx` | later |  |
| `src/components/build/ArtifactListView.tsx` | later |  |
| `src/components/build/DraftEditor.tabs.test.tsx` | later |  |
| `src/components/build/DraftEditor.tsx` | later |  |
| `src/components/build/OrchestrationCanvas.tsx` | later |  |
| `src/components/build/OrchestrationEditor.test.tsx` | later |  |
| `src/components/build/OrchestrationEditor.tsx` | later |  |
| `src/components/build/PromptBuilder.test.tsx` | later |  |
| `src/components/build/PromptEditor.tsx` | later |  |
| `src/components/build/PromptPanels.tsx` | later |  |
| `src/components/build/PublishRequestsView.test.tsx` | later |  |
| `src/components/build/PublishRequestsView.tsx` | later |  |
| `src/components/build/SectionTabs.test.tsx` | later |  |
| `src/components/build/SectionTabs.tsx` | later |  |
| `src/components/build/SkillEditor.test.tsx` | later |  |
| `src/components/build/SkillEditor.tsx` | later |  |
| `src/components/build/ToolEditor.test.tsx` | later |  |
| `src/components/build/ToolEditor.tsx` | later |  |
| `src/components/build/ToolTestPanel.tsx` | later |  |
| `src/components/build/VersionsPanel.tsx` | later |  |
| `src/components/build/pickers.tsx` | later |  |
| `src/components/build/shared.tsx` | later |  |
| `src/components/operate/CancelRunButton.tsx` | later |  |
| `src/components/operate/RunAgentLink.test.tsx` | later |  |
| `src/components/operate/RunAgentLink.tsx` | later |  |
| `src/components/operate/parts.tsx` | later |  |
| `src/components/pipelines/LazyPipelineCanvas.tsx` | later |  |
| `src/components/pipelines/NodeConfigPanel.tsx` | later |  |
| `src/components/pipelines/NodePalette.tsx` | later |  |
| `src/components/pipelines/NodeStatusList.outcome.test.tsx` | later |  |
| `src/components/pipelines/NodeStatusList.tsx` | later |  |
| `src/components/pipelines/PipelineCanvas.tsx` | later |  |
| `src/components/pipelines/PipelineEditor.outcome.test.tsx` | later |  |
| `src/components/pipelines/PipelineEditor.test.tsx` | later |  |
| `src/components/pipelines/PipelineEditor.tsx` | later |  |
| `src/components/pipelines/PipelineHistory.tsx` | later |  |
| `src/components/pipelines/PipelineRun.test.tsx` | later |  |
| `src/components/pipelines/PipelineRunContext.tsx` | later |  |
| `src/components/pipelines/PipelineRunPanels.tsx` | later |  |
| `src/components/pipelines/PipelineScreens.test.tsx` | later |  |
| `src/components/pipelines/PipelineValidationPanel.tsx` | later |  |
| `src/components/pipelines/SchedulesPanel.tsx` | later |  |
| `src/components/pipelines/TransformEditor.tsx` | later |  |
| `src/lib/agent-spec.test.ts` | later |  |
| `src/lib/agent-spec.ts` | later |  |
| `src/lib/api/pipeline-endpoints.ts` | later | area follows its types file. |
| `src/lib/api/pipeline-types.ts` | later |  |
| `src/lib/expr.test.ts` | later |  |
| `src/lib/expr.ts` | later |  |
| `src/lib/failure.ts` | later |  |
| `src/lib/graph.test.ts` | later |  |
| `src/lib/graph.ts` | later |  |
| `src/lib/pipeline-config.ts` | later |  |
| `src/lib/pipeline-hooks.ts` | later |  |
| `src/lib/pipeline-params.test.ts` | later |  |
| `src/lib/pipeline-params.ts` | later |  |
| `src/lib/pipeline-spec.test.ts` | later |  |
| `src/lib/pipeline-spec.ts` | later |  |
| `src/lib/prompt-utils.test.ts` | later |  |
| `src/lib/prompt-utils.ts` | later |  |
| `src/lib/tool-rules.test.ts` | later |  |
| `src/lib/tool-rules.ts` | later |  |
| `src/test/build.ts` | later |  |
| `src/test/pipelines.ts` | later |  |

## ontology

| File | Status | Notes |
|---|---|---|
| `src/app/(app)/o/[org]/build/ontologies/[id]/lineage/page.tsx` | later |  |
| `src/app/(app)/o/[org]/build/ontologies/[id]/page.tsx` | later |  |
| `src/app/(app)/o/[org]/build/ontologies/layout.tsx` | later |  |
| `src/app/(app)/o/[org]/build/ontologies/page.tsx` | later |  |
| `src/components/ontology/ActionsEditor.test.tsx` | later |  |
| `src/components/ontology/ActionsEditor.tsx` | later |  |
| `src/components/ontology/ActionsPanel.tsx` | later |  |
| `src/components/ontology/ExplorerPanel.tsx` | later |  |
| `src/components/ontology/LazyOntologyCanvas.tsx` | later |  |
| `src/components/ontology/LineagePanel.tsx` | later |  |
| `src/components/ontology/MappingPanel.tsx` | later |  |
| `src/components/ontology/ObjectTypeDialog.tsx` | later |  |
| `src/components/ontology/OntologyCanvas.tsx` | later |  |
| `src/components/ontology/OntologyEditor.test.tsx` | later |  |
| `src/components/ontology/OntologyEditor.tsx` | later |  |
| `src/components/ontology/OntologyGate.test.tsx` | later |  |
| `src/components/ontology/OntologyPanels.test.tsx` | later |  |
| `src/components/ontology/OntologyValidationPanel.tsx` | later |  |
| `src/components/ontology/PoliciesPanel.tsx` | later |  |
| `src/components/ontology/PrincipalAttributesDialog.tsx` | later |  |
| `src/components/ontology/RelationshipDialog.tsx` | later |  |
| `src/lib/api/ontology-endpoints.ts` | later | area follows its types file. |
| `src/lib/api/ontology-types.ts` | later |  |
| `src/lib/ontology-actions.test.ts` | later |  |
| `src/lib/ontology-hooks.ts` | later |  |
| `src/lib/ontology-spec.test.ts` | later |  |
| `src/lib/ontology-spec.ts` | later |  |
| `src/test/ontology.ts` | later |  |

## runtime

| File | Status | Notes |
|---|---|---|
| `src/app/(app)/o/[org]/command/command.css` | later |  |
| `src/app/(app)/o/[org]/command/layout.tsx` | later |  |
| `src/app/(app)/o/[org]/command/page.tsx` | later |  |
| `src/app/(app)/o/[org]/runs/[id]/page.tsx` | later |  |
| `src/app/(app)/o/[org]/runs/layout.tsx` | later |  |
| `src/app/(app)/o/[org]/runs/page.tsx` | later |  |
| `src/app/(app)/o/[org]/w/[ws]/p/[proj]/run/layout.tsx` | later |  |
| `src/app/(app)/o/[org]/w/[ws]/p/[proj]/run/page.tsx` | later |  |
| `src/app/(app)/o/[org]/w/[ws]/p/[proj]/runs/layout.tsx` | later |  |
| `src/app/(app)/o/[org]/w/[ws]/p/[proj]/runs/page.tsx` | later |  |
| `src/components/command/AdminPanel.tsx` | later |  |
| `src/components/command/CommandCenter.test.tsx` | later |  |
| `src/components/command/CommandCenter.tsx` | later |  |
| `src/components/command/NetworkMatrix.tsx` | later |  |
| `src/components/command/Reveal.tsx` | later |  |
| `src/components/command/RunTelemetry.tsx` | later |  |
| `src/components/command/StartupReadout.tsx` | later |  |
| `src/components/command/Tabs.tsx` | later |  |
| `src/components/command/ToneGlyph.tsx` | later |  |
| `src/components/command/TrussHero.tsx` | later |  |
| `src/components/operate/RunConsole.test.tsx` | later |  |
| `src/components/operate/RunConsole.tsx` | later |  |
| `src/components/operate/RunDetailView.outcome.test.tsx` | later |  |
| `src/components/operate/RunDetailView.test.tsx` | later |  |
| `src/components/operate/RunDetailView.tsx` | later |  |
| `src/components/operate/RunHistoryView.outcome.test.tsx` | later |  |
| `src/components/operate/RunHistoryView.test.tsx` | later |  |
| `src/components/operate/RunHistoryView.tsx` | later |  |
| `src/components/operate/Timeline.tsx` | later |  |
| `src/components/pipelines/RunNodesTab.tsx` | later | UNSURE: pipeline file, but only `operate/RunDetailView` (runtime) imports it, so runtime by the layer rule. |
| `src/lib/command.test.ts` | later |  |
| `src/lib/command.ts` | later |  |
