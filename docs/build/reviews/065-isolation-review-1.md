# 065 (issue #93, PR #154) isolation review 1: BLOCKED

Reviewer: isolation-reviewer (lead session, 2026-11-22). Diff: origin/main...origin/factory/065-sp3-follow-up-redact. Recorded by the lead from the reviewer's report.

No cross-client exposure. The stated within-client redaction can still be bypassed:

1. **Blocking.** `backend/src/shifde/packaging/service.py:346` (`_version_out`, `spec=dict(row["spec"])`, served by GET/list `.../solutions/{sid}/versions[/{vid}]`, not in the diff) returns `spec.data_reads[].connection_id` unredacted. The project developer of `test_hidden_models_and_connections_are_redacted_for_a_reader` can read the solution (the same READ the release needs), so they get `s.connection` from the version route while `_release_out` (line 879) nulls it. Fix: in `_version_out`, null each data-read `connection_id` that `self._readable` rejects, as `_release_out` does; test that this reader's GET and list of the version omit `s.connection`.
2. Not blocking, same class: when a tool version is visible but its connection is not readable (`hidden("connections")`, service.py:700-705), the release hides that connection, but the tool's own version GET (`backend/src/shifde/build/service.py:387`) still shows `spec.binding.connection_id` to that reader.
3. Not blocking: the count of `models.hidden` / `connections.hidden` diagnostics reveals how many pins are hidden; matches existing `tools.hidden` behaviour; accepted.

Checked, no problem: every new lookup (`get_version`, `version_access`, `version_spec`, `pipeline_resource_refs`, `conns.placement`) is organization-scoped; another client's id gets the same answer as an unknown id. `_check_binding_keys` answers a hidden key exactly as an unknown one (test compares bodies). The refused-seal audit row uses redacted paths; stored manifest and digest unchanged. `set_state` matches 23514 plus the migration's exact message; other errors re-raised. No routes added.
