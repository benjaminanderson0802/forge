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

## Review round 1 amendments (Codex review, 2026-09-29; these override anything above)

- **R1 Safe test commands.** A task's `test_cmd` must match `python -m unittest <path> [<path> ...]`, where every path is one of the task's `test_files` (unittest may be given as a module path with dots, or as a file path). It runs **without a shell** as `[sys.executable, "-m", "unittest", *paths]`. Plans or tasks with any other command are rejected at `init_queue` (raise `ValueError`) or at plan review (note `plan rejected: unsafe test_cmd`). Task fields are also validated:
  - `id` matches `^[A-Za-z0-9_-]{1,40}$`
  - `test_files` must be under `tests/`, end in `.py`, and contain no `..`
  - `files_in_scope` must not contain `..` or absolute paths
- **R2 Reply codes.** Every question gets a random `code` (8 URL-safe characters), stored in `questions.json`.
  - **Subject format:** `[Forge Q-<qid> <code>] ...`
  - **A reply counts only when** its subject contains both the qid and the code, and it comes from `owner_email`. A reply with the right qid but the wrong or missing code is ignored.
  - **`STOP` keeps working without a code** (owner address only): a forged STOP can only halt Forge, which is safe.
- **R3 Gate actions checked.**
  - If `gh pr edit` or `gh pr merge` fails (non-zero exit), the gate question stays `open` and a new email reports the error.
  - It becomes `answered` only after the merge succeeds.
  - A failed `gh pr create` does not create a gate question; it emails the error and returns `"gate"` again on the next step.
- **R4 Weak-test check needs a real failing run.** Tests are accepted only if the command:
  - does not time out, and
  - prints unittest's `Ran N test` line with N ≥ 1, and
  - exits non-zero.

  Otherwise they're rejected with a note starting `tests rejected: no real failing run`.
- **R5 Focus rule, full.**
  - **Troubleshooter rounds:** up to 3 per task. Round 1 runs as specified above (zero progress, or 2 failed attempts). After a round, 2 more failed attempts (or zero progress again) trigger the next round. The task is blocked only after round 3 is followed by 2 more failed attempts.
  - **Builder blocker evidence:** a builder `blocked` answer must include `tried` (a list of at least 2 routes) and `error` (non-empty). Otherwise the attempt fails with the reason `blocker rejected: no evidence (easy out)`, and a line `{"task", "kind": "easy_out", ...}` is appended to `state/easy_outs.jsonl`.
- **R6 Drift check must succeed.** If the drift keeper's result is unusable (`ok=False` or missing `status`), `drift_due` stays set and it is retried on the next step. After 3 unusable results a question (kind `replan`) is emailed and `PAUSED` is created. The gate never opens while `drift_due` is set.
- **R7 Email failures.**
  - If `mailer` raises, the question is saved with `"delivered": false`. Every step retries undelivered questions before anything else, and a question is `delivered: true` once sent.
  - If `inbox()` raises, the error is appended to `state/errors.log` and the step continues.
- **R8 Git errors.** If `git status` fails, the current stage fails as an attempt with the reason `git error`. It never counts as "no changes".
- **R9 State tamper alarm.** Before every agent run, the conductor fingerprints (sha256) every file in `state/` except `runs/` and `meter.json`. If anything differs after the run, it:
  - creates `KILL`
  - appends to `errors.log`
  - emails a question of kind `tamper` naming the changed files
  - returns without recording the run's result

  The conductor's own writes happen outside agent runs, so they never trigger it.

## Review round 2 amendments (Codex review, 2026-09-29)

- **R10 Fingerprint everything.** The R9 fingerprint covers every file in `state/` except `runs/` and `meter.json`. Temporary files are included.
- **R11 One conductor, guaranteed.** `main run` takes an operating-system exclusive lock on `state/conductor.lock`: `msvcrt.locking` on Windows, `fcntl.flock` elsewhere. The lock is held for the life of the process. A second process that can't get the lock exits at once with code 0. The lock is exposed as `acquire_lock(state) -> handle | None` so tests can check that a second acquisition returns `None` while the first is held.
- **R12 The loop survives everything.** Heartbeat writes happen inside the loop's error handling, so a failed heartbeat is logged and the loop continues.
- **R13 Stage errors back off.** A stage `RuntimeError` or `OSError` makes `step()` return `"error"`, not `"worked"`. `run()` backs off on `"error"` just as it does on an exception. After 3 consecutive `"error"` steps, Ben is emailed once.

## Review round 3 amendment

- **R14 Nothing is exempt.** The tamper fingerprint covers every file in `state/`, including `runs/` and `meter.json`. The conductor never writes to `state/` while an agent run is in progress: the run's `prompt.md` is written before the "before" fingerprint, and `output.json` and meter updates only after the "after" comparison. Files under `runs/` use a size+mtime signature, for speed; all other files use sha256.

## Live-run amendments (first start on Ben's PC, 2026-09-29)

- **R15 The lock file is fingerprinted by signature, and nothing unreadable is trusted.** On Windows the R11 lock makes `state/conductor.lock` unreadable to its own process, so hashing it crashed every agent run with `PermissionError`. Now:
  - `acquire_lock` empties the lock file right after taking the lock, so it always holds zero bytes and has no content to hide. It is fingerprinted as `lock:<size>:<inode>:<mtime>`, which catches any write (the size becomes non-zero), a replaced file (a new inode), or a touch.
  - **Fail closed before a run.** If any other file in `state/` can't be read when the "before" fingerprint is taken, the agent is not launched. The stage raises an error (R13 backoff, and Ben is emailed after 3).
  - **Only an empty lock is trusted.** If `conductor.lock` exists but isn't empty when the "before" fingerprint is taken, the agent is not launched, and the stage raises an error in the same way. Every command that can launch agents holds the lock: `run`, and also `step`, which prints `busy` and exits if another conductor holds it.
  - **Tamper after a run.** A file that becomes unreadable during a run gets an `unreadable:<size>:<mtime>` signature. That never equals a sha256, so the tamper alarm fires.
  - **The check itself must complete.** The fingerprint walks `state/` strictly: any error listing a folder or reading a file's details raises, and nothing inaccessible is silently left out. Before a run, a fingerprint that fails means the agent is not launched (stage error). After a run, a fingerprint that fails counts as tampering: KILL is written, Ben is emailed, and nothing from the run is recorded.
- **R16 Codex keeps the Windows sandbox.** `--ignore-user-config` also drops Ben's `windows.sandbox` setting, and without it Codex silently downgrades `workspace-write` to read-only, so the test writer could never write. On Windows, `CodexAgent` passes `-c windows.sandbox="elevated"` for every sandbox mode. The setting only chooses Windows' sandbox implementation, so the reviewer stays `read-only`. Prerequisite for unattended runs: the Codex Windows sandbox has been set up once on the PC (done 2026-09-28).

## Live-run amendments, round 2 (first real agent runs, 2026-09-29)

The first run with real agents and real email hit four faults no fake-based test could see. It sent Ben about 27 emails, each blocked email twice the size of the last. These rules stop each fault and bound the damage from any fault like it.

- **R17 Real answer schemas.** Each role's schema (`S_TESTS`, `S_BUILD`, `S_REVIEW`, `S_TROUBLE`, `S_DRIFT`, `S_PLAN`) is a full JSON Schema: `"type": "object"`, `properties` with types, and `required` listing the keys the conductor needs. Codex receives a strict form built by `agents.strict_schema(schema)`:
  - every object gets `additionalProperties: false`, and every property is listed in `required`;
  - properties that weren't required become nullable;
  - this applies recursively, through nested objects and array items.

  The conductor's own shape check (`_shape_ok`) still checks only the original `required` keys. When Codex fails, `AgentResult.error` includes Codex's own error message (the `turn.failed` or `error` event), not only the exit code.
- **R18 Forge never reads its own mail.** Every email Forge sends carries the header `X-Forge-Outgoing: 1`. The inbox reader reports it as `"outgoing": True`, and the conductor ignores every message marked outgoing. A reply is also cleaned before use: quoted lines (starting `>`) and everything from `On … wrote:` onward are dropped, and the result is capped at 2000 characters.
- **R19 Nothing grows without bound.** Every entry stored in a task's `notes` or `trouble_notes` is capped at 2000 characters, and each list keeps only its last 30 entries. The body of every email Forge sends is capped at 20000 characters.
- **R20 Mail budget.** All outgoing email goes through one method, `_send(subject, body) -> bool`. It sends at most `mail_per_hour` (default 6) and `mail_per_day` (default 30) emails, counted in `state/mail_log.json`. Over budget, nothing is sent: `_send` returns False, a question stays undelivered and is retried later, and the budget hit is logged once per window.
- **R21 KILL means everything stops.** `step()` checks KILL before anything else, including the inbox, and returns `"killed"`. No email is read or sent while KILL is set.
- **R22 The start email is rare.** "conductor started" is sent only when KILL is absent, and at most once every 12 hours (the time of the last one is kept in `state/notices.json`). The same once-per-12-hours rule covers the "keeps hitting an error" email and the smoke-test failure email (R23).
- **R23 Live smoke test before running.** `smoke(team, workdir) -> list[str]` runs each role once for real, on a tiny throwaway git repo, and returns a list of problems (empty means all passed):
  - the roles that write files (test writer, builder, planner) must create `smoke.txt`;
  - the read-only roles (reviewer, drift keeper) must not change anything;
  - every role must return JSON that passes its schema.

  `main run` runs the smoke test when `state/smoke_ok.json` is missing or more than 24 hours old. If the smoke test fails, the conductor logs the problems, emails Ben (R22 rate), and exits without starting the loop. `python -m core.bootstrap smoke` runs it on demand and prints the result.

## Review round 1 amendments to R17–R23 (Codex review, 2026-09-29)

- **R24 KILL silences everything except one halt alert.**
  - While KILL is set, `_send` refuses every email except a halt alert: `_send(..., halt=True)`.
  - A halt alert still counts against the mail budget and goes out at most once every 12 hours (notice key `halt`), so Ben always learns that Forge stopped, and why, but never gets a stream of alerts.
  - The tamper path records its question and then delivers it as a halt alert.
  - `_handle_inbox` stops processing the moment a STOP sets KILL.
- **R25 The budget counts attempts.** `_send` records the attempt in `mail_log.json` before calling SMTP, and an attempt that fails still counts. `_notice_once` records its throttle time before sending, too. A retry can therefore never exceed the budget, even if SMTP failed after the message went out.
- **R26 Self-mail is rejected three ways, and Ben's read status is never touched.**
  1. The `X-Forge-Outgoing` header.
  2. Every email Forge sends gets its own `Message-ID`. The last 500 are kept in `mail_log.json`, and any incoming message with one of those IDs is ignored.
  3. The inbox reader keeps the IDs of the messages it has already handled in `state/inbox_seen.json`. On its first read, when that file is missing, it records every current Forge message as handled and returns nothing. So no email from before the first start (the incident's included) can ever count as an answer.

  The inbox reader searches the last 3 days for subjects containing `[Forge` (and for a subject of just `STOP`). It uses peek-only fetches, so it never changes Ben's read or unread flags. It fetches headers first, and then bodies only for new messages that aren't Forge's own.
- **R27 STOP means Ben wrote "stop".** KILL is set when the subject, with any `Re:` or `Fwd:` prefixes removed, is exactly `stop`, or when the cleaned reply (quoted text removed) contains the word `stop`, unless it is negated ("don't stop", "do not stop", "never stop"). Stopping when Ben didn't mean it is the safe way to fail: pressing Start Forge undoes it. STOP works as a reply to any Forge email, including the notices.
- **R28 Every stored thing is bounded.**
  - Question subjects are capped at 300 characters and bodies at 20000 before they are stored.
  - `questions.json` keeps every open question and the 50 most recent closed ones.
  - Queue-level `notes` follow the R19 caps.
- **R29 The smoke test is guarded like real work.**
  - `smoke(team, workdir, call=None)` runs each role through `call(role, prompt, schema, cwd)`. `main` passes the conductor's `_call` (which now takes a `cwd`), so every smoke run gets the fail-closed checks, the tamper guard (KILL plus a halt alert), metering and a run record.
  - The smoke test doesn't start if the token cap has been reached.
  - Each role gets a fresh throwaway repo.
  - A role passes only if:
    - its answer passes full schema validation (`agents.schema_ok`: types, enums, required keys, nested items);
    - a writer role produced `smoke.txt` as a regular file containing `ok`, and nothing else changed;
    - a read-only role changed nothing: git `HEAD` and the file list both match the starting snapshot.
  - Git errors count as problems, and a folder that can't be cleaned up is reported.
  - Before each attempt, `smoke_ok.json` is deleted, and it is written again only on full success.
  - After a failed attempt, `smoke_fail.json` holds the time, and watchdog restarts skip the smoke test (and don't start the loop) for the next 30 minutes.
- **R30 `strict_schema` is complete for the forms Forge uses.** An optional field that has an `enum` also gets `null` added to the enum. `anyOf`, `$defs` and `definitions` are converted recursively.

## Review round 2 amendments (Codex review, 2026-09-29)

- **R31 STOP is checked before anything runs.**
  - Every `main run` start that doesn't find KILL reads the inbox first (`_handle_inbox`: it applies STOP and answers, and retries undelivered questions within budget), then checks KILL again.
  - Only after that may the smoke test or the loop start. So a STOP reply to any notice, the smoke-failure notice included, is honoured on the next watchdog start.
- **R32 A halt alert isn't lost.**
  - A question asked with `halt=True` is stored with `"halt": true`.
  - Every `main run` start that finds KILL does nothing except retry undelivered halt questions (`_retry_halts`), within the budget and the 12-hour halt throttle, and then exits.
  - The halt throttle is recorded only when an attempt actually goes ahead, after the budget check passes.
- **R33 More quoting styles are removed.** `clean_reply` first turns CRLF and lone CR line endings into LF (real email bodies use CRLF), then cuts everything from the first of these lines onward:
  - an `On … wrote:` line, including when it wraps over two lines;
  - `-----Original Message-----`;
  - a line of 10 or more underscores;
  - a line starting with `From:` that is followed within the next 4 lines by `Sent:`, `Date:` or `To:`.
- **R34 Null optional fields count as missing.** Before the shape check, an agent's answer drops any key whose value is `null` and which isn't in the schema's `required` list, recursively. Nullable optionals from Codex's strict schema then validate, and the conductor sees them as absent.

## Review round 4 amendments (Codex review, 2026-09-29)

- **R35 No reply is lost.**
  - When a STOP ends inbox processing, the rest of that batch is saved to `state/inbox_pending.json`. It is processed first on the next `_handle_inbox`, which only runs once KILL is cleared.
  - Each message is handled in its own `try`: if one message raises an error, the error is logged and processing continues with the next.
  - Pending messages follow the R19 caps: at most 50 are kept, and each body is capped.
- **R36 One bad email can't block the inbox.** The reader handles each message separately:
  - a message is recorded as seen only after its body has been fetched and decoded, or after it has been fetched and found malformed (then it is skipped for good);
  - a failed or empty fetch (a network problem) doesn't mark the message seen, so it is retried on the next read, and the reader moves on to the next message;
  - an unknown charset falls back to UTF-8, replacing bytes it can't decode;
  - progress (`inbox_seen.json`) is saved even when one message fails.

## Review round 7 amendment (Codex review, 2026-09-29)

- **R37 The token cap is checked before every agent launch.**
  - `_call` raises `Capped` before launching when that agent's provider is at or over its daily cap. Nothing is launched and no run record is written.
  - `step()` returns `"capped"`. The attempt is undone cleanly and never counts as a failure:
    - **test writer or planner capped:** the worktree is reset;
    - **builder capped:** the ledger claim is released and the worktree is reset to the tests commit;
    - **reviewer capped** (after the work was submitted): the auditor fails the run and the manager reopens the contract, exactly as a failed judge would, but no failure note or signature is recorded, `fails_since` is unchanged, and the task stays `tests_ok`;
    - **troubleshooter or drift keeper capped:** nothing changes, and they run when the cap resets.
  - **smoke test:** a `Capped` stops the smoke run with the problem "token cap reached", and `smoke_ok.json` is not written.

## Review round 8 amendments (Codex review and live smoke run, 2026-09-29)

- **R38 Deferred troubleshooting is never lost.** When the troubleshooter is capped (R37), the task records `troubleshoot_pending` with the failure's reason and the last 4000 characters of its output. Before any further builder attempt on that task, the pending troubleshooting runs first; that step does nothing else. `troubleshoot_pending` is cleared only when the troubleshooter has actually run.
- **R39 A smoke folder that can't be deleted yet doesn't block the start.** On Windows, an agent's child process can briefly keep its folder busy. Deleting a smoke folder is retried 5 times, 2 seconds apart. If it still fails, the folder is logged and left in place, and it is not counted as a smoke problem. Each smoke test first sweeps away any `forge-smoke-*` folders left over from earlier runs (best effort).

## Review round 9 amendment (Codex review, 2026-09-29)

- **R40 While paused, nothing launches.** On a `main run` start, after the R31 inbox read:
  - **If PAUSED is set:** the conductor only waits. It reads the inbox every minute (answers can clear the pause), writes its heartbeat, and runs no smoke test and no agents.
  - **When the pause clears:** it goes on to the smoke test (if stale) and then the loop.
  - **If KILL appears** while waiting, it exits.

## First-cycle amendment (2026-09-30)

- **R41 Plans carry complete tasks, and retries learn.** In the first live cycle the 1B plan was rejected twice for the same reason: its tasks carried short labels, not the instructions the test writer and builder need. Now:
  - **The planner is told** that each task's `section` is the only instruction the test writer and builder will see. It must be complete and self-contained: what to build, exact interfaces, behaviour, edge cases, dependencies on earlier tasks, and the acceptance criteria the tests must check. Each task must also be fully doable by a builder that may change only its `files_in_scope`: no steps for Ben, the conductor, or files outside that scope.
  - **Thin tasks are rejected by plain code** before any review. The plan is rejected if a task's `section` is shorter than `MIN_SECTION_CHARS` (600), with a reason naming the task.
  - **A retry sees why the last attempt failed.** Each planner attempt after the first includes the rejection reasons from earlier attempts (the task's last 3 notes, capped).

## Live-use amendment (2026-09-30)

- **R42 Stopping Forge during an agent run is a stop, not tampering.** Ben's "Stop Forge" shortcut (or a STOP email handled by another start) writes `KILL` into `state/` while an agent may be running. Before this rule, the after-run fingerprint saw the new file and raised a false tamper alarm. Now:
  - **A stop:** if the only differences between the before-run and after-run fingerprints are `KILL` and/or `PAUSED` **appearing** (absent before, present after), the run is a stop. `_call` meters the run's tokens, logs `stop requested during <role> run <run_id>`, and raises `Stopped`. No tamper alert is sent and no halt question is created.
  - **`Stopped` is a subclass of `Capped`.** So every stage undoes the attempt exactly as it does for a cap (R37/R38): worktree reset, claim released, nothing counted as a failure, and the run's result is discarded.
  - **`step()`**, on a `Capped` (including `Stopped`) from a stage or the drift check, returns `"killed"` if `KILL` exists, else `"paused"` if `PAUSED` exists, else `"capped"`.
  - **The smoke test:** a `Stopped` gives the problem `stopped during the smoke test`, not "token cap reached".
  - **Still tampering:** any other changed file, or `KILL`/`PAUSED` being **removed** or **changed** (present before the run), is still tampering (R9). An agent can never un-stop Forge.
## Live-use amendment (2026-09-30, token metering)

- **R43 The token caps count what a run really costs: cached input is weighted at one tenth.** In the first live cycle, a single planner run was metered at about 4.6 million Claude tokens. Nearly all of it was cache reads of the same context, which both providers bill at about a tenth of fresh input. Counting them in full would cap Forge for the rest of the day after two plans. The caps (`claude_daily_token_cap`, `codex_daily_token_cap`) are unchanged. What is counted:
  - **Claude** (`parse_claude`):

    `input_tokens + output_tokens + cache_creation_input_tokens + cache_read_input_tokens // 10`
  - **Codex** (`parse_codex`, per `turn.completed`):

    `(input_tokens - cached_input_tokens) + cached_input_tokens // 10 + output_tokens + reasoning_output_tokens`

    `cached_input_tokens` is clamped to `0..input_tokens`; missing fields count as 0.
  - **A usage field that isn't a non-negative number** (text, negative, missing) counts as 0 everywhere, so a bad field can never cancel real usage. A bad cached count therefore counts all input as fresh.
  - Everything else about metering is unchanged (R6/R37).
