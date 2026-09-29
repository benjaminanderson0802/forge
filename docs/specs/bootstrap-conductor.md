# Bootstrap conductor: spec

> Implements the approved change to `docs/specs/layer-1-design.md` §7: build a small conductor first, then let it build the rest of Layer 1 with the D-025 team, unattended, with Ben reached by email only. Module: `core/bootstrap.py`. Tests: `tests/core/test_bootstrap.py`.

## Purpose

Run a queue of tasks through the team with no human relaying anything. Each task goes: Codex test writer → weak-test check → Claude builder → judges → Codex reviewer → merge into the layer branch → drift keeper. Blocked work goes to a Troubleshooter, and failing that, to Ben by email. When the queue is done, it opens a pull request to `main` and emails Ben for one approval.

## Interfaces

```python
from core.agents import AgentResult   # text, tokens, ok, error, data, provider

@dataclass
class Team:                 # each member has .run(prompt: str, cwd: Path, schema: dict | None) -> AgentResult
    test_writer: object     # Codex, workspace-write
    builder: object         # Claude, acceptEdits
    reviewer: object        # Codex, read-only
    troubleshooter: object  # Claude, acceptEdits
    drift_keeper: object    # Claude, read-only
    planner: object         # Claude, acceptEdits

class Conductor:
    def __init__(self, repo: Path, work: Path, state: Path, team: Team, limits: dict, *,
                 owner_email: str,
                 mailer: Callable[[str, str], None],          # (subject, body)
                 inbox: Callable[[], list[dict]],             # new messages: {"from", "subject", "body"}
                 gh: Callable[[list[str]], tuple[int, str]],  # runs `gh <args>` in repo; (exit, stdout)
                 clock: Callable[[], datetime] | None = None,
                 judge_cmds: list[str] | None = None,         # extra commands every build must pass
                 push: bool = True): ...
    def init_queue(self, layer: str, tasks: list[dict]) -> None
    def step(self) -> str   # one unit of work; returns a status below
    def run(self, max_steps: int | None = None, idle_sleep_s: int = 60) -> str
```

- `repo`: the main git checkout, on `main`, with remote `origin`.
- `work / <layer>`: a git worktree on branch `<layer>`, created from `main` if missing. All agent work happens there.
- `state`: conductor state:
  - `queue.json`
  - `questions.json`
  - `dead_ends.jsonl`
  - `runs/<run-id>/prompt.md` and `output.json`
  - the files `KILL` and `PAUSED`
  - a `core.ledger.Ledger` rooted at `state`, with `roles.json` written by `init_queue`
- Usage is metered with `core.usage.Meter(state)` using each `AgentResult.provider`. Caps come from `limits["<provider>_daily_token_cap"]`.

## Task records (`queue.json`)

```json
{"layer": "layer-1", "tasks": [
  {"id": "T1", "kind": "build", "title": "...", "section": "<plan text the agents need>",
   "files_in_scope": ["core/readiness.py"], "test_files": ["tests/core/test_readiness.py"],
   "test_cmd": "python -m unittest tests/core/test_readiness.py",
   "status": "todo", "notes": [], "fail_signatures": [], "troubleshot": false},
  {"id": "P1B", "kind": "plan", "title": "Plan 1B", "section": "<design text>",
   "plan_file": "docs/superpowers/plans/2026-09-30-layer-1b.md", "status": "todo", "notes": []}
]}
```

`status` is one of `todo`, `tests_ok`, `done`, `blocked`. Tasks run in queue order; `blocked` tasks are skipped.

## `step()` order, first match wins

1. **Replies.** Read `inbox()`.
   - Messages not from `owner_email` are ignored.
   - A message from the owner containing the word `STOP` (anywhere, case-insensitive) creates `state/KILL`.
   - A subject containing `[Forge Q-<qid>]` answers that open question (see Questions).
2. `state/KILL` exists → return `"killed"`. No agent is called.
3. `state/PAUSED` exists → return `"paused"`.
4. Any provider at or over its cap → return `"capped"`.
5. The next `todo` or `tests_ok` task runs one stage (below) → return `"worked"`.
6. No runnable tasks:
   - Every task is `done` and no gate question exists yet → open the gate (below) → return `"gate"`.
   - Otherwise → return `"idle"`.

## Build task stages

Before every agent run, the worktree is reset to the layer branch tip (`git reset --hard`, `git clean -fd`).

**Stage A: tests** (status `todo`)
- The test writer is prompted with the task title, `section`, `test_files` and the rule "write only these files; the tests must fail until the feature exists", with schema `{"required": ["files"]}`.
- Then the conductor checks:
  - **Wrong files:** if any changed file is not in `test_files`, all changes are discarded and a note starting `tests rejected: wrote outside test_files` is added.
  - **Weak tests:** it runs `test_cmd` in the worktree. If it exits 0, the tests are weak, changes are discarded, and a note starting `tests rejected: weak` is added.
  - **Otherwise** the test files are committed to the layer branch and the status becomes `tests_ok`.
- After 2 rejected test attempts the task becomes `blocked`, and a question (kind `blocked`) is emailed.

**Stage B: build** (status `tests_ok`). One attempt per `step()`:
1. The ledger contract is created on the first attempt: `title`, `spec_ref=task id`, `acceptance=test_cmd`, `files_in_scope`, `max_attempts=6`, `token_budget=10**9`. It is then claimed by `forge-executor`.
2. The builder prompt contains the task, the `section`, and, when they exist:
   - a line `REVIEW FEEDBACK:` followed by the previous reviewer reasons
   - a line `TROUBLESHOOTER NOTES:` followed by the troubleshooter's notes
   - a line `KNOWN DEAD ENDS:` followed by the entries in `dead_ends.jsonl`

   Schema `{"required": ["status"]}`.
3. **Protected tests:** any change to `test_files` is reverted (restored from the layer tip), and the attempt **fails** with the reason `touched test files`, even if the rest of the work is correct. This matches the Phase 0 core rule (drill 7): an attempt that touched protected files can never pass.
4. **Scope:** changed files outside `files_in_scope` and `test_files` mean the attempt fails, with the reason `out of scope: <files>`.
5. **Commit:** changed in-scope files are committed.
6. **Judges:** `test_cmd` then each of `judge_cmds` run in the worktree. The first non-zero exit fails the attempt. Its **failure signature** is the sha256 of the last 20 lines of that command's combined output.
7. **Ledger evidence:**
   - `run_report` (`forge-core`) with `changed`, `violations` (the reverted test files) and `out_of_scope`
   - `submit` (`forge-executor`) with the commit
   - `test_run` (`ci`), passed or not
8. **Review:** if the judges pass, the reviewer is prompted with the task, the `section` and the diff since the tests commit, with schema `{"required": ["verdict", "reasons"]}`.
   - `verdict == "pass"` → `pass` (`forge-auditor`); if the ledger refuses the pass, the attempt fails. Otherwise: status `done`, push the layer branch when `push` is on, then run the drift keeper.
   - Otherwise → the reasons are stored for the next attempt's `REVIEW FEEDBACK:`.
9. **Any failed attempt** (bad builder output, out of scope, judges or reviewer):
   - the commit is removed (`git reset --hard` to the tests commit)
   - `fail` (`forge-auditor`) if the contract was submitted, else `release` (`forge-core`)
   - then `reopen` (`forge-manager`) if the status is `failed`
   - the failure signature (or the reason text) is appended to `fail_signatures`
10. **Troubleshooter:** called in the same step, straight after the failure, when either:
    - the last 2 `fail_signatures` are equal (zero progress), or
    - 2 failed attempts have happened since the tests were accepted, or since the last troubleshoot

    It gets the task, the failure reasons and the last judge output, with schema `{"required": ["kind", "notes"]}`. Its notes are stored for `TROUBLESHOOTER NOTES:`. If `kind == "dead_end"`, a JSON line `{"task", "notes", "alternative"}` is appended to `dead_ends.jsonl`.
11. **Blocked:** after a troubleshoot, 2 more failed attempts make the task `blocked`, and a question (kind `blocked`) is emailed with the notes. The next task continues.
12. **Builder blocker:** if the builder returns `{"status": "blocked"}`, it counts as a failed attempt with reason `blocker: <summary>`. It goes to the troubleshooter on the same rules; the builder is never trusted to stop on its own say-so.

**Drift keeper** (after each `done`)
- Prompted with the section titles of all tasks, their statuses, and the text of `docs/specs/layer-1-design.md` if it exists in the worktree. Schema `{"required": ["status"]}`.
- `status == "replan"` → create `state/PAUSED` and email a question (kind `replan`) with the reasons.

## Plan tasks

A `kind == "plan"` task with status `todo`:
1. The planner writes `plan_file` in the worktree and returns `{"tasks": [...]}`. Each task needs `id`, `title`, `section`, `files_in_scope`, `test_files` and `test_cmd`.
2. **Checks:** any change outside `plan_file`, or any task missing a field, rejects the attempt.
3. **Review:** the reviewer checks the plan (schema `{"required": ["verdict", "reasons"]}`).
4. **On pass:** the plan file is committed, the returned tasks are appended to the queue (status `todo`, kind `build`, with empty `notes` and `fail_signatures`), and the plan task becomes `done`.
5. **On fail:** the reasons go into notes, and after 2 fails the task is `blocked` and emailed.

## Gate, questions, email

- **Gate:**
  - push the layer branch (when `push` is on)
  - `gh pr create --base main --head <layer> --title "<layer>: ready for approval" --body <report>`, then parse the PR number from the URL in stdout
  - create a question of kind `gate` carrying `pr`
  - email the subject `[Forge Q-<qid>] <layer> is ready: reply y to approve` with the report (task list and statuses)
- **Question ids:** short (`qid = <kind>-<n>`), stored in `questions.json` as `{"qid": {"kind", "status": "open", "task"?, "pr"?}}`.
- **Replies:**
  - `gate` answered with a body whose first word is `y` or `yes` → `gh pr edit <pr> --add-label human-approved`, then `gh pr merge <pr> --merge --delete-branch`. The question becomes `answered`.
  - `blocked` answered → the body text is added to the task notes (as `TROUBLESHOOTER NOTES:`), the task goes back to `tests_ok` (or `todo` if its tests were never accepted), and its counters reset.
  - `replan` answered → the body is added to notes and `PAUSED` is removed.
- **Mail helpers** (not unit-tested; they use the network): `gmail_mailer(owner)` and `gmail_inbox(owner, state)`. Both read the app password from `keyring` (`forge-gmail`); the inbox marks read messages as seen.

## Invariants the tests must pin

- An agent returning malformed output (`ok=False`) is a failed attempt, never an exception.
- The builder can never change `test_files` in a merged commit.
- Nothing is merged into `main` except through the owner's gate reply.
- No agent is called when `KILL` or `PAUSED` exists, or when its provider is capped.
- Every agent run writes `runs/<run-id>/prompt.md` and `output.json`.
- Emails from anyone except `owner_email` never act.
