# 065 (issue #93, PR #154) isolation review 2: APPROVED

Reviewer: isolation-reviewer (lead session, 2026-11-22), round 2 head. Recorded by the lead from the reviewer's report.

1. Finding 1 closed: packaging `_version_out` nulls each unreadable `data_reads[].connection_id` (same check as `_release_out`); version GET and list go through it; `test_unreadable_connection_ids_are_null_on_version_routes` asserts it.
2. Finding 2 closed for tools: build `_shown_spec` nulls `spec.binding.connection_id` on every version answer and on `diff`; `spec_hash` unchanged; `conns.placement` is organization-scoped.
3. Not blocking, filed as a follow-up: a visible pipeline version still shows the connection ids its nodes name (`spec.nodes[].config.connection_id`, `refs[].ref_id`, `diff`), which the release hides.
4. Rest sound: `set_state` 409 only for the guard's 23514; hidden binding key answered as unknown; audit paths redacted; manifest and digest unchanged; no routes added. No cross-client exposure.
