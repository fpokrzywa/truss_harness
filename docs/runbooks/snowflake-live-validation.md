# Snowflake live validation (owner runbook)

The `snowflake` connection adapter is `experimental`. It has only run against the in-process fake.
This runbook is for the owner, once a Snowflake development account exists: it runs the opt-in
live test `backend/tests/adapters/dataplatforms/test_snowflake_live.py` against that account. The
generated Native App has its own opt-in live test, in "Native App" below; everything before that
section is about the read connection.

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
see the main checkout's file). One `KEY=value` per line. The read test uses these keys exactly and
ignores the deploy keys of "Native App" below, which are never used to read client data:

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

## Native App

The generated Snowflake Native App is `experimental`. It has only run against the in-process fakes.
This section is for the owner: it runs the opt-in live test
`backend/tests/adapters/native/test_snowflake_native_live.py`, which deploys the sample app to the
development account. Unlike the read test above, it **writes** to the account and leaves objects
that are removed by hand ("Cleanup").

### Preconditions

- **#358 is closed on the commit being run.** No live Snowflake app is deployed before it (S-043,
  S-047). The test checks two fixed probes of it first and fails before any host is contacted; they
  are not a proof of all of #358, so check the issue too.
- **A development account only.** Never a client account, never a production account.
- **`SNOWFLAKE_ACCOUNT` is an organization-account name** (`<orgname>-<account_name>`). An account
  locator (`xy12345`, `xy12345.west-europe.azure`) and a `privatelink` name are refused by this test,
  although the read test accepts a locator.
- **The Snowflake CLI 3.33.0**, installed from the hashed lock in its own virtual environment (never
  in the backend's) and on `PATH` as `snow`:

  ```powershell
  python -m venv C:\tools\snowflake-cli
  C:\tools\snowflake-cli\Scripts\python -m pip install --require-hashes -r tools/snowflake-cli.txt
  $env:PATH = "C:\tools\snowflake-cli\Scripts;$env:PATH"; snow --version
  ```

- **Egress** (B-3926): https to the account host and, for the stage copy, to the account's stage
  cloud storage (presigned S3, Azure Blob or GCS endpoints) and the certificate OCSP responders.

### The deploy role and user

A user of its own with its own key pair, never the read connection's. Make the key as in
"Prerequisites" above, under another name, then as an administrator:

```sql
CREATE ROLE <deploy role>;
GRANT CREATE APPLICATION PACKAGE ON ACCOUNT TO ROLE <deploy role>;
GRANT CREATE APPLICATION ON ACCOUNT TO ROLE <deploy role>;
GRANT USAGE ON WAREHOUSE <warehouse> TO ROLE <deploy role>;
CREATE USER <deploy user> TYPE = SERVICE DEFAULT_ROLE = <deploy role>
  RSA_PUBLIC_KEY = '<public key body>';
GRANT ROLE <deploy role> TO USER <deploy user>;
```

Those are all of its grants: no privilege on any client table, nor on the sample tables below. The
application reads the tables with grants of its own.

### The deploy keys

In the same `.devdata/snowflake.env`, beside `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_WAREHOUSE` and
`SNOWFLAKE_ACCOUNT_PURPOSE` (shared with the read test):

- `SNOWFLAKE_DEPLOY_USER`: the deploy user
- `SNOWFLAKE_DEPLOY_PRIVATE_KEY_FILE`: its private key file, absolute or relative to `.devdata/`
- `SNOWFLAKE_DEPLOY_ROLE`: the deploy role
- `SNOWFLAKE_LIVE_DATABASE`: the database of the sample tables, a plain identifier
- `SNOWFLAKE_CORTEX_MODEL`: a Cortex model of the account's region, for example `mistral-large2`

The test never uses `SNOWFLAKE_USER`, `SNOWFLAKE_PRIVATE_KEY_FILE` or `SNOWFLAKE_ROLE`, and the read
test never uses the deploy keys. The contents of `.devdata/` (the env file and every key file) are
never printed, committed or pasted to an agent, an issue or a chat.

### Sample tables

As a role that may create them (not the deploy role):

```sql
CREATE SCHEMA IF NOT EXISTS <database>.PUBLIC;
CREATE SCHEMA IF NOT EXISTS <database>.SALES;
CREATE TABLE <database>.PUBLIC.CUSTOMERS (ID NUMBER, NAME TEXT, REGION TEXT);
INSERT INTO <database>.PUBLIC.CUSTOMERS VALUES
  (1, 'Sample Customer 1', 'North'), (2, 'Sample Customer 2', 'South'),
  (3, 'Sample Customer 3', 'East'), (4, 'Sample Customer 4', 'West'),
  (5, 'Sample Customer 5', 'North'), (6, 'Sample Customer 6', 'South');
CREATE TABLE <database>.PUBLIC.ORDERS (ID NUMBER, CUSTOMER_ID NUMBER, AMOUNT FLOAT, ORDERED_ON DATE);
INSERT INTO <database>.PUBLIC.ORDERS VALUES
  (1, 1, 25.5, '2026-01-11'), (2, 2, 50.5, '2026-01-12'), (3, 3, 75.5, '2026-01-13'),
  (4, 4, 100.5, '2026-01-14'), (5, 5, 125.5, '2026-02-15'), (6, 6, 150.5, '2026-02-16'),
  (7, 1, 175.5, '2026-02-17'), (8, 2, 200.5, '2026-02-18'), (9, 3, 225.5, '2026-03-19'),
  (10, 4, 250.5, '2026-03-20'), (11, 5, 275.5, '2026-03-21'), (12, 6, 300.5, '2026-03-22');
CREATE TABLE <database>.SALES.INVOICES (ID NUMBER, ORDER_ID NUMBER, TOTAL FLOAT);
INSERT INTO <database>.SALES.INVOICES VALUES
  (1, 1, 27.5), (2, 2, 52.5), (3, 3, 77.5), (4, 4, 102.5), (5, 5, 127.5), (6, 6, 152.5),
  (7, 7, 177.5), (8, 8, 202.5), (9, 9, 227.5), (10, 10, 252.5), (11, 11, 277.5), (12, 12, 302.5);
```

### The live run

```powershell
cd backend; uv run pytest tests/adapters/native/test_snowflake_native_live.py -m live -q -s
```

`-s` is needed: the run prints the application's name and then waits for the hand grants. Without
the env file the test is skipped with "live validation waiting on owner input: no Snowflake
account", and with the same text and the key's name for a missing or empty key. With the file but
without `-m live` it is skipped with "live validation is opt-in: run with -m live". Every refusal
of the file's values (as for the read test, plus the account name and each name's pattern) fails
before any host is contacted and names only the key or rule.

In order:

1. the #358 gate, before any host is contacted
2. capabilities: the pinned CLI and the metadata read
3. the Snowpark column-name table, before any deploy
4. the sample release rendered for the live database and Cortex model, validated, built, planned,
   applied once (never retried) and observed. The package and application names are new for every
   run (`SHIFDE_LIVE_<random>_PKG` and `_APP`) and are printed after apply
5. verify (read only) every 15 s, for up to 20 minutes, until `native-version`, `runtime-health` and
   `app-deep-health` pass: run the hand grants now. A failure names the checks, never their reasons
6. the Snowpark version the deep health procedure resolved to, printed beside the pin
7. a second release as patch 1 of the same package and application, applied once and verified

### Hand grants

Run them as an administrator as soon as the test prints the application's name and "run the Hand
grants" (S6-F: the app declares no references, so every read is granted by hand):

```sql
GRANT USAGE ON WAREHOUSE <warehouse> TO APPLICATION <application>;
GRANT DATABASE ROLE SNOWFLAKE.CORTEX_USER TO APPLICATION <application>;
GRANT USAGE ON DATABASE <database> TO APPLICATION <application>;
GRANT USAGE ON SCHEMA <database>.PUBLIC TO APPLICATION <application>;
GRANT USAGE ON SCHEMA <database>.SALES TO APPLICATION <application>;
GRANT SELECT ON TABLE <database>.PUBLIC.CUSTOMERS TO APPLICATION <application>;
GRANT SELECT ON TABLE <database>.PUBLIC.ORDERS TO APPLICATION <application>;
GRANT SELECT ON TABLE <database>.SALES.INVOICES TO APPLICATION <application>;
```

Exactly these tables. To see what is still missing, call
`CALL <application>.APP_PUBLIC.DEEP_HEALTH();` as the deploy role: each failed check names the
warehouse, the Cortex model or the table.

### Snowpark checks

- **The column-name table.** The generated app's tests use a local stand-in for Snowpark (B-4000)
  that follows a table frozen from Snowpark 1.55.0 (`COLUMN_NAMES` in
  `backend/tests/_support/snowpark_types.py`). Step 3 sends one anonymous Python procedure (nothing
  is created in the account) with `snowflake-snowpark-python` at the version `ui/environment.yml`
  pins, and fails unless the version and every field name equal the table and the app's
  `column_name` gives each name back.
- **The version comparison.** `setup.sql` names the package without a version, so step 6 reads
  what `APP_PUBLIC.DEEP_HEALTH` resolved to (`installed_packages` of `DESCRIBE PROCEDURE`). A
  different version, or "not reported", fails the test at the end, after patch 1 ran.
- **On a mismatch or "not reported":** record it, and deploy no live app until it is settled (the
  pin, the table or the procedure's packages change, or the owner or lead accepts it).
- **The table is compared again on any change of the Snowpark pin.** A default-suite test fails
  until `SNOWPARK_VERSION` beside the table is the new pin; change it only with a new comparison.

### Unknown outcomes

As in "Writes, retries and unknown outcomes" of `databricks-live-validation.md`: when apply raises,
its outcome is unknown. Do not run the test again to "fix" it. Take the package and application
names from the failure and look at the account: the query history (statements carry the query tag
`shifde-native-app`), `SHOW APPLICATION PACKAGES LIKE 'SHIFDE_LIVE_%'`, `SHOW APPLICATIONS LIKE
'SHIFDE_LIVE_%'` and `SHOW VERSIONS IN APPLICATION PACKAGE <package>`. Record what is there, clean
up, and only then start a new run, which uses new names.

### Cleanup

Manual; nothing cleans up automatically, also not after a failed run. The application goes first,
then its package:

```sql
DROP APPLICATION <application>;
DROP APPLICATION PACKAGE <package>;
DROP TABLE <database>.PUBLIC.CUSTOMERS;
DROP TABLE <database>.PUBLIC.ORDERS;
DROP TABLE <database>.SALES.INVOICES;
ALTER USER <deploy user> UNSET RSA_PUBLIC_KEY;
```

Dropping the application drops its usage table and its grants. Delete the deploy key file when no
more runs are planned.

### Live-only risks

The fakes cannot prove these (design 9.7):

- setup script and `IMPORTS` semantics of the real account
- the stage layout after the real `snow stage copy`
- Cortex reply shapes (the options form of `COMPLETE`)
- the path of the OpenAI-compatible Cortex endpoint (not driven by this test)
- Streamlit behaviour inside the app (the screens are not driven by this test)
- whether `DESCRIBE PROCEDURE` shows `installed_packages` for an application's procedure
- the privileges an anonymous procedure needs, and whether the account allows the pinned package

### What to record for the Native App

For each run: the date, the commit (`git rev-parse HEAD`), pass or fail per step, and the two
Snowpark versions. Never a value of the env file, the account, a key or a name in the account other
than the run's own `SHIFDE_LIVE_` names. A passing run changes no status: the integration stays
`experimental`.

## Status

The read adapter and the Native App integration remain `experimental` whatever the outcome of these
runs. They are not validated by this runbook; promoting either is a separate decision for the owner.
