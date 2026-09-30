# Layer 1 design: the conductor, the team, and the always-on service

> Source of truth for Layer 1. It implements decisions D-018 to D-033 in `docs/DECISIONS.md`. The implementation plans in `docs/superpowers/plans/` argue from this file. It replaces the 2026-09-29 draft plan (`2026-09-29-forge-layer-1.md`), which is kept only for history.

## 1. The physics

- **Agents never talk to each other.** Each agent is one short-lived process (`claude.cmd -p …` or `codex exec …`). It starts, does exactly one job in its own git worktree, writes its result to a file, and exits.
- **The conductor** (plain Python, no AI) is the only thing that starts agents, validates their output against a fixed JSON shape, records results in the ledger, and decides what runs next. All hand-offs are files in git plus ledger events.
- **Hidden, low priority.** Every agent process starts with no window (`CREATE_NO_WINDOW`) and at below-normal priority, and has a hard timeout that kills its whole process tree.
- **Codex runs isolated:** `codex exec --ignore-user-config --ephemeral --json -o <file> [--output-schema <file>] -s <read-only|workspace-write> -C <worktree>`. Usage is read from the `turn.completed` event: `input_tokens + output_tokens + reasoning_output_tokens`, with cached input counted separately.
- **Claude runs as:** `claude.cmd -p <prompt> --output-format json --permission-mode <plan|acceptEdits> [--allowedTools …]`. Usage is read from the `usage` object.
- **Free fallback** (never for independent roles): `codex exec --oss --local-provider ollama` for chores such as summarizing logs.

## 2. Roles

| Role | Engine | May write | Output shape |
|---|---|---|---|
| Manager | Claude, read-only | nothing | `plan.schema.json` |
| Test writer | Codex, workspace-write | `tests/acceptance/**` only | `tests.schema.json` |
| Builder | Claude Code, acceptEdits | the contract's `files_in_scope` only | `claim.schema.json`: `done` or a blocker with evidence |
| Judges | plain code | — | test run, drills, mutation score |
| Reviewer | Codex, read-only | nothing | `review.schema.json`: pass/fail with reasons, plus a drift flag |
| Troubleshooter | Claude, acceptEdits, research tools | its own scratch worktree | `fix.schema.json`: fix, or dead end plus alternative |
| Drift keeper | Claude, read-only | nothing | `drift.schema.json`: ok, or re-plan plus reasons |

The engines are fixed by role (D-020): when a cap is hit, the team slows down; it never swaps engines.

## 3. One contract's path

1. **Test writer** writes the tests. The weak-test check runs: the new tests must fail on the current code, and must fail on an empty implementation.
2. **Builder** runs, capped by the focus rule (D-026):
   - A blocker, or 2 attempts or 20 minutes without passing, hands off to the Troubleshooter.
   - 2 zero-progress attempts hand off immediately.
3. **Runner:** protected-file restore and scope check (Phase 0 core).
4. **Judges:**
   - acceptance tests at the exact commit, in a throwaway worktree
   - all drills
   - mutation testing on changed lines: each mutant must be caught, and the rate must be at least `mutation_min` (charter limits)
5. **Reviewer:** the verdict and its reasons go into the ledger. On a fail, the reasons are fed to the next Builder attempt.
6. **Merge** into the layer branch. `main` changes only with Ben's approval (D-027).
7. **Drift keeper** runs after every merge. Coverage must rise. If there's no gain in 3 merges, or no merge in 2 active hours, it re-plans.

## 4. Blockers, capabilities, readiness

- **Readiness check** (plain code) before every session and cycle. It writes `ledger/capabilities.json`: for each capability, `ok`, `detail` and `checked_at`. A failing check starts a Troubleshooter job, or puts an item in Ben's queue with the exact fix. No Builder starts while a capability its contract needs is broken.
- **Blocker claims** (D-031) need at least 2 routes tried, the error output, the capability needed, and what the agent works on meanwhile.
  - Plain code rejects any claim that contradicts `capabilities.json`.
  - The Reviewer rejects claims without real attempts.
  - Every rejection is logged as an `easy_out` event.
- **Known dead ends:** `ledger/dead_ends.jsonl`. It is appended by the Troubleshooter and included in every Builder and Troubleshooter prompt.

## 5. The always-on service (D-007, D-018, D-019)

- Started at logon by Task Scheduler (hidden, `pythonw`). A watchdog task checks every 5 minutes and restarts it if needed.
- Main loop:
  - While there is runnable work, run the next step immediately.
  - Otherwise sleep until woken by: a changed spec approval, a reply from Ben, a scheduled venture timer, or the cap reset at 00:00 UTC.
- **Activity-aware:**
  - While Ben has been active in the last 10 minutes (Windows `GetLastInputInfo`): one agent at a time, below-normal priority, no browsers.
  - Once he's been idle for 10+ minutes: up to `max_parallel_idle` agents.
- **Emergency stop:** `ledger/KILL`. It can be created by the desktop shortcut, an email "STOP", or the status page button. The service checks it between every step and before starting any agent.

## 6. Reaching Ben (D-021 to D-024)

- **Queue:** `ledger/queue.jsonl`. Each item has an id, kind, question, default, deadline and status.
- **Instant email** only for the D-023 kinds. The **digest** goes out at 08:00 local. **Quiet hours** are 23:00–07:00, except emergencies.
- **Replies** are read from Gmail over IMAP, matched by `[Forge Q-<id>]` in the subject, and accepted only from Ben's address. Answers: `y`, `n`, `STOP` or free text.
- **Status page** at `http://127.0.0.1:8765`. It is bound to localhost only and shows the queue, current work, caps and capabilities, with Approve / Stop buttons.

## 7. Build order (bootstrap)

Each sub-plan is built by the D-025 team. For **1A and 1B there is no conductor yet**, so the conductor's steps are run by hand. Cowork Claude drives:
- Codex (`codex exec`) writes the tests.
- Claude builds.
- The judges run.
- Codex reviews.

Every task is still a ledger contract. From **1C** on, the 1B conductor runs the team itself.

| Sub-plan | Delivers | Gate |
|---|---|---|
| 1A Agent runtime | Claude and Codex adapters (hidden, low priority, timeouts, usage); two-provider meter; readiness check and capability map | Adapter tests; readiness check shows every capability truthfully on the PC |
| 1B Conductor pipeline | One contract end to end: test writer, weak-test check, builder with focus rule, runner, judges with mutation testing, reviewer, merge; blocker and Troubleshooter path | Drills: false blocker rejected, weak test caught, zero-progress hand-off, mutation gate, full pipeline on a toy contract with real agents |
| 1C Planning and drift | Manager; spec coverage map; drift keeper; stall rules | Drills: coverage must rise; re-plan triggers; a fresh Manager sees the ledger only |
| 1D Always-on service | Service loop, watchdog, activity awareness, kill switch (three ways), cap waiting | Drills: kill mid-step; restart after crash; active vs idle behaviour |
| 1E Ben channel | Queue, instant email, digest, reply reading, status page | Drills: STOP by email halts; a reply answers the right question; a foreign sender is ignored |

Layer 1 is done when all five gates pass and Forge builds a small real project from an approved spec with no human help except approvals.


## Input for 1E (Ben's channel): ChatGPT Dots evaluation (2026-09-30)

OpenAI launched Dots on 2026-09-29: always-on ChatGPT agents on a cloud computer, with event triggers, approval gates and an activity view. Ben is on Pro, which includes one Dot. What was found, from the Dot's own answers and OpenAI's docs:

- **No verified API for a personal Dot.** The nearest mechanism is MCP Events (developers.openai.com/plugins/build/mcp-events): a plugin server sends signed webhooks, and they wake **chats**. Dots aren't named as a recipient. The spec is a **draft**, and it needs a plugin server that ChatGPT can reach over HTTPS, which would open Ben's PC to the internet.
- **Two-way messaging:** the Dot can ask Ben questions in the app. There's no built-in callback that returns an answer to Forge in a fixed format, and SMS or Dot-initiated calls aren't established.
- **Limits:** no published per-day task limits. Dot rules are instructions, not enforced caps.

**Decision for 1E:**
- **Email stays Forge's channel** (R17-R40 caps and checks). Forge keeps enforcing all caps, spending blocks and the audit log itself.
- 1E includes a **Dot round-trip trial as its own task**: a Forge event produces one question to Ben, and Ben's answer is written back and validated by Forge.
- **Adopt a Dot as the channel only if:**
  1. the round trip passes end to end;
  2. it needs no inbound exposure of Ben's PC (or uses a vetted, authenticated tunnel);
  3. Forge's caps still apply.
- **Until then, use a Dot only outside the trusted core**, for example venture research. Its findings are data, and Forge verifies them before use.
