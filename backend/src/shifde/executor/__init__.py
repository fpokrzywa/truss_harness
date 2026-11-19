"""The `shifde-executor` process (design sections 2 and 5): it leases its organization's delivery
jobs from the executor gateway and runs them through the native adapter, against a Databricks
workspace or, with `--fake-platform`, the in-memory fake.

It never reaches the database: it imports the stdlib, httpx, pydantic and `shifde.adapters.native`
only (import-linter contract "The executor never reaches the database").
"""
