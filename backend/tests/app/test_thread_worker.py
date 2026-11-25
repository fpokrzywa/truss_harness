"""`p5util.thread_worker` returns only after the worker's last heartbeat (DECISIONS B-4160)."""

from __future__ import annotations

import threading
import time

from shifde.core.settings import Settings
from tests._support.p5util import thread_worker


def test_a_heartbeat_in_progress_ends_before_thread_worker_returns(settings: Settings) -> None:
    entered, finished = threading.Event(), threading.Event()
    with thread_worker(settings, lease=3, reap=1000) as worker:

        def beat() -> None:
            entered.set()
            worker.closed.wait(30)
            time.sleep(0.5)  # still in progress while `Worker.run` ends
            finished.set()

        worker._beat = beat  # type: ignore[method-assign]
        assert entered.wait(30)
    assert finished.is_set()
