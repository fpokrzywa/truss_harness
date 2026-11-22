# Handoff — 2026-11-22 ~15:00 UTC, charter 7 factory run (lead session e05940fb)

Read `docs/build/CHARTER.md` (sections 7, 10-12), then `docs/build/STATUS.md` (newest "Charter 7 progress"), then this file.

**State:** main = origin/main after this commit (code head `725e0a7` + docs). Cluster on 55450 running. `concurrency` 6. Handed off at the context limit (~190k).

## Finished this session
- Merged: #115 job 068 (PR #170, `20f8829`), #123 job 069 SP4-1 (PR #171, `725e0a7`).
- Filed: #174 (retention-delete hardening, from 072 reviews), #175 (front-end static matcher skips proxy headers), #177 (**main regression** from 069: `tests/runtime/test_agent_action.py::test_an_agent_naming_bs_model_directly_gets_not_found_from_the_real_model_lookup[app-role|schema-owner]` fails on `725e0a7`: "the model provider's adapter is not available in this environment", likely `AdapterRegistry.load(include_fake)` B-2776/B-2778). Fix #177 first: every full-suite gate fails until it is fixed.
- Decision ids used: B-2784, B-2785 (lead, on `lead/cm-l1-l2`), B-2791 (job 073). Builders were told: 073 from B-2791, 075 from B-2801, 071 from B-2811. **Next free for the lead: B-2821**; give 073's round-2 builder B-2831+.

## Exact next step
1. `PYTHONUTF8=1 factory/bin/job queue`. Before spawning a builder for 071 or 075, check `head -1 factory/jobs/<id>/build.md` and `git log -1 origin/factory/<id>`: this session's builders for 071 round 2 and 075 round 1 were still running at handoff and may have finished (READY → `job advance <id> gate`).
2. Start #177 as a job (`job create 177`) right away, priority over other `new` lines; then CM-1 (#159) once L1/L2 is merged (owner priority S-025).

## Jobs and their stages
| Job | Issue | PR | Stage | Next |
|---|---|---|---|---|
| 071-sp4-3-front-end | #125 | #172 | build r2 (feedback-1) | Lead isolation review BLOCKED round 1: `proxy.ts` forwards `//evil.example/%252e%252e/api/...` off-origin with cookies. feedback-1.md has the fix. After round 2 gate+reviews, approver will escalate again (proxy.ts): rerun `isolation-reviewer`, `gates.py` 13/13 (needs #177 fixed), merge by hand. |
| 072-sp4-8-retention-delete | #126 | #173 | gate r1 (fails only on lead-owned protected yaml, B-2705) | Code, security, isolation all PASS/APPROVED. Branch head `a14ebdb` (main merged). `gates.py --name 072-merge` 12/13, the only failures are #177. After #177 merges: merge main into the branch, `gates.py` 13/13, `gh pr ready 173`, `gh pr merge 173 --merge --match-head-commit <sha>`, `job queue`. |
| 073-sp4-4-cli-backup | #127 | #176 | build r2 (feedback-1) | Isolation review APPROVED (0 blocking); feedback-1 asks for: child env without SHIFDE_*, partial dump removal on any exception, password-scrub test, TLS params. Spawn `builder` named `builder-073-sp4-4-cli-backup` (round 2, port 5830, decision ids B-2831+). Then gate, reviewers, approver (will escalate: core/crypto.py), lead `gates.py` 13/13, hand merge. |
| 074-sp4-5-cli-backup | #128 | - | build r1 (not started) | Waits for 073 to merge (reuses `shifde.cli.backup`). Then spawn builder (port 5840). |
| 075-sp4-6-worker-and | #129 | ? | build r1 (builder was running) | Check build.md; if READY, gate. Port 5850. |
| 076-sp4-7-scripts-procctl | #130 | - | build r1 (not started) | Waits for 075 to merge. Port 5860. |
| 077-sp4-9-go-live | #131 | - | spec (not started) | Spawn spec-writer; build after 073-076 merge. |

## Branch `lead/cm-l1-l2` (worktree `.worktrees/cm-l1-l2`, DB `shifde_cm_l1_l2`)
- Head `2e60853`: r1_critic test setup now builds its pre-state with schema-owner SQL (trigger off inside one transaction), assertions unchanged (B-2784); departures accepted (B-2785). Targeted tests 192 passed. Isolation review APPROVED (minor: test_r1_critic.py:733 picks `ws["items"][0]`, could select "main" by slug).
- Next: merge origin/main into it, `python scripts/gates.py --ref <sha> --name cm-l1-l2-merge` 13/13 in an agent (needs #177 fixed), then fast-forward main or PR merge (no force push).

## Traps learned this session
- `gates.py` runs use the single `.worktrees/verify` checkout: never run two `gates.py` at once. `factory/bin/gate` (checks.sh) runs can overlap; they took 2-4 minutes each today.
- `factory/checks.sh` runs only the core test subset, so a job can merge with a full-suite regression (that is how #177 reached main). The full suite in `gates.py` catches it.
- Agent notifications cost the lead ~1-3k tokens each; ask every agent for a 1-5 line reply.
- Every merge still hits a DECISIONS.md append conflict; sync the branch with origin/main first (keep both sides' rows sorted by id).
