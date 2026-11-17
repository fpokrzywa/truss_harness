# fe-areas-5 notes: runtime front-end and platform status

Ported verbatim from SETH 3ba0e06 (70 `later` rows of frontend-areas.md): runs (list, detail, run console, timeline, `RunNodesTab`), approvals, pipelines (pages, editor, canvas, run, history, schedules, validation, `NodeStatusList`), the command center (`/o/{org}/command`, `components/command/*`, `lib/command.ts`), `lib/operate-hooks.ts`, `lib/pipeline-hooks.ts`, `lib/run-utils.ts`, `operate-endpoints.ts`, `pipeline-endpoints.ts`, `ontology/ActionsPanel.tsx`, and the platform status page (`PlatformStatusView`, `TelemetryPanel`, `platform/status/page.tsx`). `ArtifactDetailView.tsx` and `OntologyPanels.test.tsx` are restored to SETH content (pipeline tabs, Actions tab, `ActionsPanel` tests).

Contract check against `backend/src/shifde/runtime/api.py` and `tenancy/api.py`: every endpoint the screens call exists with a compatible shape (`/runs` incl. pipeline kind and `Idempotency-Key`, `/runs/{id}/steps|events|result|cancel|nodes|artifacts`, `/approvals` and `decide`, `/pipelines/{id}/runs|schedules`, `/pipelines/node-types`, `POST /ontologies/{id}/actions/{key}/invoke`, `GET /platform/status`). The earlier `later` reason for `ActionsPanel` (no invoke route) no longer holds: APP-2 added it.

Modified:
- `lib/api/endpoints.ts`: `platformStatus` added; operate and pipeline endpoint groups spread in. `lib/api/types.ts`: `PlatformStatus` added (shape equals `PlatformStatusOut`).
- `lib/contrast.ts` (new): `contrastRatio` extracted from SETH `branding.ts`; `lib/command.test.ts` imports it from `./contrast` (assertions unchanged).
- Assertion change, `PlatformStatusView.test.tsx`: the `profile: null` property removed from the installation fixture, because the unified `PlatformInstallation` has no `profile` (B-1101). No other assertion changed.

Navigation: `nav.ts` already carries the Runs and Approvals items (feature `operate`), the Build Pipelines item (feature `pipelines`) and the platform status link; no change was needed. `RunAgentLink` shows when the backend advertises `operate`, which `app.py` now registers.

Left `later`: the five security groups rows (`GroupsView`, `GroupDetailView`, pages, test). They belong to the admin unit, not runtime: the org-scoped group create/membership routes still do not exist (groups live under `/api/v1/platform/groups`).

API gaps for the lead: none. The SETH UI has no create-schedule form, so `POST .../schedules` is unused.
