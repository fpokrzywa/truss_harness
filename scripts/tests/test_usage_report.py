"""Tests for scripts/usage_report.py (run: python -m unittest discover -s scripts/tests)."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "usage_report", Path(__file__).resolve().parents[1] / "usage_report.py"
)
assert _SPEC and _SPEC.loader
ur = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = ur
_SPEC.loader.exec_module(ur)

PREFIX = "proj-x"


def _line(rid: str, model: str, inp: int, out: int, w1h: int = 0, read: int = 0) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "requestId": rid,
            "timestamp": "2026-11-13T10:00:00Z",
            "message": {
                "model": model,
                "usage": {
                    "input_tokens": inp,
                    "output_tokens": out,
                    "cache_creation_input_tokens": w1h,
                    "cache_read_input_tokens": read,
                    "cache_creation": {
                        "ephemeral_1h_input_tokens": w1h,
                        "ephemeral_5m_input_tokens": 0,
                    },
                },
            },
        }
    )


class UsageReportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.projects = root / "projects"
        self.repo = root / "repo"
        (self.repo / "docs" / "build").mkdir(parents=True)
        (self.repo / "docs" / "build" / "prices.json").write_text(
            json.dumps(
                {
                    "m-opus": {
                        "input": 4.0,
                        "output": 20.0,
                        "cache_write_5m": 5.0,
                        "cache_write_1h": 8.0,
                        "cache_read": 0.2,
                    },
                    "m-sonnet": {
                        "input": 2.0,
                        "output": 10.0,
                        "cache_write_5m": 2.5,
                        "cache_write_1h": 4.0,
                        "cache_read": 0.1,
                    },
                }
            )
        )
        sess = self.projects / PREFIX
        sub = sess / "sess1" / "subagents"
        sub.mkdir(parents=True)
        # Lead session: req-1 appears on two lines (counted once), req-2 distinct.
        (sess / "sess1.jsonl").write_text(
            "\n".join(
                [
                    _line("req-1", "m-opus", 1000, 100),
                    _line("req-1", "m-opus", 1000, 100),
                    _line("req-2", "m-opus", 0, 0, w1h=1_000_000),
                    json.dumps({"type": "user", "message": {}}),
                ]
            )
        )
        (sub / "agent-abc.jsonl").write_text(_line("req-3", "m-sonnet", 0, 1_000_000))
        (sub / "agent-abc.meta.json").write_text(
            json.dumps({"agentType": "module-builder", "description": "[core] build"})
        )
        # A worktree folder with the same prefix is included.
        wt = self.projects / (PREFIX + "--worktrees-core")
        wt.mkdir(parents=True)
        (wt / "sess2.jsonl").write_text(_line("req-4", "m-opus", 10, 10))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_request_counted_once_and_grouped(self) -> None:
        r = ur.build_report(self.projects, PREFIX, self.repo)
        self.assertEqual(r["totals"]["requests"], 4)
        by_agent = {g["agent_type"]: g for g in r["by_agent_type"]}
        self.assertEqual(by_agent["lead"]["input"], 1010)
        self.assertEqual(by_agent["lead"]["cache_write_1h"], 1_000_000)
        self.assertEqual(by_agent["module-builder"]["output"], 1_000_000)
        by_unit = {g["unit"]: g for g in r["by_unit"]}
        self.assertIn("core", by_unit)
        self.assertIn("lead", by_unit)

    def test_role_tag_overrides_general_purpose(self) -> None:
        sub = self.projects / PREFIX / "sess1" / "subagents"
        (sub / "agent-def.jsonl").write_text(_line("req-9", "m-sonnet", 1, 1))
        (sub / "agent-def.meta.json").write_text(
            json.dumps({"agentType": "general-purpose", "description": "[core] verify (verifier)"})
        )
        r = ur.build_report(self.projects, PREFIX, self.repo)
        types = {g["agent_type"] for g in r["by_agent_type"]}
        self.assertIn("verifier", types)
        self.assertNotIn("general-purpose", types)

    def test_cost_from_prices(self) -> None:
        r = ur.build_report(self.projects, PREFIX, self.repo)
        by_model = {g["model"]: g for g in r["by_model"]}
        self.assertAlmostEqual(by_model["m-sonnet"]["est_usd"], 10.0, places=4)
        # opus: 1010 in *4 + 110 out *20 + 1M 1h writes *8, per million
        self.assertAlmostEqual(
            by_model["m-opus"]["est_usd"], (1010 * 4 + 110 * 20 + 1_000_000 * 8) / 1e6, places=4
        )

    def test_rounds_per_unit(self) -> None:
        r = ur.build_report(self.projects, PREFIX, self.repo)
        self.assertEqual(r["rounds_per_unit"], [
            {"unit": "core", "build": 1, "verify": 0, "fix": 0, "review": 0, "inventory": 0,
             "other": 0}
        ])

    def test_main_writes_outputs(self) -> None:
        out = Path(self.tmp.name) / "out"
        rc = ur.main(
            ["--projects-dir", str(self.projects), "--prefix", PREFIX, "--repo", str(self.repo),
             "--out-dir", str(out)]
        )
        self.assertEqual(rc, 0)
        self.assertTrue((out / "usage.json").exists())
        self.assertIn("estimates at list price", (out / "usage.html").read_text())


if __name__ == "__main__":
    unittest.main()
