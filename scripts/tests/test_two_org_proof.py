"""scripts/two_org_proof.py against a fake API: an isolated pair of organizations passes, a single
leak of the other organization fails the proof, and a missing users file stops it from running."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import threading
import unittest
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

_SPEC = importlib.util.spec_from_file_location(
    "two_org_proof", Path(__file__).resolve().parents[1] / "two_org_proof.py"
)
assert _SPEC and _SPEC.loader
top = importlib.util.module_from_spec(_SPEC)
sys.modules["two_org_proof"] = top
_SPEC.loader.exec_module(top)

PASSWORDS = {e: f"pw-{i}" for i, e in enumerate([top.PLATFORM_ADMIN, *(p[0] for p in top.PEOPLE)])}
ORG_OF = {email: org for email, org, _ in top.PEOPLE}
ADMINS = {email for email, _, reads in top.PEOPLE if reads}


def _handler(leak: str | None) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def _send(self, status: int, body: object = None, cookie: str | None = None) -> None:
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            if cookie:
                self.send_header("Set-Cookie", f"sid={cookie}; Path=/; HttpOnly")
            self.end_headers()
            self.wfile.write(raw)

        def _who(self) -> str | None:
            for part in self.headers.get("Cookie", "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == "sid":
                    return v
            return None

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if PASSWORDS.get(body["email"]) != body["password"]:
                return self._send(401)
            self._send(200, {"csrf_token": "t"}, cookie=body["email"])

        def do_GET(self) -> None:
            url = urlsplit(self.path)
            who = self._who()
            if who is None:
                return self._send(401)
            parts = url.path.split("/")[3:]  # after /api/v1
            if parts == ["orgs"]:
                if url.query == "scope=all":
                    if who != top.PLATFORM_ADMIN:
                        return self._send(403)
                    return self._send(200, {"items": [{"slug": o} for o in top.ORGS]})
                return self._send(200, {"items": [{"slug": ORG_OF[who]}]})
            org, rest = parts[1], parts[2:]
            if org != ORG_OF.get(who) and not (leak and rest == [leak]):
                return self._send(404)
            if rest == ["groups"]:
                if who not in ADMINS:
                    return self._send(403)
                return self._send(200, {"items": [{"name": f"Staff of {org}"}]})
            if rest == ["workspaces"]:
                return self._send(200, {"items": [{"slug": top.WORKSPACE}]})
            if rest[-1:] == ["projects"]:
                return self._send(200, {"items": [{"slug": top.PROJECT}]})
            self._send(200, {})

    return Handler


@contextlib.contextmanager
def fake_api(leak: str | None = None) -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(leak))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


class TwoOrgProofTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.users = Path(tmp.name) / "dev-users.txt"
        lines = ["# generated"] + [f"{e}\t{p}" for e, p in PASSWORDS.items()]
        self.users.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _run(self, base_url: str, users: Path | None = None) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = top.main(["--base-url", base_url, "--users", str(users or self.users)])
        return code, out.getvalue()

    def test_isolated_organizations_pass_and_no_password_is_printed(self) -> None:
        with fake_api() as url:
            code, out = self._run(url)
        self.assertEqual(code, 0, out)
        self.assertIn("TWO-ORG PROOF PASSED", out)
        self.assertNotIn("FAIL", out)
        for password in PASSWORDS.values():
            self.assertNotIn(password, out)

    def test_one_route_leaking_the_other_organization_fails_the_proof(self) -> None:
        with fake_api(leak="workspaces") as url:
            code, out = self._run(url)
        self.assertEqual(code, 1, out)
        self.assertIn("TWO-ORG PROOF FAILED: 5 of", out)  # each of the five people sees the leak
        self.assertIn("FAIL admin@demo-a.local: GET /api/v1/orgs/demo-b/workspaces -> 200", out)

    def test_missing_users_file_or_server_stops_the_proof(self) -> None:
        with fake_api() as url:
            code, out = self._run(url, self.users.with_name("absent.txt"))
        self.assertEqual(code, 2)
        self.assertIn("TWO-ORG PROOF NOT RUN", out)
        code, out = self._run("http://127.0.0.1:9", self.users)
        self.assertEqual(code, 2)
        self.assertIn("cannot reach", out)


if __name__ == "__main__":
    unittest.main()
