"""RestTestServer answers a request whose body arrives after its headers (issue #51)."""

from __future__ import annotations

import json
import socket
import time

from shifde.adapters.testservers import RestTestServer


def _read_all(sock: socket.socket) -> bytes:
    chunks: list[bytes] = []
    while chunk := sock.recv(65536):
        chunks.append(chunk)
    return b"".join(chunks)


def test_flaky_retry_with_late_body_gets_its_200_and_no_reset() -> None:
    # httpx writes the headers and the body separately; under load the body lands after the
    # server answered. A server that closes with those bytes unread sends RST and the client
    # loses the 200, so a retried write looks indeterminate again.
    body = json.dumps({"v": "x"}).encode()
    head = (
        "POST /flaky HTTP/1.1\r\n"
        "Host: 127.0.0.1\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        "\r\n"
    ).encode()
    with RestTestServer() as server:
        server.flaky_calls = 1  # the next call is the retry that answers
        with socket.create_connection(("127.0.0.1", server.port), timeout=5) as sock:
            sock.sendall(head)
            time.sleep(0.2)
            sock.sendall(body)
            time.sleep(0.2)
            raw = _read_all(sock)
    status_line, _, rest = raw.partition(b"\r\n")
    assert status_line.split()[1] == b"200", raw
    payload = rest.partition(b"\r\n\r\n")[2]
    assert json.loads(payload) == {"ok": True, "call": 2}
    assert server.flaky_calls == 2
