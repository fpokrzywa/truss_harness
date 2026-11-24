# SP5-5 appgen render: isolation review 1

Recorded by the lead from the isolation-reviewer's report (the reviewer is read-only), 2026-11-23.

Verdict: APPROVED. No migration, table, route, service, worker path or cache added. `render()` is pure; its only organization data is its inputs. The generated app runs as the deploying organization's own Databricks app service principal and holds no SETH address, token or organization id (tests/appgen/test_render.py checks). Design 6.4's missing per-user check in the app is confined to one client: the app identity's grants are the only data boundary inside the client. Import-linter contract keeps flask/databricks out of shifde.

Carried to SP5-6 (#259, issue comment): gather Resolved under the requester's RLS context, check pinned spec_hash against the same organization's manifest (409 release_source_changed), keep the archive within that organization's artifacts, add the design 9.5 test (B's executor leases nothing of A's generated-app plan), and state the app-identity boundary in the deploy runbook.
