"""API shortcuts of the full-app tests (SETH 3ba0e06 `tests/p2util.py`, the parts they use)."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient


def login(client: TestClient, passwords: dict[str, str], email: str) -> dict[str, str]:
    client.cookies.clear()
    r = client.post("/api/auth/local/login", json={"email": email, "password": passwords[email]})
    assert r.status_code == 200, r.text
    return {"X-CSRF-Token": r.json()["csrf_token"]}


def post(
    client: TestClient, url: str, headers: dict[str, str], body: Any, status: int = 201
) -> Any:
    r = client.post(url, json=body, headers=headers)
    assert r.status_code == status, f"{url}: {r.status_code} {r.text}"
    return r.json() if r.content else None
