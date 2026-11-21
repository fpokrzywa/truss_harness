APPROVED

Isolation review 1 of job 038-sp3-8-front-end (#85, PR #102), head `dda51d6` vs `origin/main`. Reviewer: `isolation-reviewer` (read-only; recorded by the lead from its report).

No way found for one organization to reach another's data through these screens.

1. Release picker (`frontend/src/components/deliver/PlansView.tsx`): every list comes from org-scoped routes. POST plans does not trust the picker: `_gate` in `backend/src/shifde/delivery/plans.py:304-318` calls `scoped_release` (`backend/src/shifde/packaging/service.py:171`), which reads the release by tenant and runs a policy check; an unknown or another client's release gets the same "release not found". The release's workspace and project must match the target's. Withdrawn releases are refused on the server too.
2. Org IdP dialog: the UI omits `profile`, and the server enforces it: `_check_profile` in `backend/src/shifde/admin/providers.py:176-194` refuses `entra` on a client IdP on create and update; database constraint `ck_idp_configs__entra_installation` (`identity_0007_entra_profile`) does the same.
3. Group mapping (`POST /api/v1/platform/groups`, `backend/src/shifde/admin/iam.py:993`): platform administrator only; trigger `trg_groups_idp_scope` (identity_0005) accepts only an installation IdP (client IdP id refused 409); Entra object id checked as a GUID server-side; audited. `GET /platform/groups` also requires a platform administrator (`iam.py:894`). The dialog's IdP filter only affects display.

Non-blocking:
4. `EntraGroupsDialog` in `frontend/src/components/security/IdpsView.tsx` reads only the first page of `/platform/groups`; with many groups some mappings may be missing from the table. Display gap, not a leak.
5. No routes changed, so no new cross-client tests are needed.
