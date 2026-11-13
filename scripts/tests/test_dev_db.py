"""Tests for scripts/dev_db.py (run: python -m unittest discover -s scripts/tests).

Only the parts that need no PostgreSQL: checkout/database naming and the dev.env rewrite.
"""

from __future__ import annotations

import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "dev_db", Path(__file__).resolve().parents[1] / "dev_db.py"
)
assert _SPEC and _SPEC.loader
dev_db = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(dev_db)


class NamingTests(unittest.TestCase):
    def test_main_checkout_uses_plain_name(self) -> None:
        main = Path("/repo")
        self.assertEqual(dev_db.database_name(main, main), "shifde")

    def test_worktree_gets_its_own_sanitised_name(self) -> None:
        main = Path("/repo")
        self.assertEqual(
            dev_db.database_name(Path("/repo/.worktrees/Front-End.2"), main), "shifde_front_end_2"
        )

    def test_main_root_of_a_worktree_is_the_main_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            main = Path(tmp) / "main"
            main.mkdir()

            def git(*args: str, cwd: Path = main) -> None:
                subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)

            git("init", "-q")
            git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q",
                "--allow-empty", "-m", "init")
            git("worktree", "add", "-q", str(main / ".worktrees" / "wt"))
            wt = main / ".worktrees" / "wt"
            self.assertEqual(dev_db.checkout_root(wt), wt.resolve())
            self.assertEqual(dev_db.main_root(wt), main.resolve())
            self.assertEqual(dev_db.main_root(main), main.resolve())


class DevEnvTests(unittest.TestCase):
    def test_rewrite_replaces_managed_keys_and_keeps_others(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            main, checkout = Path(tmp) / "main", Path(tmp) / "wt"
            (main / ".devdata").mkdir(parents=True)
            (main / ".devdata" / "cluster.env").write_text(
                "SUPERUSER_PASSWORD=s1\nOWNER_PASSWORD=o1\nAPP_PASSWORD=a1\n", encoding="utf-8"
            )
            (checkout / ".devdata").mkdir(parents=True)
            env_file = checkout / ".devdata" / "dev.env"
            env_file.write_text(
                "SHIFDE_DATABASE_URL=old\nSHIFDE_API_KEY_PEPPER=keep-me\n", encoding="utf-8"
            )
            cluster = dev_db.Cluster(main)
            cluster.write_dev_env(checkout, "shifde_wt")
            values = dev_db._read_env(env_file)
            port = dev_db.PORT
            self.assertEqual(
                values["SHIFDE_DATABASE_URL"],
                f"postgresql+psycopg://shifde_app:a1@127.0.0.1:{port}/shifde_wt",
            )
            self.assertEqual(
                values["SHIFDE_MIGRATION_DATABASE_URL"],
                f"postgresql+psycopg://shifde_owner:o1@127.0.0.1:{port}/shifde_wt",
            )
            self.assertEqual(
                values["SHIFDE_TEST_ADMIN_DATABASE_URL"],
                f"postgresql+psycopg://postgres:s1@127.0.0.1:{port}/postgres",
            )
            self.assertIn("SHIFDE_TEST_DATABASE_URL", values)
            self.assertIn("SHIFDE_TEST_MIGRATION_DATABASE_URL", values)
            self.assertEqual(values["SHIFDE_API_KEY_PEPPER"], "keep-me")
            # Rewriting again is stable (no duplicated header or keys).
            first = env_file.read_text(encoding="utf-8")
            cluster.write_dev_env(checkout, "shifde_wt")
            self.assertEqual(env_file.read_text(encoding="utf-8"), first)


if __name__ == "__main__":
    unittest.main()


class EnsureRolesTests(unittest.TestCase):
    """`ensure_roles` resets shifde_app's memberships (isolation review 1, item 13)."""

    def _sql(self) -> str:
        sent: list[str] = []
        cluster = dev_db.Cluster.__new__(dev_db.Cluster)
        cluster.passwords = lambda: {  # type: ignore[method-assign]
            "SUPERUSER_PASSWORD": "s1",
            "OWNER_PASSWORD": "o1",
            "APP_PASSWORD": "a1",
        }
        cluster.psql = lambda sql, *a, **k: sent.append(sql) or ""  # type: ignore[method-assign]
        cluster.ensure_roles()
        return "\n".join(sent)

    def test_revokes_every_membership_of_the_app_role(self) -> None:
        sql = self._sql()
        self.assertIn("FROM pg_auth_members", sql)
        self.assertIn("m.member = 'shifde_app'::regrole", sql)
        self.assertIn("REVOKE %I FROM %I GRANTED BY %I", sql)

    def test_app_role_attributes_stay_unprivileged(self) -> None:
        sql = self._sql()
        self.assertIn(
            "ALTER ROLE shifde_app LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE", sql
        )
        self.assertIn("NOREPLICATION", sql)
