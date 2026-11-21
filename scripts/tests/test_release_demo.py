"""scripts/release_demo.py without the backend environment or a cluster: only `--fake` runs, and
without it the script stops with exit 2 and the fake-only message."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
import unittest
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "release_demo", Path(__file__).resolve().parents[1] / "release_demo.py"
)
assert _SPEC and _SPEC.loader
demo = importlib.util.module_from_spec(_SPEC)
sys.modules["release_demo"] = demo
_SPEC.loader.exec_module(demo)


class WithoutFake(unittest.TestCase):
    def test_exits_2_with_the_fake_only_message(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = demo.main([])
        self.assertEqual(code, 2)
        self.assertEqual(out.getvalue().strip(), f"RELEASE DEMO NOT RUN: {demo.FAKE_ONLY}")
        self.assertIn("only the fake platform and the fake Entra issuer", out.getvalue())
        self.assertIn("no Databricks workspace or Entra tenant is configured", out.getvalue())


if __name__ == "__main__":
    unittest.main()
