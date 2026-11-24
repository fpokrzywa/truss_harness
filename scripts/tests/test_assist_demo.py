"""scripts/assist_demo.py without the backend environment or a cluster: only `--fake` runs, and
without it the script stops with exit 2 and the fake-only message; `--through` takes 1..7 only."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
import unittest
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "assist_demo", Path(__file__).resolve().parents[1] / "assist_demo.py"
)
assert _SPEC and _SPEC.loader
demo = importlib.util.module_from_spec(_SPEC)
sys.modules["assist_demo"] = demo
_SPEC.loader.exec_module(demo)


class WithoutFake(unittest.TestCase):
    def test_exits_2_with_the_fake_only_message(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = demo.main([])
        self.assertEqual(code, 2)
        self.assertEqual(out.getvalue().strip(), f"ASSIST DEMO NOT RUN: {demo.FAKE_ONLY}")
        self.assertIn("run with --fake", out.getvalue())

    def test_through_takes_steps_1_to_7_only(self) -> None:
        for value in ("0", "8", "x"):
            err = io.StringIO()
            with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as refused:
                demo.main(["--fake", "--through", value])
            self.assertEqual(refused.exception.code, 2, value)
            self.assertIn("--through", err.getvalue())

    def test_through_1_to_7_without_fake_still_needs_fake(self) -> None:
        for value in ("1", "7"):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = demo.main(["--through", value])
            self.assertEqual(code, 2)
            self.assertIn("run with --fake", out.getvalue())


if __name__ == "__main__":
    unittest.main()
