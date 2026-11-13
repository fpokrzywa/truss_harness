# Decisions made by the builder

Calls the design left open, made under charter section 3. Newest last.

| Id | Date | Decision | Reason |
|---|---|---|---|
| B-001 | 2026-11-13 | Tests live in `backend/tests/<module>/`, mirroring `backend/src/shifde/<module>/`; shared helpers in `backend/tests/_support/`; schema tests in `backend/tests/schema/`. Run one module with `uv run pytest tests/<module>`. | Keeps the installed package free of tests while staying "beside the module" one directory over; design §8 leaves layout to the builder. |
| B-002 | 2026-11-13 | Local PostgreSQL 17 cluster on port 55450, data in `.devdata/pg/`, managed by `python scripts/dev_db.py init|start|stop|status`. | Must not collide with SETH's 55440 (design §8). A Python script works from both PowerShell and bash. |
| B-003 | 2026-11-13 | Roles keep SETH's names: `shifde_owner` (owns schema, runs migrations) and `shifde_app` (NOSUPERUSER, NOBYPASSRLS, owns nothing). | Ported SQL and policies refer to these names; renaming adds churn for no gain. |
| B-004 | 2026-11-13 | One Alembic `versions/` directory; root revision `base_0001` (branch label `base`); each module's revisions carry `branch_labels=("<module>",)` and `depends_on` the modules whose tables they reference. Upgrades use `alembic upgrade heads`. | Lets modules be built in parallel without revision collisions (design §5.4). |
| B-005 | 2026-11-13 | Schema comparison: `scripts/make_seth_schema_snapshot.py` migrates a scratch database with SETH's own migrations at `3ba0e06` (from `git archive`, SETH untouched) and writes `backend/tests/schema/seth_3ba0e06.json`; `tests/schema/test_schema_vs_seth.py` compares every ported table against it, allowing only entries listed with a reason in `backend/tests/schema/deliberate_changes.yaml`. | An automated, repeatable comparison that does not depend on SETH's dev database. |
| B-006 | 2026-11-13 | Parallel tests with pytest-xdist: migrate a template database once per run, then one `CREATE DATABASE ... TEMPLATE` per worker. | Design §6 asks for one database per worker and a 20-minute full suite. |
| B-007 | 2026-11-13 | The usage report attributes sub-projects through `docs/build/units.json` (unit → sub-project); units not listed count as `sp1`, the lead as `lead`. | Agent descriptions carry the unit, not the sub-project. |
