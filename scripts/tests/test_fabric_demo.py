"""scripts/fabric_demo.py without the backend environment or a cluster: only `--fake` runs, and
without it the script stops with exit 2 and the fake-only message."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
import unittest
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "fabric_demo", Path(__file__).resolve().parents[1] / "fabric_demo.py"
)
assert _SPEC and _SPEC.loader
demo = importlib.util.module_from_spec(_SPEC)
sys.modules["fabric_demo"] = demo
_SPEC.loader.exec_module(demo)


class WithoutFake(unittest.TestCase):
    def test_exits_2_with_the_fake_only_message(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = demo.main([])
        self.assertEqual(code, 2)
        self.assertEqual(out.getvalue().strip(), f"FABRIC DEMO NOT RUN: {demo.FAKE_ONLY}")
        self.assertIn("only the fake platform is supported", out.getvalue())
        self.assertIn("no Fabric workspace or Entra tenant is configured", out.getvalue())


if __name__ == "__main__":
    unittest.main()
