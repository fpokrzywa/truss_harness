#!/usr/bin/env bash
# The repo's checks. Exit 0 only when the branch is fit to review.
#
# The gate runs this script from the main checkout with the job's worktree as
# the working directory. Keep it fast, deterministic and non-interactive: no
# watch modes, no prompts, nothing that rewrites files.
#
# Backend tests: by default this runs a fixed core set (row-level security
# schema, schema comparison against SETH, startup role check, cross-client
# isolation) plus every backend test file the branch adds or changes against
# origin/<base_branch>. FACTORY_FULL_PYTEST=1 runs the whole suite (-n 8).
# The full suite stays a human step before merging (see CLAUDE.md).
#
# Needs the local PostgreSQL cluster running (python scripts/dev_db.py start).
set -euo pipefail

MAIN="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WT="$(pwd)"
BASE="origin/$(python -c "import json,sys; print(json.load(open(sys.argv[1]))['base_branch'])" "$MAIN/factory/config.json")"
# Paths relative to backend/.
CORE_TESTS=(
  tests/schema/test_rls_schema.py
  tests/schema/test_schema_vs_seth.py
  tests/core/test_startup_role_check.py
  tests/core/test_cross_client.py
  tests/tenancy/test_cross_client.py
  tests/authz/test_cross_client.py
  tests/audit/test_cross_client.py
  tests/connections/test_cross_client.py
  tests/runtime/test_cross_client.py
  tests/build/test_cross_client.py
  tests/governance/test_cross_client.py
  tests/admin/test_cross_client.py
  tests/app/test_cross_client.py
)

step() { echo; echo "== $*"; }

step "dev PostgreSQL reachable"
python "$MAIN/scripts/dev_db.py" status >/dev/null || {
  echo "local PostgreSQL (127.0.0.1:55450) is down: run python scripts/dev_db.py start" >&2
  exit 1
}
# This checkout's own database and .devdata/dev.env (gitignored): shifde in the main checkout,
# shifde_<worktree> in a worktree, never the shared dev database from a job worktree.
python "$MAIN/scripts/dev_db.py" env >/dev/null

step "secret scan"
python -I scripts/validate/scan-secrets.py >/dev/null

cd "$WT/backend"
# Fresh worktree venvs: uv's default hardlinks from its cache can fail on this machine.
export UV_LINK_MODE=copy
step "backend: uv sync"
uv sync --frozen --quiet
step "backend: ruff format --check"
uv run --frozen ruff format --check .
step "backend: ruff check"
uv run --frozen ruff check .
step "backend: mypy"
uv run --frozen mypy src
step "backend: lint-imports"
uv run --frozen lint-imports

if [ "${FACTORY_FULL_PYTEST:-}" = "1" ]; then
  step "backend: pytest (full suite)"
  uv run --frozen pytest -q -n 8
else
  changed=()
  if git rev-parse -q --verify "$BASE" >/dev/null; then
    while IFS= read -r f; do
      changed+=("${f#backend/}")
    done < <(git -C "$WT" diff --name-only --diff-filter=d "$BASE"...HEAD -- 'backend/tests/*test_*.py')
  fi
  tests=($(printf '%s\n' "${CORE_TESTS[@]}" "${changed[@]+"${changed[@]}"}" | sort -u))
  step "backend: pytest (core + changed: ${#tests[@]} files)"
  uv run --frozen pytest -q -n 8 "${tests[@]}"
fi

cd "$WT/frontend"
step "frontend: pnpm install"
pnpm install --frozen-lockfile --prefer-offline --reporter=silent
step "frontend: lint"
pnpm lint
step "frontend: typecheck"
pnpm typecheck
step "frontend: test"
pnpm test
step "frontend: build"
pnpm build

echo
echo "checks.sh: all checks passed"
