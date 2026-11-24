# 140 (#293, PR #303) lead isolation review 3 — 2026-11-23, head d4271ea

APPROVED. Read `git diff e66a210..d4271ea`; test_docker_runtime.py + test_docker.py 169 passed.

1. Daemon version: `runtime.py:207-211` reads `{{.Server.Version}}`, refuses < (28, 0) and unreadable versions (`docker_engine_too_old`), before every command (daemon_id, build, image_labels, compose_up, compose_ps, and http_get_local via compose_ps before and after). No other `_run` call in the package. Test `test_docker_runtime.py:211` covers all five entry points with 27.x, 20.10, garbage, empty. CLAUDE.md and adapter capability reason say Engine 28+.
2. Labels: Dockerfile LABEL keys exactly `org.shifde.release_digest` (`source.py:194-196`), legacy `LABEL key value` refused (`:145-147`), compose labels only `org.shifde.target_id` (`:282-283`). Five refusal tests.
3. Health race: `runtime.py:318-321` re-runs compose_ps after the response; residual window (port leaves and returns within one ≤5 s request) accepted.
4. Bookkeeping: engine 28.0 floor recorded in DECISIONS at merge; SP8-9 must accept only generator-produced archives (bundle RUN steps constrained) — carried to SP8-9.
