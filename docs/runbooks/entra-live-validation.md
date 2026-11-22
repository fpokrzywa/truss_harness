# Entra live validation (owner runbook)

Sign-in through Microsoft Entra ID is `experimental`. It has only run against the fake Entra
issuer of the tests (`backend/tests/_support/fake_oidc.py`, `FakeOidcProvider.entra`). This
runbook is for the owner, once an Entra app registration exists: it runs the opt-in live test
`backend/tests/identity/test_entra_live.py` against the real tenant, then a manual sign-in in
the browser.

A passing run is evidence, nothing more. Entra stays `experimental` and is not called validated
because of this run; changing that status (and REQ F-22) is a separate decision for the owner.

The in-process proof against the fake Entra issuer (group mapping, access to `demo-a` but not
`demo-b`, and the overage refusal) is `python scripts/release_demo.py --fake`.

## Prerequisites

- **An Entra tenant chosen by the owner**, with permission to register an application there.
- **An app registration** (Microsoft Entra admin center, App registrations, New registration):
  - Supported account types: accounts in this organizational directory only (single tenant).
  - Redirect URI, platform Web: `{SHIFDE_PUBLIC_BASE_URL}/api/auth/oidc/{idp_id}/callback`, where
    `SHIFDE_PUBLIC_BASE_URL` is the API origin of the SETH installation (required in production;
    in development and test, when unset, the first of `SHIFDE_ALLOWED_ORIGINS`) and `idp_id` the id of the IdP created below (create the IdP first,
    then add the redirect URI).
  - A client secret (Certificates & secrets), entered once into SETH and never anywhere else.
  - Token configuration, Add groups claim: **Security groups**, emitted as **Group ID** in the
    ID token. SETH maps groups by their object ids only.
  - Optional claims of the ID token: `acct` (**required**), `email`, `preferred_username` and
    `xms_edov`. `acct` is what proves a person is a member of the tenant (0) and not a guest:
    without it no address is ever verified, so nobody is linked or provisioned and sign-in works
    only through identities already bound to a person. `xms_edov` says Entra verified the
    `email` domain; without it `email` is never trusted.

## The env file

Create `.devdata/entra.env` in the root of the checkout that runs the test (a worktree does not
see the main checkout's file). One `KEY=value` per line, these keys exactly:

- `ENTRA_TENANT_ID`: the directory (tenant) id, a GUID
- `ENTRA_CLIENT_ID`: the application (client) id of the registration

The test reads only this file, never process environment variables. A missing or empty key or a
tenant id that is not a GUID fails the test before Entra is contacted; the failure names the key
or rule, never a value. The client secret does not go in this file.

`.devdata/` is gitignored. Never commit this file, never print it, and never paste its contents
(or the client secret) to an agent, an issue or a chat.

## Run the live test

```powershell
cd backend; uv run pytest tests/identity/test_entra_live.py -m live
```

Without the env file the test is skipped with "live validation waiting on owner input: no Entra
registration". With the file but without `-m live` (a default pytest run, CI or
`factory/checks.sh`) it is skipped with "live validation is opt-in: run with -m live".

The test runs the real `OidcClient.run_checks` against
`https://login.microsoftonline.com/<tenant>/v2.0`: the discovery document's issuer must equal
that issuer, and its JWKS must publish signing keys. No database is used, nothing is written and
no value is printed. A full sign-in needs a person at a browser: it is the manual step below.

## Create the Entra IdP in SETH

As the platform administrator, `POST /api/v1/platform/idps` (installation IdPs only: a client's
IdP refuses the `entra` profile with 422):

```json
{
  "display_name": "Contoso Entra",
  "profile": "entra",
  "entra_tenant_id": "<tenant GUID>",
  "issuer": "https://login.microsoftonline.com/<tenant GUID, lowercase>/v2.0",
  "discovery_url": "https://login.microsoftonline.com/<tenant GUID, lowercase>/v2.0/.well-known/openid-configuration",
  "client_id": "<application (client) id>",
  "client_secret": "<client secret>",
  "scopes": ["openid", "email", "profile"],
  "jit": "create_user",
  "group_sync": "sync"
}
```

The issuer, the discovery URL and the groups claim (`claim_mappings.groups`, `groups` if absent)
are pinned to the tenant: any other value is a 422 naming the rule, on create and on a patch.
Then add the redirect URI with the new IdP's id to the app registration, and run
`POST /api/v1/platform/idps/{idp_id}/test` (discovery and JWKS from the server).

At each sign-in SETH checks, after the ID token is valid, that `tid` is the IdP's tenant,
`ver` is `2.0` and `oid` is a GUID; the person is keyed by `oid`, never by Entra's pairwise
`sub`. An installation IdP links a pending setup token in any client by e-mail, so an address
counts as verified (for that link and for just-in-time provisioning) only for a member of the
tenant (DECISIONS B-2712, narrowing design SP3 section 12 item 24):

- only `acct` present and equal to 0 proves a member; a guest (`acct` 1), a token without
  `acct` or with any other value, and any `preferred_username` / `upn` containing `#EXT#`
  never has a verified address: they sign in only through an identity already bound to them;
- a member's `email` is verified only when `xms_edov` is `true`;
- otherwise a member's `preferred_username` (their UPN in the tenant) is the verified address;
- with neither, the sign-in has no verified address: no link, no new user
  (`jit_requires_verified_email`).

Any refusal is audited `identity.login.failed` with the
reason (`entra_tenant_mismatch`, `entra_version_invalid`, `entra_oid_invalid`) and the browser
gets the generic sign-in error.

## Map a platform group by Entra object id

As the platform administrator, `POST /api/v1/platform/groups` with `idp_config_id` the Entra
IdP's id and `external_id` the security group's **object id** (a GUID; any other value is a
422). It is stored lowercase, and the groups claim is lowercased before matching, so the case
the GUID was given in does not matter. Bind the group in a client as usual; the person's
membership follows the groups claim at every sign-in.

## Group overage

When a person is in more groups than fit in the token, Entra sends `_claim_names` /
`_claim_sources` (or `hasgroups`) instead of the groups. SETH does not call Microsoft Graph: with
group sync on, the sign-in is refused (fail closed, DECISIONS B-2711), as it is for more than 200
groups in the claim. The person's group memberships stay as they were, no session is created,
the refusal is audited once as `identity.oidc.groups_overage`, and the login page shows:

> Your Entra account is in too many groups for SETH to read. Ask an administrator to limit the
> groups sent to SETH.

To fix it, limit the groups in the registration's groups claim (for example, groups assigned to
the application only).

## Manual browser sign-in

1. Start SETH with `SHIFDE_PUBLIC_BASE_URL` set to the origin named in the redirect URI.
2. Open the login page; the Entra IdP's button is listed under its display name.
3. Sign in as a test person of the tenant (a member, not a guest) who is in a mapped security
   group.
4. Check: you land in SETH signed in; the Users screen shows the person with the expected
   address; their platform group membership matches the group; the audit log has the sign-in
   and no failure. A second sign-in maps to the same person.
