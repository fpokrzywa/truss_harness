# Snowflake live validation (owner runbook)

The `snowflake` connection adapter is `experimental`. It has only run against the in-process fake.
This runbook is for the owner, once a Snowflake development account exists: it runs the opt-in
live test `backend/tests/adapters/dataplatforms/test_snowflake_live.py` against that account.

A passing run is evidence, nothing more. The adapter stays `experimental` and is not called
validated because of this run; changing that status is a separate decision for the owner.

## Prerequisites

- **A development account chosen by the owner.** Never a client account, never a production
  account. Its account identifier (`<orgname>-<account_name>`, or a locator such as
  `xy12345.west-europe.azure`) must give a host under `.snowflakecomputing.com`.
- **A user with key-pair authentication.** Generate an unencrypted PKCS#8 RSA key of at least
  2048 bits, for example:

  ```bash
  openssl genrsa 2048 | openssl pkcs8 -topk8 -inform PEM -out snowflake_live.p8 -nocrypt
  openssl rsa -in snowflake_live.p8 -pubout -out snowflake_live.pub
  ```

  Then set the public key (the base64 body of `snowflake_live.pub`, without the header and
  footer lines) on the user: `ALTER USER <user> SET RSA_PUBLIC_KEY='<public key body>';`.
  Passphrase-protected (encrypted) keys and PKCS#1 keys (`BEGIN RSA PRIVATE KEY`) are refused.
- **A role with read-only grants**: `USAGE` on the warehouse, the databases and schemas to
  discover, and `SELECT` on their tables. No write grant is needed or wanted.
- **A warehouse** the role may use; `SELECT 1` runs on it.

## The env file

Create `.devdata/snowflake.env` in the root of the checkout that runs the test (a worktree does not
see the main checkout's file). One `KEY=value` per line, these keys exactly:

- `SNOWFLAKE_ACCOUNT`: the account identifier
- `SNOWFLAKE_USER`: the user with the key pair
- `SNOWFLAKE_PRIVATE_KEY_FILE`: the path of the private key file, absolute or relative to
  `.devdata/` (for example `snowflake_live.p8` with the key in `.devdata/`)
- `SNOWFLAKE_ROLE`: the read-only role
- `SNOWFLAKE_WAREHOUSE`: the warehouse
- `SNOWFLAKE_ACCOUNT_PURPOSE`: must be `development`; any other value refuses the run

The test reads only this file (and the key file it names), never process environment variables.
A purpose other than `development`, a key file that cannot be read, a key that is not an
unencrypted PKCS#8 RSA key of at least 2048 bits or an invalid account identifier fails the test
before any host is contacted; the failure names the key or rule, never a value.

`.devdata/` is gitignored. Never commit this file or the key file, never print them, and never
paste their contents (or the key) to an agent, an issue or a chat.

## Run the live test

```powershell
cd backend; uv run pytest tests/adapters/dataplatforms/test_snowflake_live.py -m live -q
```

Without the env file the test is skipped with "live validation waiting on owner input: no
Snowflake account". With the file but without `-m live` (a default pytest run, CI or
`factory/checks.sh`) it is skipped with "live validation is opt-in: run with -m live". A missing
or empty key skips it with "`<KEY>` is missing or empty in the live env file".

## What it does

The test drives the real adapter with key-pair auth, behind the strict production egress policy
(https on port 443, public addresses only), in this order:

1. `test`: the configuration, egress, DNS, TLS, authentication and capability checks must all pass
2. `discover`: a schema of the tables the role can see
3. `SELECT 1` on the warehouse must answer `1`

It is read only: nothing is written to the account and nothing needs cleanup. Running `SELECT 1`
(and the test's own statement) may resume a suspended warehouse and use credits. No database is
used and nothing is written to the repository.

## What to record

For each run, record:

- the date and the commit (`git rev-parse HEAD`)
- pass or fail of each step above

Never record the account, the user, the key, the key path or the env file's contents.

## Status

The adapter remains `experimental` whatever the outcome of these runs. It is not validated by this
runbook; promoting it is a separate decision for the owner.
