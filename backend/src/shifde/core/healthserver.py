"""The liveness / readiness port of `shifde-worker` and `shifde-executor` (SP4 design 6.2).

`GET /healthz` and `GET /readyz` answer 200 `{"status":"ok"}` while their predicate holds and 503
`{"status":"unavailable"}` when it does not or raises; any other path or method gets 404. Served
by the standard library on a daemon thread, without request logs. Stdlib only: the executor uses
it too and must never reach the database, so nothing here imports sqlalchemy or another `shifde`
module.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

Probe = Callable[[], bool]


class _Server(ThreadingHTTPServer):
    # no SO_REUSEADDR: on Windows it would let a second process bind a port already in use
    allow_reuse_address = False
    daemon_threads = True

    def __init__(self, address: tuple[str, int], probes: dict[str, Probe]) -> None:
        self.probes = probes
        super().__init__(address, _Handler)


class _Handler(BaseHTTPRequestHandler):
    server: _Server

    def do_GET(self) -> None:  # noqa: N802 - the http.server method name
        probe = self.server.probes.get(self.path)
        if probe is None:
            self._answer(404, "not_found")
            return
        try:
            ok = bool(probe())
        except Exception:  # noqa: BLE001 - a failing probe means not live / not ready
            ok = False
        self._answer(200 if ok else 503, "ok" if ok else "unavailable")

    def __getattr__(self, name: str) -> Callable[[], None]:
        # any other method (`do_POST`, `do_HEAD`, ...) is a 404, not http.server's 501
        if name.startswith("do_"):
            return lambda: self._answer(404, "not_found")
        raise AttributeError(name)

    def _answer(self, status: int, text: str) -> None:
        body = json.dumps({"status": text}, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - the base signature
        return  # no request log on stderr


class HealthServer:
    """Binds `host:port` when built (an `OSError` when it cannot); `start()` serves it on a daemon
    thread and `stop()` closes it. `port` is the bound port (port 0 binds a free one)."""

    def __init__(self, host: str, port: int, *, live: Probe, ready: Probe) -> None:
        self._server = _Server((host, port), {"/healthz": live, "/readyz": ready})
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True, name="shifde-health"
        )

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def start(self) -> HealthServer:
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._thread.is_alive():
            self._server.shutdown()
        self._server.server_close()
