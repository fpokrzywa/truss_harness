APPROVED

Round 2 isolation review of `factory/073-sp4-4-cli-backup` (PR #176), by `isolation-reviewer`; saved by the lead (2026-11-22). 0 blocking findings. Round-2 changes and `KeyRing.key_ids` expose no key material, password or client data.

1. `cli/backup.py:126-141`: the child process env holds only PATH (plus SYSTEMROOT/TEMP/TMP on Windows), PGPASSWORD and PG* values built from the URL. No `SHIFDE_*` variable passes, nor an inherited PGPASSFILE, PGSERVICE or PGSSLMODE. Tests: `test_backup.py:187,250`. Side effect: on Windows, libpq cannot find `%APPDATA%\postgresql\root.crt`, so `verify-full` without `sslrootcert` fails. It fails closed.
2. `backup.py:155-170`: the password becomes `***` in error messages, and a connection failure reports only the exception's class (:389). The password is on no command line. Advice: set `pretty_exceptions_show_locals=False` explicitly in `cli/main.py:61`.
3. `backup.py:72-78, 113-122`: the only query parameters allowed are ssl*. Minor: pg_dump reads `-d <db>` (:152) as a connection string when the name contains `=` or `://`. The name comes from the operator's own configuration. To harden, refuse those names in `_parse_url`.
4. `backup.py:369-386`: a partial dump is removed on pg_dump failure. Minor: a failure after pg_dump leaves a complete dump with no manifest, in a 0700 directory on POSIX. On Windows, a file still locked by the child can stay after a kill. Neither crosses clients.
5. `core/crypto.py:72-75`: `key_ids` returns only the key ids, never key material. The manifest stores only the ids (`backup.py:404`, test `test_backup.py:287`).
6. The manifest holds whole-database row counts per table and catalog metadata: no rows, no organization ids. It runs as the schema owner, only from the admin command line.
