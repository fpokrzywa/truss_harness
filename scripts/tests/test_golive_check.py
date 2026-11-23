"""scripts/golive_check.py without the backend environment or a cluster: the mode flag, the
register verdict, the edge proxy against a stub upstream, the env-file builder and the child
environment."""

from __future__ import annotations

import contextlib
import http.client
import http.server
import importlib.util
import io
import socket
import sys
import threading
import unittest
from pathlib import Path
from typing import Any

_SPEC = importlib.util.spec_from_file_location(
    "golive_check", Path(__file__).resolve().parents[1] / "golive_check.py"
)
assert _SPEC and _SPEC.loader
golive = importlib.util.module_from_spec(_SPEC)
sys.modules["golive_check"] = golive
_SPEC.loader.exec_module(golive)

APP_URL = "postgresql+psycopg://app:pw@127.0.0.1:1/db"
OWNER_URL = "postgresql+psycopg://owner:pw@127.0.0.1:1/db"
CLOSED_URL = "postgresql+psycopg://app:pw@127.0.0.1:2/db"
SECRETS = {
    "SHIFDE_API_KEY_PEPPER": "p" * 48,
    "SHIFDE_SECRET_KEYS": "k1:secret",
    "SHIFDE_METRICS_TOKEN": "t" * 32,
}


class WithoutMode(unittest.TestCase):
    def test_exits_2_with_the_not_run_line(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = golive.main([])
        self.assertEqual(code, 2)
        self.assertEqual(out.getvalue().strip(), "GO-LIVE CHECK NOT RUN: choose --local or --final")


class RegisterVerdict(unittest.TestCase):
    def test_local_passes_closed_accepted_and_pending_rows_and_lists_the_pending_ids(self) -> None:
        rows = [("A", "closed"), ("B", "accepted"), ("C", "pending-owner"), ("D", "pending-owner")]
        self.assertEqual(golive.register_verdict(rows, "local"), ([], ["C", "D"]))

    def test_both_modes_fail_on_an_open_row(self) -> None:
        rows = [("A", "closed"), ("B", "open"), ("Hosting target", "closed")]
        for mode in ("local", "final"):
            failures, _ = golive.register_verdict(rows, mode)
            self.assertEqual(failures, ["open: B"], mode)

    def test_final_fails_on_a_pending_owner_row(self) -> None:
        rows = [("A", "pending-owner"), ("Hosting target", "accepted")]
        failures, pending = golive.register_verdict(rows, "final")
        self.assertEqual(failures, ["pending-owner: A"])
        self.assertEqual(pending, ["A"])

    def test_final_fails_without_a_hosting_target_row(self) -> None:
        failures, _ = golive.register_verdict([("A", "closed")], "final")
        self.assertEqual(failures, ["no closed or accepted Hosting target row"])

    def test_final_fails_with_a_pending_hosting_target_row(self) -> None:
        failures, _ = golive.register_verdict([("Hosting target", "pending-owner")], "final")
        self.assertEqual(
            failures,
            ["pending-owner: Hosting target", "no closed or accepted Hosting target row"],
        )

    def test_final_passes_when_every_row_is_closed_or_accepted(self) -> None:
        rows = [("A", "closed"), ("B", "accepted"), ("Hosting target", "closed")]
        self.assertEqual(golive.register_verdict(rows, "final"), ([], []))
        rows[-1] = ("Hosting target", "accepted")
        self.assertEqual(golive.register_verdict(rows, "final"), ([], []))


class _Upstream(http.server.BaseHTTPRequestHandler):
    seen: list[dict[str, Any]] = []

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        _Upstream.seen.append(
            {"xff": self.headers.get_all("X-Forwarded-For"), "body": body, "path": self.path}
        )
        self.send_response(418)
        self.send_header("X-Upstream", "yes")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"hi")

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - base signature
        pass


class EdgeProxy(unittest.TestCase):
    def test_the_upstream_sees_the_peer_address_and_the_answer_is_passed_through(self) -> None:
        _Upstream.seen = []
        upstream = golive.LoopbackServer(("127.0.0.1", 0), _Upstream)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(upstream.server_close)
        self.addCleanup(upstream.shutdown)
        with golive.EdgeProxy(int(upstream.server_address[1])) as edge:
            conn = http.client.HTTPConnection("127.0.0.1", edge.port, timeout=10)
            try:
                conn.request(
                    "POST",
                    "/api/x?y=1",
                    body=b"{}",
                    headers={"X-Forwarded-For": "203.0.113.7", "Content-Type": "application/json"},
                )
                response = conn.getresponse()
                body = response.read()
            finally:
                conn.close()
        self.assertEqual(response.status, 418)
        self.assertEqual(response.getheader("X-Upstream"), "yes")
        self.assertEqual(body, b"hi")
        self.assertEqual(
            _Upstream.seen, [{"xff": ["127.0.0.1"], "body": b"{}", "path": "/api/x?y=1"}]
        )

    def test_no_other_socket_can_bind_the_edge_port_while_it_is_open(self) -> None:
        # Windows lets a SO_REUSEADDR socket share a port bound with SO_REUSEADDR
        with golive.EdgeProxy(1) as edge, socket.socket() as other:
            other.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            with self.assertRaises(OSError):
                other.bind(("127.0.0.1", edge.port))


class EnvFiles(unittest.TestCase):
    def setUp(self) -> None:
        self.good = golive.good_env(APP_URL, OWNER_URL, SECRETS)

    def test_the_good_file_has_every_production_required_key(self) -> None:
        for key in golive.PRODUCTION_REQUIRED:
            self.assertTrue(self.good.get(key), key)
        self.assertEqual(self.good["SHIFDE_ENV"], "production")
        self.assertEqual(self.good["SHIFDE_TRUSTED_PROXIES"], "127.0.0.1/32")
        self.assertTrue(self.good["SHIFDE_PUBLIC_BASE_URL"].startswith("https://"))
        for key in ("SHIFDE_API_DOCS_ENABLED", "SHIFDE_AUTH_LOCAL_ENABLED"):
            self.assertEqual(self.good[key], "false")
        self.assertEqual(self.good["SHIFDE_TRUST_LOOPBACK_PROXY"], "false")
        self.assertNotIn("SHIFDE_HSTS_MAX_AGE_SECONDS", self.good)  # the default

    def test_each_variant_differs_from_the_good_file_in_its_one_setting(self) -> None:
        variants = golive.preflight_variants(OWNER_URL, CLOSED_URL)
        self.assertEqual(len(variants), 9)
        for name, changes, expected in variants:
            bad = golive.changed(self.good, changes)
            differing = {k for k in self.good.keys() | bad.keys() if self.good.get(k) != bad.get(k)}
            self.assertEqual(differing, set(changes), name)
            self.assertTrue(expected.startswith("refused: "), name)
            # one setting: local login is the switch and its production allowance together
            prefixes = {
                k.removesuffix("_ALLOW_IN_PRODUCTION").removesuffix("_ENABLED") for k in changes
            }
            self.assertEqual(len(prefixes), 1, name)
        removed = [n for n, c, _ in variants if None in c.values()]
        self.assertEqual(
            removed, ["SHIFDE_METRICS_TOKEN removed", "SHIFDE_PUBLIC_BASE_URL removed"]
        )

    def test_the_child_environment_drops_inherited_shifde_keys(self) -> None:
        parent = {
            "PATH": "/bin",
            "SHIFDE_DATABASE_URL": "postgresql://hosted",
            "shifde_env": "production",
            "SHIFDE_PG_BIN_DIR": "/pg/bin",
        }
        env = golive.child_env(parent, {"SHIFDE_ENV": "development"})
        self.assertEqual(
            env, {"PATH": "/bin", "SHIFDE_PG_BIN_DIR": "/pg/bin", "SHIFDE_ENV": "development"}
        )


if __name__ == "__main__":
    unittest.main()
