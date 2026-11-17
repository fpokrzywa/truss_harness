# fe-areas-4 notes: build and ontology front-end areas

Ported (verbatim from SETH 3ba0e06 unless the frontend-areas.md row says modified): the build screens for agents, tools, skills, prompts, orchestrations and publish requests; the ontology screens (list, detail, lineage) and panels; their libs and tests. `build-endpoints.ts`, `build-types.ts` and `build-hooks.ts` replace the governance slices (B-2210) with the full SETH files. `createBuildEndpoints` and `createOntologyEndpoints` are spread into `endpoints.ts`. `nav.ts` needed no change: the build and ontology items already point at these routes.

Pulled forward from runtime-owned rows (types and a test stub only, copied verbatim): `lib/api/operate-types.ts` (orchestration spec types, `ErrorClass`, `Run`) and `test/operate.ts` (`stubOperate`). The runtime unit may overwrite them with identical content.

Left `later` (runtime routes not mounted: `app.py` registers `operate` and `pipelines` with runtime):
- Pipelines: pages, `components/pipelines/*` (editor, canvas, run, history, schedules), `pipeline-hooks.ts`, `pipeline-endpoints.ts`, `operate/CancelRunButton.tsx`, `operate/parts.tsx`. The editor reads run state from `operate-hooks` and draws run outcomes on the canvas. The pure pipeline libs (`pipeline-spec`, `pipeline-params`, `pipeline-config`, `pipeline-types`, `expr`, `graph`, `failure`) and `test/pipelines.ts` are ported with their tests.
- `ontology/ActionsPanel.tsx`: the backend has no `POST .../ontologies/{id}/actions/{key}/invoke` route. `invokeAction` stays in `ontology-endpoints.ts` as in SETH.

Modified: `ArtifactDetailView.tsx` (pipeline tabs and the Actions tab removed; the pipelines case renders nothing and no route reaches it). `OntologyPanels.test.tsx` lost its `ActionsPanel` describe block and its fixture with the component; no other assertion changed.

API gaps for the lead: none beyond the above. `RunAgentLink` stays hidden until the backend advertises the `operate` feature.
