# Isolation review 1: job 036 (#83 SP3-6 Entra, PR #100)

**Verdict: BLOCKED, 1 blocking finding, 9 notes (all OK).** Reviewer: `isolation-reviewer`, 2026-11-20. Scope: `git diff origin/main...44afc70`.

## Blocking

1. `identity/oidc.py:472-474` (`_sign_in`, entra branch) treats the `email` claim, else `preferred_username`, as verified. In Entra `email` is the mutable, unverified `mail` attribute (nOAuth class); for B2B guests in the pinned tenant (`tid` passes) it comes from the invitation or the home directory. It is passed as `link_email` to `shifde_resolve_identity` (`identity_0004_oidc.py:87-92`), which for an installation IdP (`v_tenant` NULL) links the Entra `oid` to any user, in any client, with a pending link token and that primary e-mail. A guest or member of the pinned directory with `email=<client B admin>` signing in during B's link window is bound to B's account: takeover into another client. JIT also creates users with unverified addresses that e-mail-keyed invitations can later attach to.
   Fix: trust `email` only when the optional claim `xms_edov` is true, never for guests (`acct` = 1, or a UPN containing `#EXT#`); trust `preferred_username` only for non-guest members; otherwise `link_email=None` and no JIT. Test: a guest-shaped token whose unverified `email` matches a pending-link user in another client is not linked.

## Notes (OK)

2. Recreated `shifde_resolve_idp(uuid)` keeps the identity_0004 body, locked search_path, WHERE filter; EXECUTE revoked from PUBLIC, granted to `shifde_app`; owned by `shifde_owner`.
3. Both CHECKs sound; `ck_idp_configs__entra_installation` stops a client IdP from taking the entra profile; grant is UPDATE on the two columns only.
4. tid/ver/oid checks after signature, issuer and nonce; subject is canonical `oid`; issuer pinned per tenant.
5. Overage refuses before identity mapping and sync; groups lowercased; pre-entra groups fail closed.
6. Create and merged-row patch refuse entra for a client scope, backed by the DB CHECK, tested.
7. `identity.oidc.groups_overage` audited pre-tenant, details reason and idp_id only.
8. Only the overage reason gets its own page.
9. `deliberate_changes.yaml`: six entries, all `idp_configs`, matching the migration.
10. Cross-client group test present; no test of the guest / unverified-email link path (finding 1).
