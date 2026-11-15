# build isolation review 1 (part 1: data layer, `build_0001`)

## Round 1 (`4205524..784c2bd`): BLOCKED

Clean: RLS enabled and forced on all eight tables, one `tenant_isolation` policy on `current_tenant_id()` in USING and WITH CHECK, `tenant_id` NOT NULL; grants SELECT, INSERT, column UPDATE as SETH (no DELETE, no definer); every FK between client tables includes `tenant_id`; repository reads, writes and joins filter on `tenant_id`; `update_returning`'s new `tenant_id` keyword defaults to None and every build caller passes it.

1. `build_0001_build.py:327, :202, :298`: the PKs of `pipeline_validation_reports`, `artifact_version_pins`, `pipeline_version_refs` lacked `tenant_id`, so A's `ON CONFLICT DO NOTHING` inserts (`repository.py:313-321, 501-510, 535-547`) collided with B's rows. Probe under A's context: B's id with a row succeeded silently where an unknown id gave 23503, and A waited on B's uncommitted insert (55P03). A could confirm B's version ids, validation state and pin/ref graph. The test asserting the silent no-op pinned the oracle as correct.
2. `repository.py:603-633` `upsert_mapping`, `:697-706` `insert_ontology_info` (not reachable yet): conflict target without the client, DO UPDATE without a tenant filter, no `tenant_id` argument.

## Round 2 (`5701758`): code fix correct; BLOCKED on tests

`tenant_id` first in the three PKs (six `deliberate_changes.yaml` entries, no `test_rls_schema.py` change); EXISTS-guarded ontology writes with a tenant-filtered DO UPDATE. But 4 of 6 new cases (pin, ref) passed against the old PKs, because A never wrote a key B held.

## Round 3 (`7f05da8`): APPROVED

A now writes B's real keys (pin `(b.draft, b.published, 'prompt')` and `(version, b.published, 'tool')`; ref `("n1", "model", b.model)`). On the old PKs each case hits B's row (silent success or 55P03) and fails; the builder showed all 6 failing with the PKs recreated without `tenant_id`. On the current PKs every case gets the composite-FK 23503 with no wait.

Follow-ups (carry_forward "build (part 2-4)" and the ontology row): server-minted ids only; workspace/project scope and `visibility='private'` enforced in the service; principal columns from the session; due-schedule scan through a client-scoped claim (runtime); ontology migration keys the mappings unique constraint and the info PK on `tenant_id`.
