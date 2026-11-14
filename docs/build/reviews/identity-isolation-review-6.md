# Identity part 3b fix round 1: isolation review, round 6

Range `ccfe02a..b62d03b` on `identity`. Reviewer: isolation-reviewer (opus), 2026-11-14. **Verdict: APPROVED** (review 5 items 1-2 closed). Group, cross-client, schema and OIDC-hardening modules at `b62d03b`: 197 passed.

1. Closed: `test_rls_schema.py:168` no longer lists `shifde.group_members` in `PRINCIPAL_TABLES`; line 1225 asserts it stays out; `own_row` pinned by exact text in `REVIEWED_EXPRESSIONS` (220-231); `test_a_widened_group_members_policy_is_reported` covers a self-join INSERT policy and a widened `own_row`; `test_a_staff_member_cannot_write_group_memberships_with_or_without_a_client` as `shifde_app` (no client, then B's): self-join and adding a colleague refused, no UPDATE grant, DELETE 0 rows, membership unchanged.
2. Closed: `identity_0005_platform_groups.py` (~77) the sync definer refuses first whenever `current_tenant_id()` is set; `test_the_sync_definer_refuses_inside_a_client_context` (A with/without principal, with the admin flag, enrol and drop); control call with no client works; editing the unmerged migration in place is acceptable.
3. FOLLOW-UP identity: flaky `_same_failure` (`tests/identity/test_cross_client.py:766`) compares the raw Set-Cookie header incl. wall-clock `Expires`: compare parsed names, values, Max-Age and attributes without `Expires`, or freeze the clock.
4. Review 5 follow-ups 3-5 and the sync audit gap stand.
