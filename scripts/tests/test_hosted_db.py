"""Tests for scripts/hosted_db.py (run: python -m unittest discover -s scripts/tests).

Only the parts that need no PostgreSQL: the instance secrets and the backend subprocess environment.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location("hosted_db", ROOT / "scripts" / "hosted_db.py")
assert _SPEC and _SPEC.loader
hosted_db = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = hosted_db
_SPEC.loader.exec_module(hosted_db)

VENV_BIN = ROOT / "backend" / ".venv" / ("Scripts" if sys.platform == "win32" else "bin")
VENV_PYTHON = VENV_BIN / ("python.exe" if sys.platform == "win32" else "python")
APP_URL = "postgresql+psycopg://shifde_app:x@127.0.0.1:5432/x"
OWNER_URL = "postgresql+psycopg://shifde_owner:x@127.0.0.1:5432/x"
STRAY = {
    "SHIFDE_LOG_FORMAT": "console",
    "SHIFDE_ENV": "stray-env",
    "SHIFDE_PUBLIC_BASE_URL": "http://stray.example.com",
    "SHIFDE_METRICS_TOKEN": "stray-token",
}
CHECK = (
    "import sys\n"
    "from shifde.core.settings import load_settings\n"
    "sys.exit(0 if load_settings().env == 'production' else 1)\n"
)


def build_env() -> dict[str, str]:
    base = {k: v for k, v in hosted_db.os.environ.items() if not k.startswith("SHIFDE_")}
    base.update(STRAY)
    return hosted_db.subprocess_env(base, APP_URL, OWNER_URL, hosted_db.instance_secrets({}))


class SubprocessEnvTests(unittest.TestCase):
    def test_settings_validate(self) -> None:
        if not VENV_PYTHON.is_file():
            self.skipTest(f"backend venv has no Python at {VENV_PYTHON} (run uv sync in backend)")
        r = subprocess.run(
            [str(VENV_PYTHON), "-c", CHECK],
            cwd=ROOT / "backend",
            env=build_env(),
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_stray_values_dropped(self) -> None:
        env = build_env()
        for key, value in STRAY.items():
            self.assertNotEqual(env.get(key), value, key)
        self.assertEqual(env["SHIFDE_ENV"], "production")
        self.assertTrue(env["SHIFDE_METRICS_TOKEN"])
        self.assertEqual(env["SHIFDE_PUBLIC_BASE_URL"], hosted_db.SUBPROCESS_PUBLIC_BASE_URL)


class InstanceSecretsTests(unittest.TestCase):
    def test_saved_token_is_reused(self) -> None:
        saved = {"SHIFDE_METRICS_TOKEN": "kept-token", "SHIFDE_API_KEY_PEPPER": "kept-pepper"}
        got = hosted_db.instance_secrets(saved)
        self.assertEqual(got["SHIFDE_METRICS_TOKEN"], "kept-token")
        self.assertEqual(got["SHIFDE_API_KEY_PEPPER"], "kept-pepper")

    def test_missing_token_is_generated(self) -> None:
        first = hosted_db.instance_secrets({})
        second = hosted_db.instance_secrets({"SHIFDE_METRICS_TOKEN": ""})
        self.assertTrue(first["SHIFDE_METRICS_TOKEN"])
        self.assertTrue(second["SHIFDE_METRICS_TOKEN"])
        self.assertNotEqual(first["SHIFDE_METRICS_TOKEN"], second["SHIFDE_METRICS_TOKEN"])
        self.assertTrue(first["SHIFDE_SECRET_KEYS"].startswith("hosted1:"))


if __name__ == "__main__":
    unittest.main()
