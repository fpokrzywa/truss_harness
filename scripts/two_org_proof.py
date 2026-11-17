"""Two-organization side-by-side proof against a running `shifde-api` (charter section 5).

Standard library only. Needs a database seeded with `shifde-admin dev-seed` (two organizations,
`demo-a` and `demo-b`, each with its own staff group) and the API serving it with local login on.
Every seeded person logs in over HTTP and the script checks that each one sees its own
organization (its workspace, project and, for an administrator, its own group) and that every
route of the other organization answers 404, as if it did not exist. The platform administrator
sees both. Passwords are read from the users file and never printed.

    python scripts/two_org_proof.py [--base-url http://127.0.0.1:8710]
        [--users .devdata/dev-users.txt]

Prints one line per check and ends with `TWO-ORG PROOF PASSED` (exit 0) or
`TWO-ORG PROOF FAILED: <n> of <m> checks` (exit 1). Exit 2: the proof could not run.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
ORGS = ("demo-a", "demo-b")
PLATFORM_ADMIN = "platform@shifde.local"
# (email, organization, may read the organization's groups)
PEOPLE = (
    ("admin@demo-a.local", "demo-a", True),
    ("viewer@demo-a.local", "demo-a", False),
    ("staff@demo-a.local", "demo-a", False),
    ("admin@demo-b.local", "demo-b", True),
    ("staff@demo-b.local", "demo-b", False),
)
WORKSPACE, PROJECT = "main", "sample"  # what dev-seed creates in each organization


class ProofError(Exception):
    """The proof could not run (no server, no users file, a login refused)."""


def read_users(path: Path) -> dict[str, str]:
    """`email<TAB>password` lines as written by `shifde-admin dev-seed`."""
    if not path.is_file():
        raise ProofError(f"{path} not found: run `shifde-admin dev-seed` first")
    users: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and not line.lstrip().startswith("#"):
            users[parts[0].strip().lower()] = parts[1].strip()
    return users


class Client:
    """One browser-like session: its own cookie jar, the allowed Origin on every request."""

    def __init__(self, base_url: str, origin: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.origin = origin
        self.csrf: str | None = None
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )

    def request(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        headers = {"Origin": self.origin, "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if self.csrf:
            headers["X-CSRF-Token"] = self.csrf
        req = urllib.request.Request(self.base_url + path, data, headers, method=method)
        try:
            with self._opener.open(req, timeout=30) as resp:
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read()
        except (urllib.error.URLError, OSError) as exc:
            raise ProofError(f"cannot reach {self.base_url}: {exc.__class__.__name__}") from None
        try:
            return status, json.loads(raw) if raw else None
        except ValueError:
            return status, None

    def get(self, path: str) -> tuple[int, Any]:
        return self.request("GET", path)

    def login(self, email: str, password: str) -> None:
        status, body = self.request(
            "POST", "/api/auth/local/login", {"email": email, "password": password}
        )
        if status != 200 or not isinstance(body, dict):
            raise ProofError(f"login refused for {email} (HTTP {status})")
        self.csrf = body.get("csrf_token")


def _items(body: Any, key: str) -> list[str]:
    if not isinstance(body, dict) or not isinstance(body.get("items"), list):
        return []
    return [str(i.get(key)) for i in body["items"] if isinstance(i, dict)]


@dataclass
class Proof:
    results: list[tuple[bool, str]] = field(default_factory=list)

    def check(self, ok: bool, what: str) -> None:
        self.results.append((ok, what))

    @property
    def failures(self) -> int:
        return sum(1 for ok, _ in self.results if not ok)


def prove(new_client: Callable[[], Client], users: dict[str, str]) -> Proof:
    missing = [e for e in (PLATFORM_ADMIN, *(p[0] for p in PEOPLE)) if e not in users]
    if missing:
        raise ProofError(f"users file lacks {', '.join(missing)}: run `shifde-admin dev-seed`")
    proof = Proof()

    anon = new_client()
    status, _ = anon.get("/api/v1/orgs")
    proof.check(status == 401, f"no session: GET /api/v1/orgs -> {status} (want 401)")

    admin = new_client()
    admin.login(PLATFORM_ADMIN, users[PLATFORM_ADMIN])
    status, body = admin.get("/api/v1/orgs?scope=all")
    seen = sorted(set(_items(body, "slug")) & set(ORGS))
    proof.check(
        status == 200 and seen == list(ORGS),
        f"{PLATFORM_ADMIN}: ?scope=all lists {seen} side by side (want {list(ORGS)})",
    )

    for email, org, reads_groups in PEOPLE:
        other = ORGS[1] if org == ORGS[0] else ORGS[0]
        c = new_client()
        c.login(email, users[email])
        status, body = c.get("/api/v1/orgs")
        slugs = _items(body, "slug")
        proof.check(status == 200 and slugs == [org], f"{email}: lists orgs {slugs} (want [{org}])")
        status, _ = c.get("/api/v1/orgs?scope=all")
        proof.check(status == 403, f"{email}: GET /api/v1/orgs?scope=all -> {status} (want 403)")
        status, body = c.get(f"/api/v1/orgs/{org}/workspaces")
        proof.check(
            status == 200 and WORKSPACE in _items(body, "slug"),
            f"{email}: own workspaces -> {status} {_items(body, 'slug')}",
        )
        status, body = c.get(f"/api/v1/orgs/{org}/workspaces/{WORKSPACE}/projects")
        proof.check(
            status == 200 and PROJECT in _items(body, "slug"),
            f"{email}: own projects -> {status} {_items(body, 'slug')}",
        )
        status, body = c.get(f"/api/v1/orgs/{org}/groups")
        if reads_groups:
            names = _items(body, "name")
            proof.check(
                status == 200 and names == [f"Staff of {org}"],
                f"{email}: own groups -> {status} {names} (want ['Staff of {org}'])",
            )
        else:
            proof.check(status == 403, f"{email}: own groups -> {status} (want 403, viewer)")
        for suffix in (
            "",
            "/members",
            "/dashboard",
            "/groups",
            "/workspaces",
            f"/workspaces/{WORKSPACE}",
            f"/workspaces/{WORKSPACE}/projects",
            f"/workspaces/{WORKSPACE}/projects/{PROJECT}",
        ):
            path = f"/api/v1/orgs/{other}{suffix}"
            status, _ = c.get(path)
            proof.check(status == 404, f"{email}: GET {path} -> {status} (want 404)")
    return proof


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://127.0.0.1:8710")
    parser.add_argument("--origin", default="http://localhost:3710")
    parser.add_argument("--users", type=Path, default=ROOT / ".devdata" / "dev-users.txt")
    args = parser.parse_args(argv)
    try:
        users = read_users(args.users)
        proof = prove(lambda: Client(args.base_url, args.origin), users)
    except ProofError as exc:
        print(f"TWO-ORG PROOF NOT RUN: {exc}")
        return 2
    for ok, what in proof.results:
        print(f"{'ok  ' if ok else 'FAIL'} {what}")
    if proof.failures:
        print(f"TWO-ORG PROOF FAILED: {proof.failures} of {len(proof.results)} checks")
        return 1
    print(f"TWO-ORG PROOF PASSED: {len(proof.results)} checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
