"""Tests for scripts/gates.py (pure parts only; running gates needs a checkout and a database)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gates  # noqa: E402


class PlanTests(unittest.TestCase):
    def test_full_plan_has_every_gate_in_order(self) -> None:
        names = [g.name for g in gates.plan(tests=[], workers="8", frontend=True)]
        self.assertEqual(names[0], "scan-secrets")
        for expected in (
            "ruff format --check",
            "ruff check",
            "mypy src",
            "lint-imports",
            "pytest (full suite)",
            "scripts unittest",
            "pnpm lint",
            "pnpm typecheck",
            "pnpm test",
            "pnpm build",
        ):
            self.assertIn(expected, names)
        self.assertLess(names.index("lint-imports"), names.index("pytest (full suite)"))

    def test_test_targets_and_workers_reach_pytest(self) -> None:
        plan = gates.plan(tests=["tests/core"], workers="4", frontend=False)
        pytest_gate = next(g for g in plan if g.name.startswith("pytest"))
        self.assertEqual(pytest_gate.argv[-3:], ["-n", "4", "tests/core"][-3:])
        self.assertNotIn("pnpm build", [g.name for g in plan])

    def test_backend_gates_use_copy_link_mode(self) -> None:
        for gate in gates.plan(tests=[], workers="8", frontend=False):
            if gate.cwd == "backend":
                self.assertEqual(gate.env.get("UV_LINK_MODE"), "copy")


class OutputTests(unittest.TestCase):
    def test_last_line_skips_blank_lines(self) -> None:
        self.assertEqual(gates.last_line("a\n  b  \n\n\n"), "b")
        self.assertEqual(gates.last_line(""), "")

    def test_tail_and_safe_name(self) -> None:
        self.assertEqual(gates.tail("1\n2\n3", 2), "2\n3")
        self.assertEqual(gates.safe_name("pytest tests/core"), "pytest_tests_core")


if __name__ == "__main__":
    unittest.main()
