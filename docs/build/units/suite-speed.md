# suite-speed notes

Decision to record at merge:

| B-2330 | 2026-11-16 | Suite speed: in `tests/app` the clone of the per-test database no longer runs under the run lock (only the one-time template build does), and `DROP DATABASE` (which forces an immediate checkpoint) is deferred to the end of the worker session and run on 8 threads so waiting drops share checkpoints. Every test keeps its own fresh database; no assertion changed. Measured (idle, `-n 8`): `tests/app` alone 671 s with deferred serial drops, 306 s with unlocked clones, 272 s with threaded end-of-run drops; full suite baseline 1617 s, 1057 s with a drop-thread variant. Per-directory (sum of phases, baseline-like run): build setup 2063 s / call 593 s, app setup 1156 s / call 305 s, identity setup 326 s. `synchronous_commit=off` per clone was tried: no gain, dropped. Full-suite timing of the final variant not yet measured (cluster stopped mid-run). |
