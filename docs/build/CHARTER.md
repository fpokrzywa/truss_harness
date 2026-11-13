# Build charter: SETH unified platform

You are the lead agent building the SETH unified platform in this directory. You are starting with no memory of how the design was reached. This charter is your standing instruction for the whole build. The owner will not be available while you work, so everything you need to decide alone is covered here.

## 1. What you are building and where the truth lives

Three existing applications are being merged into one, in one codebase with one PostgreSQL database. The full design is in `docs/superpowers/specs/2026-11-13-seth-unified-platform-design.md`. Read all of it before anything else. Where this charter and the design disagree about *what* to build, the design wins. This charter governs *how* you work.

**Check the design's Status line first. If it does not say "approved", stop and say so. Build nothing.**

The design conversation is finished and the design is its result, so do not restart it. Start at the plan: write the sub-project 1 plan with `superpowers:writing-plans`, then carry it out with the agents in section 4. Where any skill or process asks for the owner's approval or choice, the owner has delegated that to you under section 3: make the call, record it, continue.

You keep three files current, because your context will be compacted many times and these are how you find your place again:

- `docs/build/STATUS.md`: one row per module or issue with its state, the commit that completed it, the verification output summary, and tokens used per model.
- `docs/build/DECISIONS.md`: every call you made that the design did not settle, with the reason, dated.
- `CLAUDE.md` at the repository root: stack, local commands, validation commands, and a pointer to this charter. Create it with the skeleton.

Read `STATUS.md` at the start of every turn that follows a compaction or a resume.

## 2. Owner inputs

The owner fills these in before launch. A blank means use the fallback and record the item in `STATUS.md` as "waiting on owner input". Never invent a value and never ask; carry on with everything that does not depend on it.

| Input | Value | Fallback if blank |
|---|---|---|
| Hosted PostgreSQL connection | `DATABASE_URL` in `.env.local` at the repository root (supplied, not committed) | Prove everything on the local cluster; hosted steps wait |
| GitHub repository | `https://github.com/fpokrzywa/truss_harness`. Checked on 2026-11-13: private, empty, the signed-in `gh` account has admin rights, and it is already set as `origin` here | None: required |
| Token cap | None. Report spend (section 8) | |
| Databricks development workspace | In `.devdata/databricks.env` | Build and test against the fake platform; live validation waits |
| Entra application registration | In `.devdata/entra.env` | Build and test against a local test identity provider; live sign-in waits |
| Snowflake development account | In `.devdata/snowflake.env` | Same as Databricks |
| Where SETH will be hosted | (blank) | Build the go-live step's hosting-neutral parts; the hosting target waits |

Never print, log, commit or pass to an agent the contents of `.env.local` or of any file in `.devdata/`. Agents that need a connection read the file themselves. `.gitignore` already excludes both; confirm that with `git check-ignore` before the first commit.

**The hosted database.** `DATABASE_URL` points at the owner's PostgreSQL 17 server. Its role is for setup only: use it to create the schema-owning role and the application role, and never run the application as it. Before changing anything, list what is in the database it names. If that database holds tables this project did not create, leave it alone and create a database named `seth_unified` on the same server for this project; if the role cannot create one, record the hosted steps as waiting. Never drop or alter anything this project did not create.

## 3. What you may do without asking, and what you may never do

**You may:**

- Commit in the git repository that already exists here and push to the repository in section 2. Before the first push, confirm again with `gh repo view` that it is private; if it is public, do not push, and record why. If the remote has gained unrelated history since the check in section 2, push to a branch named `seth-unified`. Never force-push.
- Create issues, labels, branches and pull requests in that repository, and merge pull requests under the rules in section 7.
- Create the two database roles on the hosted instance, create this project's database there if section 2 calls for it, and run this project's migrations in it. Nothing else on that server is yours to touch.
- Make any design call the design leaves open, including the short design for each later sub-project. Log each in `DECISIONS.md`.
- Install development dependencies in this repository.

**You may never:**

- Modify, delete, commit to or push from the three source repositories. They are read-only references.
- Open `.env` or `azure_key.txt` in `S:\Development_2026\the_agent_factory`.
- Run tests against the hosted database, or create or drop databases there beyond the one named.
- Deploy to, or write to, any client platform other than a development target named in section 2.
- Mark a vendor integration as validated unless it has run against a real platform in this build. Without one, its status stays experimental.
- Remove, skip, or loosen a test, a type check, a lint rule or an import contract to make a gate pass. If a gate cannot pass, the work is not done.
- Merge any change that removes or weakens a cross-client test, the row-level security schema test, or the startup check on the database role.
- Commit a secret. Run the secret scan before every commit.

## 4. Agents and models

Agents cost three to ten times the tokens of doing the same work in one context, so use them only where they pay: to keep bulky work out of your own context, to run independent work at the same time, and to check work without knowing how it was built. Divide the work by what an agent needs to know, never by job title. One agent owns one module end to end: its code, its migration, its API and its tests. Do not create separate "database", "API" or "tester" agents for the same module; they would spend more on handing work to each other than on the work.

Create these as files in `.claude/agents/` as part of the skeleton, so the model for each is fixed by file and not by memory.

| Agent | Model | Owns | Why this model |
|---|---|---|---|
| You, the lead | opus | The plan, the order, briefs, decisions, the three files in section 1 | Judgment across the whole build. Keep your context small: delegate reading and building |
| `scout` | haiku | Inventories of a source module (files, tests, tables, routes, findings that belong to it), summaries of long failing output | Reading and listing; no judgment |
| `module-builder` | sonnet | One module: port the code and tests, write its migration branch, add its cross-client tests, fix its audit findings, make every gate pass | The bulk of the tokens; mostly careful adaptation of existing code |
| `module-builder-critical` | opus | The same, for `authz`, `secrets`, `identity`, `tenancy`, `connections`, `runtime`, and every new module that holds client credentials | A mistake here leaks one client's data to another |
| `frontend-builder` | sonnet | The front-end area for one module, started only after that module's API is merged | The API is then a fixed contract, so this is safely separate work |
| `verifier` | sonnet | Checking one finished unit from the outside | Does not need to know how it was built, only whether it holds |
| `isolation-reviewer` | opus | Reading the diff of critical modules and every migration for a way one client could reach another's data | The one review where a miss is unacceptable |

Do not use Fable for any agent. It costs two and a half times Opus and nothing here needs it.

**Briefs.** An agent knows only what you write. Each brief states the goal, the exact source paths to read, the base commit, the acceptance checks as commands, what is out of scope, and the rules from section 3 that apply. Give the verifier the branch, the acceptance checks and the tools; do not give it the builder's reasoning.

**Verification.** No unit is accepted on the builder's word. The verifier runs the complete gate set for that unit (section 5) and reports each command with its exit code and the failing output. Tell it in the brief to run every check in full before reporting a pass. On a failure, send the failures back to the same builder. After two failed rounds with a sonnet builder, give the unit to the opus builder once. If it still fails, record it in `STATUS.md` as blocked with the evidence, and continue with work that does not depend on it.

**Running at once.** At most three agents at a time, each in its own git worktree with its own local database. Modules in the same layer of the design's layer order may be built in parallel. A front-end area may be built while the next module's back end is built.

## 5. Sub-project 1: skeleton, then SETH's modules

Follow section 6 of the design ("Sub-project 1 in detail"). In short:

1. Skeleton: both packages, the local database script, the gates, the test layout, continuous integration, the baseline migration, both database roles, the startup role check, the agent files from section 4 and the usage report from section 8. Windows notes: `UV_LINK_MODE=copy`, `python` not `python3`, `git config core.longpaths true`. Use a local database port other than 55440.
2. Modules from the bottom layer up, from SETH at commit `3ba0e06`. For each: scout inventory, build, verify, isolation review if critical, merge to `main`, push, update `STATUS.md`. Then its front-end area the same way.
3. Confirm by test, before accepting `runtime` and `connections`, that a worker crash during an approved write is not reported as cancelled, and that repointing a connection cannot send a secret without the `secret:use` permission.

**The gate set for a unit**, all from a clean checkout of its branch:

- secret scan
- `ruff format --check`, `ruff check`, `mypy` (strict), `lint-imports`
- the module's tests, and the tests of every module that imports it
- the schema comparison against SETH for ported tables
- the row-level security schema test and the module's cross-client tests
- for front-end units: lint, typecheck, tests, build

**Sub-project 1 is done when** the full backend and front-end suites pass from a clean checkout of `main`; the full backend suite time is measured and recorded against the 20-minute target; the application refuses to start under a privileged database role; two test organizations with separate groups work side by side with neither visible to the other's users (on the hosted database if section 2 supplies it); and `main` is pushed.

## 6. After sub-project 1: install the software factory

When sub-project 1 is done and pushed, install the factory by following its own instructions:

> Install this software factory in this project from the reference repository:
> https://github.com/zazencodes/zazencodes-season-3/tree/main/src/software-factory-claude-code-v2

Adapt it as its `INSTALL.md` directs, with these additions:

- `factory/checks.sh` runs the gate set from section 5. It may run the changed modules' tests plus a fixed core set by default and everything when a full-run variable is set. SETH's own `factory/checks.sh` and `factory/bin/jobdb` (one database per job) are working references for this machine; read them.
- Scripts run with `python`, not `python3`. On this machine `python3` is a Microsoft Store stub, so the factory's `python3` prerequisite is met by `python`; change the scripts' invocations accordingly.
- Start with `concurrency` 1. Raise it to 2 after five issues in a row merge without a failed gate.
- Set the intake agent's model to sonnet. Issues will be written by you from an approved design, so intake does not need the most expensive model.
- The factory is licensed GPLv3. Record that in `DECISIONS.md` so the owner sees it; do not copy factory code into the application package.

Commit and push the install.

## 7. Sub-projects 2 to 8 through the factory

First, file one issue for each major audit finding that sub-project 1 left as a known defect, so the factory works through them alongside the sub-projects. All must be closed, or recorded as needing the owner's written acceptance, by the end of the go-live step (sub-project 4). No real client data enters SETH before then, so use test organizations only.

For each sub-project in the design's table, in order:

1. Write its short design in `docs/superpowers/specs/`, have a fresh agent read it as a builder and list what it would have to guess, settle those yourself, and log the calls.
2. Cut it into issues. One issue is one module-sized slice with its migration, API, screen and tests, and states its acceptance checks as commands. Label each `factory`.
3. Run `/factory run` until the queue is empty.
4. Where the factory asks the issue author a question, answer it from the design; if the design does not say, decide and log it.
5. Where the factory hands a pull request to a human, you are that human: run the full suite on it, have `isolation-reviewer` read it, then merge it or send it back through the factory with what to change. A pull request that still fails after that is closed and recorded as blocked. Section 3's "never merge" rule has no exceptions.
6. Run the sub-project's "done when" check from the design and record the result.

Where a sub-project needs a real platform and section 2 does not supply one, finish it against the fake platform, leave the integration marked experimental, record "live validation waiting on owner input", and move to the next sub-project.

## 8. Cost

The owner wants to see where every token goes. Build this with the skeleton and keep it working for the whole build.

**`scripts/usage_report.py`** (standard library only) reads Claude Code's own records for this project and writes `docs/build/usage.html` (one self-contained page) and `docs/build/usage.json`, and prints a summary table.

- Source: the session files under `~/.claude/projects/<this project's folder>/`. Each session is `<session-id>.jsonl`; each agent it started is `<session-id>/subagents/agent-<id>.jsonl` with an `agent-<id>.meta.json` beside it holding `agentType` and `description`. Include the folders for worktrees under this repository too.
- Every assistant line carries `message.model` and `message.usage` with `input_tokens`, `output_tokens`, `cache_creation_input_tokens` and `cache_read_input_tokens`. One model response can appear on several lines; count each `requestId` once.
- Break the totals down by model, by agent type, by unit, by sub-project and by day, with the four token classes kept separate. Start every agent's description with its unit in square brackets, for example `[tenancy] build`, so the report can group by unit. Work done by you directly is reported as "lead".
- Show an estimated dollar figure per row from `docs/build/prices.json`, in dollars per million tokens for each token class and model. Seed it from `prices_usd_per_mtok` in SETH's `factory/config.json`. Label every dollar figure as an estimate at list price, not a bill.
- Also show, per unit, how many build and verify rounds it took, so expensive units stand out.

Regenerate the report after every accepted unit and commit it with `STATUS.md`. The owner can run `python scripts/usage_report.py` at any time to see the current figures. After the factory is installed, its own dashboard covers factory jobs; keep this report as the single view across the whole build.

Spend less by: keeping your own context small; giving `scout` the reading; never running two builders on the same module; never re-running a full suite that has not changed since it last passed.

## 9. Proving you are done

The check on whether you have finished reads only this conversation. It cannot run commands or open files. So when you believe a goal is met, run its checks again in that same turn and show each command with its exit code and the summary line of its output, followed by the current `STATUS.md` table. A claim without the output in the same turn does not count.

Report things as they are. A blocked unit, a missed time target or an integration that has not touched a real platform is stated as such, with the evidence.
