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
  - R49 extends this: no launch while a stop flag is set, and a running agent is killed when one appears.

## Live-use amendment (2026-09-30, token metering)

- **R43 The token caps count what a run really costs: cached input is weighted at one tenth.** In the first live cycle, a single planner run was metered at about 4.6 million Claude tokens. Nearly all of it was cache reads of the same context, which both providers bill at about a tenth of fresh input. Counting them in full would cap Forge for the rest of the day after two plans. The caps (`claude_daily_token_cap`, `codex_daily_token_cap`) are unchanged. What is counted:
  - **Claude** (`parse_claude`):

    `input_tokens + output_tokens + cache_creation_input_tokens + cache_read_input_tokens // 10`
  - **Codex** (`parse_codex`, per `turn.completed`):

    `(input_tokens - cached_input_tokens) + cached_input_tokens // 10 + output_tokens + reasoning_output_tokens`

    `cached_input_tokens` is clamped to `0..input_tokens`; missing fields count as 0.
  - **A usage field that can't be read as a finite non-negative number** counts as 0 everywhere: non-numeric text, negative, infinite or NaN, true/false, or missing. Numbers and numeric strings ("1000") are read normally. So a bad field can never cancel real usage, and a bad cached count counts all input as fresh.
  - Everything else about metering is unchanged (R6/R37).

## Live-use amendment (2026-09-30, plan review)

- **R44 The plan reviewer blocks only for blocking problems; its detailed notes travel with the tasks.** In the first live cycle, every plan was rejected for reasonable edge-case details: a new set each attempt. Two rejections block a plan task, so planning stalled although the plans were sound. Now:
  - **The plan review uses the schema `S_PLAN_REVIEW`:**
    - `verdict`: `pass` or `fail`;
    - `reasons`: a list of strings;
    - `task_notes`: optional, a list of `{task, note}`.
  - **The reviewer is told to fail ONLY for blocking problems:**
    - a requirement of this plan task that no task covers;
    - a task that contradicts `docs/DECISIONS.md` or the design;
    - a task that can't be done within its `files_in_scope`;
    - wrong ordering or dependencies between tasks;
    - placeholders or thin sections.

    Edge cases, extra tests and implementation details go in `task_notes` against the task they affect. Those are not a reason to fail.
  - **On a pass:**
    - Each note whose `task` matches a returned task id is appended to that task's `section`, under the heading `REVIEWER NOTES (handle and test these):`, one `- ` line per note.
    - Notes whose `task` matches no returned task are appended to every task under `PLAN-WIDE REVIEWER NOTES:`.
    - Empty notes are dropped. Each note is capped at `NOTE_CAP` characters, and each task gets at most 10 notes (the first 10).
    - The notes are also appended to the plan file under `## Reviewer notes` in the same plan commit.
  - **On a fail:** unchanged (the reasons become the rejection note, R41).
  - **The reviewer sees the whole plan.** In the first cycle the tasks JSON was cut at 20,000 characters, so the reviewer rightly rejected a "truncated" plan. Now the plan file and the tasks JSON are passed in full. If their combined length exceeds `PLAN_REVIEW_MAX` (200,000 characters), code rejects the plan before any review, with `plan rejected: plan too large for review (<n> characters); split this plan task`.
  - **Build reviews** (`S_REVIEW` after a build) are unchanged.

## Live-use amendment (2026-09-30, plan attempts)

- **R45 Plans get three attempts, and the planner checks itself against the rules first.** Under R44, plan reviews converged to a single blocking reason per attempt. Each attempt fixed the last reason, but a new contradiction with an existing rule surfaced each time. With only two attempts, plan tasks still blocked. Now:
  - **`PLAN_ATTEMPTS = 3`:** a plan task is blocked after its third rejection, not its second. Test-writer rejections are unchanged (2).
  - **The planner is told** to check every task against the numbered rules in `docs/specs/bootstrap-conductor.md` and the decisions in `docs/DECISIONS.md` before answering. The prompt says that any contradiction with them will be rejected as blocking.

## Live-use amendment (2026-09-30, metering failed runs)

- **R46 A Claude run that times out, is killed, or returns unreadable output is still metered.** Before this rule, such runs reported 0 tokens. Their real use never reached the caps. On 2026-09-30 about 30% of the day's Claude use came from timed-out or killed runs. Now:
  - **Session id:** `ClaudeAgent.run` starts every run with `--session-id <uuid4>`, and keeps that id.
  - **Metering from the log:** if the run times out, or its output can't be parsed (`parse_claude` returns 0 tokens with `ok` false), tokens are read from Claude Code's session log instead. The log is `<projects_dir>/*/<session-id>.jsonl`, where `projects_dir` defaults to `~/.claude/projects`.
  - **How the log is counted:** every line whose `message` has an `id` and a `usage`, each message id counted once. The R43 formula applies:

    `input_tokens + output_tokens + cache_creation_input_tokens + cache_read_input_tokens // 10`

    Unreadable lines are skipped. A missing log counts as 0.
  - **The result:** the returned `AgentResult` keeps its failure (`ok` false, the same error) but carries those tokens, so `_call` meters them as usual (R6).
  - **Codex** reports usage only on a completed turn and keeps no session log under `--ephemeral`. A timed-out Codex run stays unmetered: a known gap, noted in STATUS.

## Live-use amendment (2026-09-30, planner memory)

- **R47 The planner sees every earlier rejection of its plan task, not just the last three notes.** P1B2's attempts regressed: problems fixed two attempts earlier came back, because only the last 3 notes mentioning "plan" were shown, and reopen notes pushed real rejections out. Now:
  - **The planner prompt** includes every note of the task that starts with `plan rejected` or `plan review failed`, oldest first, under `YOUR EARLIER ATTEMPTS WERE REJECTED FOR (fix ALL of these; none may come back):`.
  - **Limits:** at most the last `PLAN_MEMORY_NOTES` (10) such notes, and at most `PLAN_MEMORY_CHARS` (12,000) characters in total. When over, the oldest notes are dropped first.
  - **Other notes** are not shown under that heading: reopen notes, Ben's replies, git errors.
  - **Notes are kept longer:** the task's notes list keeps its last `NOTES_KEEP` (30) entries as before (R19), so 10 rejections are always available.

## Live-use amendment (2026-09-30, provider limits)

- **R48 A provider's own usage limit is a pause, not a failed attempt.** At 10:55 UTC on 2026-09-30, Ben's Claude plan hit its session limit ("You've hit your session limit · resets 6am"). Every planner call then failed within seconds. The conductor counted each one as a plan rejection, so P1D and P1E used up all 3 attempts and were blocked in under a minute. Now:
  - **Detection:** after an agent run, `_guarded_run` checks for a provider limit. If the result failed and its error matches the limit detector, the run is a limit hit. The detector is case-insensitive: `(session|usage|rate)[ _-]?limit`, `limit (reached|exceeded)`, `quota exceeded` or `too many requests` (R50 widens it and moves it to `core.usage`).
  - **On a limit hit:**
    - The run's tokens are metered.
    - `state/holds.json` records `{provider: <until>}` in ISO form (the reset time the message names since R50; otherwise now + 30 minutes).
    - `limit hit for <provider>; holding until <time>` is logged.
    - `Capped(provider)` is raised, so the stage undoes the attempt exactly as for a token cap (R37): nothing is counted as a failure or a rejection.
  - **While a hold is active** (`now < until`): the provider counts as capped, both in `_capped()` and in `_call`'s pre-launch check (R37). No agent of that provider is launched. An expired or unreadable hold entry is ignored.
  - **The token cap check (R37)** is unchanged.

## Layer 1D amendments: the always-on service (2026-09-30)

Plan: `docs/superpowers/plans/2026-09-30-layer-1d.md`. Module: `core/service.py`. Layer 1D numbered these rules R41–R45 on its branch; they collided with the live-use amendments above and are renumbered R49–R53. Its mid-run kill and limit holds were unified with R42 and R48 (one mechanism each).

- **R49 A stop works mid-cycle (extends R42).** KILL or PAUSED is checked before every agent launch (`_call` and `_guarded_run`, readiness probes included), not only at the start of `step()`: if either is set, nothing is launched and `Stopped` is raised. A real agent is given the stop check (`should_stop`), and `launch` polls it every 2 seconds; when a stop flag appears, the agent's process tree is killed and the run reports `agent stopped: …`, which `_guarded_run` turns into `Stopped` (never a failure, even if the flag was cleared meanwhile). The after-run rule is R42's: only `KILL`/`PAUSED` appearing is a stop; any other change, or a flag removed or changed, is still tampering (R9). There is one exception class, `Stopped` (a `Capped`), so every stage undoes the attempt exactly as for a cap (R37). `run()` returns `"killed"` right after any step that ended with KILL set. A stop during the smoke test gives `stopped during the smoke test (<flags>)` and is not a smoke failure (no `smoke_fail.json`).
- **R50 Limit windows are holds (extends R48).** One detector (`core.usage.limit_hold_until`) reads the failed run's error and text. It matches R48's wording (`(session|usage|rate)[ _-]?limit`, `limit (reached|exceeded)`, `quota exceeded`, `too many requests`) and Layer 1D's (`hit your … limit`, `weekly limit`). The hold lasts until the reset the message names ("resets 6am (America/Chicago)"), or 30 minutes (R48's hold) if it can't be read, always between 5 minutes and 24 hours. Holds live in `state/holds.json`, written only through `Meter.hold` (a later hold extends an earlier one, never shortens it). `Meter.over` is true while held, so `_call`'s pre-launch check and `_capped()` both see it.
- **R51 Launches per day are capped.** `limits["agent_runs_per_day"]` caps all providers by the number of agent launches today (UTC), counted from `state/runs/`.
- **R52 The service.** `main run` starts a heartbeat thread writing `state/service/heartbeat.json` (outside `state/bootstrap/`, so it never trips R14) and runs the loop through `service.Service.serve()`: sleeps end within 2 seconds on KILL or on `state/service/WAKE`; while Ben is active (input in the last 10 minutes, or unknown) there is a pause of `active_step_gap_s` (30 s) after every work step; `state/service/status.json` is written after every step. One step running longer than `step_stall_s` (4 hours) kills the process tree and exits, so the task restarts it and crash recovery resumes.
- **R53 Watchdog.** The "Forge watchdog" task runs `python -m core.service watchdog` every 5 minutes. KILL set: nothing. Heartbeat fresh: nothing. Stale heartbeat and the conductor lock free: `schtasks /Run`. Stale heartbeat and the lock held: `schtasks /End` then `/Run`. A lock held with no heartbeat file is left alone.

## Layer 1E amendments: Ben's channel (2026-09-30)

Plan: `docs/superpowers/plans/2026-09-30-layer-1e.md`. Code: `core/channel.py`, `core/status_page.py`, and additive hooks in `core/bootstrap.py`. R17-R40 all still hold; every email still goes through `_send`.

- **E1 Owner check is exact.** A message counts as Ben's only when its `From` holds exactly one address and that address is `owner_email`. Ben's address in a display name (`"ben@..." <someone@else>`) or beside another address is someone else.
- **E2 Queue view.** Every write of `questions.json` also rewrites `state/queue.jsonl`: one line per open question with `id, kind, question, default, deadline, status, via, code`. `questions.json` stays the source of truth.
- **E3 Answer drop folder.** `<state>/../channel/in/*.json` (`{qid, code, answer, source}`) is read at the end of `_handle_inbox`, never while KILL is set. Each answer is checked exactly like an email reply (open question, matching code). A STOP creates KILL and the remaining answers are kept. The folder is outside `state/`, so the status page can write to it during an agent run without tripping the tamper check. This is also the interface point for a future Dot bridge.
- **E4 Digest and quiet hours (only when `limits` has `digest_hour`).**
  - `_ask`: kinds outside `channel.INSTANT_KINDS` (D-023) are held for the digest; every question body ends with "If you don't answer: <default>".
  - Quiet hours (`quiet_start` to `quiet_end`, default 23 to 7, local time): `_send` refuses everything except a halt alert, without counting it; `_notice_once` waits without recording its throttle.
  - `_channel_tick()` (each step, after the inbox): at most one digest per local day at or after `digest_hour`, only when a question is open or task statuses changed; plus an early digest, at most every 12 hours, when no task can run and a held question hasn't been in a digest yet. Each attempt is recorded in `digest.json` before sending and retried at most hourly.
- **E5 Status page** (`python -m core.status_page`): binds 127.0.0.1 only, refuses a foreign `Host` and cross-site POSTs. Stop writes `state/KILL`; answers go to the drop folder. A Stop pressed while an agent runs is a clean stop (R42/R49), never tampering.

## Layer 1 integration amendments (2026-09-30)

Fixes made when 1B, the R42–R48 amendments, 1C, 1D and 1E were merged into one branch.

- **R54 A reviewer that can't run never costs an attempt (Codex review P1).** If the reviewer raises `NotReady` or `Capped` (a cap, a hold or a stop) after a build was submitted, or while it reviews a blocker claim, the conductor applies the ledger action `withdraw` (core only, from `claimed` or `submitted`) and resets the task worktree. `withdraw` returns the contract to `open` without counting an attempt and without a false claim, and drops that attempt's commit and run report, so they can never back a later pass. Before this, the build review applied `fail`, so a Codex outage counted attempts and false claims and eventually parked contracts. Task selection also checks the reviewer: a build task's builder is not started while the reviewer's capabilities lack usable evidence (the task records them in `waiting_on`). Task selection and the capability-email hold (`_capability_mail_check`) use the same predicate (`_stage_unready`), so a build waiting only for the reviewer is not counted as runnable progress and the capability email goes out.
- **R55 A finalization is progress only if its next phase can run (Codex review P1).** An active merge-journal record whose next phase is a merge candidate that still awaits review (`candidate` phase, candidate state `creating`, `created` or `judged`) needs the reviewer as well as git (and GitHub with push on). While any of these lacks usable evidence, the finalization waits: `step()` goes on to other runnable tasks instead of returning `not_ready` every cycle, `_capability_mail_check` no longer counts it as progress (so the capability email goes out), and the 1E early digest doesn't count it as runnable work.
- **R56 Line endings never change what Forge reads or restores.** The repo's `.gitattributes` keeps text files LF (`* text=auto eol=lf`). Role files are read with CRLF and CR normalized to LF. The empty-implementation check (R4) writes back the exact bytes it read from each stubbed module, never git's checkout (which writes CRLF under `core.autocrlf=true`).
- **R57 One stop, one hold (R42/R49, R48/R50).** There is one stop mechanism: the `Stopped` exception (a `Capped`), checked before every launch, with a running agent's process tree killed within about 2 seconds of KILL or PAUSED appearing; after the run, only KILL/PAUSED appearing is a stop. Every way to stop Forge writes `state/KILL` in place and nothing else in `state/` (`core.service.request_stop` no longer writes a `KILL.tmp` beside it), so a Stop pressed mid-run is never a tamper alarm. There is one hold mechanism: `core.usage.limit_hold_until` and `Meter.hold` (`state/holds.json`), holding until the parsed reset time, or 30 minutes (R48's length) when none can be read.

## Live-use amendment (2026-10-01, gate run)

- **R55 Judges fit the PC, and tasks wait for their dependencies.** In the first Phase 1 gate run, every build failed its judges: the full core suite run sequentially takes about 20 minutes on Ben's PC, but judges were limited to `test_timeout_s` (600 s). And T1Gb was built before T1Ga, whose code it needs, had merged. Now:
  - **The judges** run `python drills/run_drills.py` and `python -m core.suite`. The suite runs every `tests/core` module in its own process, several at once, and fails if any module fails or times out. Judge commands have their own limit, `judge_timeout_s` (default `JUDGE_TIMEOUT_S`, 2400 s). Task tests keep `test_timeout_s`.
  - **Dependencies:** a plan's tasks may list `depends_on` (ids from the same plan). A task is not picked, for tests or build, until every task it depends on is `done`. Waiting on a dependency is not a capability wait: no `waiting_on` entry and no capability email.

## Live-use amendment (2026-10-01, judge speed and layer freshness)

(Numbering note: the Layer 1 integration amendments above also hold an "R57 One stop, one hold". It stands unchanged; R57 below is the fast-judge rule.)

- **R57 Fast per-task judges; the full suite before main (amends R55).** On Ben's PC the full parallel suite takes about 19 minutes, too slow to run for every task. Now:
  - **Per-task judges** (the build stage's judges and the merge-candidate judge) run each `-m core.suite` judge command as `python -m core.suite --changed <base>..<sha>`, plus `--include <file>` for each of the task's own `tests/core/test_*.py` files (`task_judge_cmds`). The build judge's range is the task's base (the layer tip it started from) to its commit; a merge candidate's is the candidate's base to the merge commit. Other judge commands (`python drills/run_drills.py`) are unchanged.
  - **`--changed` selects** the union of:
    - every `tests/core` module that imports a changed module, directly or through other repo modules (a static `ast` scan of the repo: absolute, relative and sibling imports, parent packages, and repo module names written as strings such as `-m core.x`);
    - test modules the range itself changed, and the `--include` files;
    - every module listed in `tests/core/FAST_MODULES.txt` (one name per line, `#` comments). `python -m core.suite --write-fast` runs every module once and rewrites the list with those that passed in under 20 s.
  - **The full suite runs instead** when the range touches `core/bootstrap.py`, `core/ledger.py`, `core/agents.py`, `core/protect.py` or anything under `drills/` (high blast radius), when it changes a non-Python file outside `docs/`, `.github/` and Markdown that no repo source names (its readers are unknown), or when the range can't be read. A non-Python file that some source names counts as a change to those modules.
  - **The full suite always runs** at the layer gate: before the push and the pull request to main, every judge command runs as written, at exactly the layer tip, in a throwaway worktree, once per tip (`queue.json` `gate_suite`). If it fails, nothing is pushed and no pull request is opened; the failure is logged and told to Ben once per tip, and the gate runs again when the tip changes. CI runs the full suite as before.
- **R58 Layers stay current with main.** On 2026-09-30 a layer branch stayed on old main code and its judges failed because new core modules were missing. Now, at the start of each `step()` (after crash recovery, before capability routing and work):
  - **Only when nothing is mid-stage:** no active merge-journal record, no `tests_ok` task whose contract is `claimed` or `submitted`, no task worktree with uncommitted work, and a clean layer worktree. Waiting for that never uses up the fetch window.
  - **Rate limit:** `git fetch origin main` runs at most once every `MAIN_SYNC_EVERY_S` (600 s), recorded in `state/main_sync.json` (`fetched_at`). No `origin` remote: nothing happens.
  - **When `origin/main` has commits the layer lacks,** it is merged into the layer worktree: `--no-ff`, author and committer Forge, message `Sync <layer> with main`. The sync merge and main's own merge commits are recorded in `approved_merges.json` (kinds `main_sync` and `main`) so the safe push accepts them. Then the layer is pushed (with push on) and the sync is logged. Task worktrees created afterwards start from the new tip.
  - **On a conflict** the merge is aborted and the worktree reset to its old tip; the layer keeps building on its old base. It is logged and Ben gets ONE `merge` question (`sync: true`, through `_ask`); while it is open no second one is asked. The same main and layer tips are never merged again while that question is open; Forge retries after Ben answers or when main or the layer moves. A later clean sync closes the open sync question.
  - **Never fatal:** a git error is logged and retried at the next fetch window; the step goes on.

- **R59 A task's judges skip tests of layer tasks not built yet** (found by Forge's own troubleshooter in the gate run, 2026-10-01). Stage A writes and commits a task's acceptance tests before earlier tasks merge, so the layer branch holds tests whose code doesn't exist yet. The suite judge therefore failed every earlier task, every time. Now each per-task suite judge gets `--exclude <file>` for every test file of another build task in the layer that isn't `done`. A task's own test files are never excluded. A module that isn't excluded still fails the suite as before. The layer gate, where every task is done, and CI run everything.

## Lanes amendment (2026-10-01)

Module: `core/lanes.py`, with hooks in `core/bootstrap.py`, `core/usage.py`, `core/service.py`, `core/status_page.py` and `scripts/start_conductor.ps1`. Tests: `tests/core/test_lanes.py`. This amendment was requested as "R56"; R56 and R57 were already taken above, so it is numbered **R60**.

- **R60 Lanes: parallel conductors with shared caps.** Within one conductor agents can't run at the same time: the after-run tamper check (R9/R14) would see the conductor's own writes for another task. A lane is a separate conductor process with its own state, so lanes run side by side and each lane's tamper check sees only its own state.
  - **Command line:** `python -m core.bootstrap <cmd> --lane NAME`. The default lane, `main`, keeps today's paths.
    - **State:** `state/bootstrap/` for main, `state/lanes/<NAME>/` for every other lane: its own lock (R11), queue, questions, runs, capabilities, ledger and notices.
    - **Worktrees:** `Forge-work/` for main, `Forge-work/lanes/<NAME>/` for the others.
    - **Layer branch:** set per lane by `init --layer`. `init` refuses a layer branch that another lane's queue already uses, and adds the lane to `state/lanes.json` (a list of names; main is implied).
    - **Names:** a lowercase letter, then up to 23 lowercase letters, digits or `_`. No `-` (a question id's lane prefix must be unambiguous). Question kinds, `main` and the state and service folder names are reserved.
  - **Shared by every lane, in `state/shared/`:**
    - the token meter (`meter.json`) and holds (`holds.json`), through `Meter(shared, lock=…, runs_dirs=…)`;
    - the runs-per-day count (R51), counted over every lane's `runs/` folder;
    - the mail log and budget (`mail_log.json`, R20/R25): `_send` reserves a send in one locked read-modify-write;
    - `inbox_seen.json` (R26);
    - the global `KILL`.
  - **Locking:** every read-modify-write of a shared file holds `state/shared/shared.lock` (`msvcrt.locking` on Windows, `fcntl.flock` elsewhere, non-blocking with retries, `TimeoutError` after 60 s). Writes go to a `.tmp-*` file beside the target and are replaced atomically. On Windows a replace is retried while a reader has the file open.
  - **Migration:** on any `core.bootstrap` start, if `state/shared/meter.json` doesn't exist, the main lane's `meter.json`, `holds.json`, `mail_log.json` and `inbox_seen.json` are copied into `state/shared/` (the meter last, as the done-marker). The old copies are left in place. Each later start raises the shared meter to the old file's count for any day and provider where the old one is higher, so an older conductor that kept writing the old file can't reset a cap. A count is never lowered.
  - **KILL:**
    - **Global KILL** (`state/shared/KILL`) stops every lane: `step()`, `_send`, the service sleep, the watchdog and R49's mid-run stop all check it.
    - **Lane KILL** (`<lane state>/KILL`) stops one lane.
    - **These write the global KILL** (plus the writing lane's own KILL): a STOP by email or by the status page, the Stop Forge shortcut, `python -m core.service stop` (`--lane NAME` stops one lane), and a tamper alarm in any lane (the lanes share caps and state, so a tamper alarm stops them all).
    - **A global KILL appearing during a run** is a stop under R42, not tampering.
    - **Start Forge** clears every KILL and runs the watchdog, which starts every lane.
  - **Tamper fingerprint (R9/R14/R15, per lane):** a lane fingerprints its own state folder (as before), its own answer drop folder, and every file in `state/shared/` except:
    - the accumulate files (`meter.json`, `holds.json`, `mail_log.json`, `inbox_seen.json`);
    - `shared.lock`;
    - `.tmp-*` replace temporaries.

    Other lanes' conductors write the accumulate files at any time, and only plain conductor code writes them. An agent writing them is the new risk. So instead of a fingerprint they get a **never-decreases check** around every agent run (`lanes.shrunk`). Each of these is tampering:
    - any meter count for any day and provider decreasing or disappearing;
    - a hold shortened or removed;
    - a send younger than 23 hours, or a recent Message-ID, removed from the mail log;
    - a handled Message-ID removed from `inbox_seen.json` while the list is under its cap;
    - a file that was readable becoming missing or unreadable.

    Additions are never tampering.
  - **Not fingerprinted by a lane:**
    - other lanes' state folders, written by their own conductors at any time;
    - `state/lanes.json`, written by Ben's `init`;
    - `state/service/`: heartbeats.
  - **Known limit:** an agent in lane A that edits lane B's state (its queue, its questions) is caught only if lane B has an agent run in progress at that moment (B's own fingerprint). Lane A can't tell an agent's write to B's folder from B's conductor's own writes. The strong fix is the one already planned for Layer 3: agents run as a separate Windows user that can't write Forge's state at all.
  - **Ben's email:**
    - **Only the main lane reads the mailbox.** Other lanes send through the same `_send`, under the shared budget.
    - **Question ids:** a non-main lane's question ids carry its name as a prefix (`p2-blocked-3`).
    - **Routing:** when main reads an owner's reply whose subject names a lane-prefixed question of a listed lane, it doesn't answer it. It appends the reply (from, subject, cleaned body) to `state/bootstrap/routed/<lane>.json` (last 50). That file is in main's own state, written between main's agent runs.
    - **The lane's inbox:** it reads that file, keeps what it has read in its own `routed_seen.json`, and never writes main's folder. It then handles each reply exactly like an email: owner check, question code, kind.
    - **STOP** is handled by main itself and writes the global KILL.
  - **Service and watchdog:**
    - **Heartbeats:** each lane beats in `state/service/` (main, as before) or `state/service/<lane>/`.
    - **Watchdog:** `python -m core.service watchdog` checks main and every lane in `state/lanes.json` (R53 per lane: its heartbeat, its lock, its task `Forge conductor <lane>`; main keeps `Forge conductor`).
    - **`scripts/start_conductor.ps1`** registers one conductor task per listed lane.
  - **Status page:** the usage bars are the shared meter, runs and mail budget (every lane together). A "Lanes" section shows each other lane's layer, current task, flags and queue. Answers on the page are for main's questions; other lanes' questions are answered by email.

- **R61 Each layer is built against its own design.** `init --spec <file>` records `spec_file` in the lane's queue. The drift keeper and the coverage map read that design. Without it they read `limits["spec_file"]`, else the Layer 1 design. A Phase 2 lane therefore drifts against `docs/specs/phase-2-design.md`, not Layer 1's.

## R60 review round 1 (Codex review, 2026-10-01; overrides R60 where they differ)

- **R60a Launch admission is atomic.** Under the shared lock, one transaction does all of this before the lock is released:
  - checks this lane's own accounting and the shared accounting;
  - checks the provider's token cap, holds and runs per day;
  - reserves the launch by creating its run folder, which every lane's runs-per-day count sees.

  `_admit`, called by `_guarded_run`, does this for every launch, probes included. Two lanes can never both take the last run of the day.
- **R60b Shared usage can't be lost or lowered silently.** The single shared `meter.json`, `holds.json` and `mail_log.json` are replaced by one file per lane: `state/shared/meter/<lane>.json`, `holds/<lane>.json` and `mail/<lane>.json`.
  - **Writing:** each lane writes only its own files, under the lock.
  - **Totals:** the meter and the mail budget are summed over every lane's file; a hold is the latest over every lane's file.
  - **No lost updates:** no lane ever rewrites a file that another lane increments, so a concurrent increment can't be overwritten by a stale copy.
  - **Protection:**
    - **Own files** are fingerprinted by their lane, like the rest of its state.
    - **Other lanes' files** may only grow during this lane's agent runs (`lanes.shrunk`).
    - **Expected-content check:** the owning conductor remembers exactly what it last wrote, and checks before every write, every launch and every step (`OwnFiles.verify`). Any difference is a tamper alarm (KILL for every lane and a halt question). An agent that writes back a stale copy of another lane's counter is therefore caught by the lane that owns it, even when the running lane's before/after comparison can't see it.
    - **While the owning lane isn't running,** its file doesn't change, so any decrease is below the running lane's snapshot and that lane catches it.
  - **`inbox_seen.json`** is the main lane's own file: main fingerprints it, and the other lanes check it only grows.
- **R60c Unreadable accounting fails closed.** Shared accounting is read strictly: a missing marker, a missing main meter, unreadable files, invalid JSON, or invalid shapes all count as unreadable.
  - Every `step()` checks it. If it can't be trusted: nothing is launched, `step()` returns `"not_ready"`, the problem is logged, and Ben is asked once (question kind `accounting`, a halt question).
  - `Meter.over` reports capped.
  - A meter or mail file that can't be read is never overwritten.
  - `_send` sends nothing while the mail accounting can't be read.
  - Before a run, an unreadable accounting file means the agent is not launched. After a run, a file that became unreadable is tampering.
- **R60d Migration happens once, with a marker.** The first start copies the old main-lane files into `meter/main.json`, `holds/main.json`, `mail/main.json` and `inbox_seen.json`, then writes `state/shared/migrated.json`. Every start calls `migrate` (the CLI and every `Conductor` with lanes), but a marked folder is never migrated again: a meter missing after that is unreadable accounting (R60c), never a reset. The R60 meter top-up is dropped.
- **R60e `init` never replaces a queue silently.** `init` refuses (exit 2) when the lane's queue still has tasks, unless `--force` is given. The Phase 2 start command is `python -m core.bootstrap init --lane p2 --layer phase-2 --tasks docs/specs/phase-2-queue.json --spec docs/specs/phase-2-design.md`.
- **R60f The mailbox reader outlives main's own stop.** When main is stopped by its own KILL alone (no global KILL) and other lanes exist, main stays up as the mailbox reader (`_mail_reader_only`). It does nothing else:
  - its `step()` reads the mailbox;
  - a STOP still writes the global KILL;
  - replies to other lanes' questions are still routed;
  - replies to main's own questions wait in `inbox_pending.json` until main restarts (R35);
  - nothing is sent and nothing else runs.

  The run loop and the service sleep continue, and the watchdog keeps main running. Only the global KILL stops the mailbox reader.

## R60 review round 2 (Codex review, 2026-10-01; overrides R60 and R60a–f where they differ)

- **R60g Accounting manifest.** `state/shared/accounting.json` lists every accounting file that must exist. Migration registers main's files. A lane registers its own `meter/`, `holds/` or `mail/` file, under the shared lock, before it first writes that file.
  - **Entries are never removed.** A registered file that is missing, unreadable or invalid is unreadable accounting (R60c): every lane fails closed, `Meter.over` reports capped, and `_send` sends nothing. A lost file is never read as zero usage.
  - **Tamper protection:** any lane may add to the manifest at any time, so no lane fingerprints it. Instead it is checked like the other accounting files: during an agent run its entries may only grow.
- **R60h No migration over existing accounting.** Migration happens only when `state/shared` has no accounting at all (no manifest, no `inbox_seen.json`, no file in `meter/`, `holds/` or `mail/`).
  - **The marker:** `migrated.json` is written as `{"state": "migrating"}` before the copy and `{"state": "done"}` after it. The next start finishes an interrupted migration, and no conductor runs until it is done.
  - **A lost marker:** if the marker is missing or unreadable while accounting exists, every lane fails closed (R60c) and nothing is migrated.
  - **Read-only commands never migrate:** `python -m core.bootstrap status`, `core.service status` and the status page.
- **R60i Smoke folders per lane.** `main run` and `smoke` run the smoke test in the lane's own work root.
  - **Folder names:** with lanes they are `forge-smoke-<lane>-<role>-<hex>`.
  - **The R39 sweep** removes only this lane's own folders, and only those older than an hour (`SMOKE_SWEEP_AGE_S`), best effort. Another process's folder is never touched.
- **R60j Task branches per lane.** Main keeps `forge-task/<tid>`. Any other lane uses `forge-lane/<lane>/<tid>` (`Worktrees(branch_prefix=…)`). Preparing, cleaning up, recovering and finalizing a task all go through it.
  - **Why not `forge-task/<lane>/<tid>`:** git can't hold both that and a main task branch `forge-task/<lane>`.
  - **A lane's sweep** removes orphan branches only in its own namespace.
  - **Task ids are unique across lanes:** `init` refuses a task id that another lane's queue already has (exit 2).
- **R62 Planned tasks name the spec requirements they cover** (found 2026-10-01: the drift keeper paused Forge after the gate project's three merges raised coverage by 0 of 40). The plan stage dropped `covers`, so no planned task could ever raise spec coverage, so every planned layer was bound to stall on "no coverage gain in 3 merges". Now:
  - **The planner sees the requirements.** When the layer spec (`spec_file`) has requirements, the planner prompt lists them (`id: text`, from `core.coverage.parse_requirements`) and asks each task for `covers`: the requirement ids the task's acceptance tests prove. `S_PLAN` allows `covers` (a list of strings).
  - **Validation.** Unless the plan task sets `"no_coverage": true`, every returned task must have a non-empty `covers` of known requirement ids with no repeats; otherwise the plan is rejected with `plan rejected: covers ...` naming the task and problem (it counts toward R45's attempts like any rejection). With `no_coverage` (a gate or toy project that implements no spec requirement), `covers` is optional and ignored. With no usable spec, `covers` is not required, and any given is ignored.
  - **The reviewer checks claims.** The plan reviewer is told that a task's `covers` must be backed by acceptance criteria in its section; a claim with no matching criteria is a blocking problem.
  - **Carried into the queue.** Each new build task keeps its validated `covers`, so `core.coverage` credits it once it is verified done.
  - **Stall rule unchanged.** A merged task that claims no requirement still counts as a merge without coverage gain (design 3.7, drill `re-plan triggers`): a gate or toy project (`no_coverage`) is off the layer design and is expected to bring in the drift keeper after 3 merges.

- **R63 Evidence tasks prove work that already exists** (2026-10-01). Most Layer 1 code was built before the coverage ledger existed, through reviewed pull requests rather than ledger contracts, so spec coverage reads 0 of 40 even where the code works. An ordinary task can't credit it: its tests must fail on the current code (R4), and tests of working code pass. Now a planned task may set `"evidence": true` (`S_PLAN` allows it; it is carried into the queue). For an evidence task:
  - **Tests stage.** The test writer is told the code already exists and the tests must prove the task's `covers` against it. The new tests must **pass** on the current code (a real run in which tests executed; a timeout or no tests run is rejected as `tests rejected: no real passing run`), and must still **fail on an empty implementation** of the in-scope modules (the unchanged R4 check, so the tests really exercise that code). A failing run is rejected as `tests rejected: evidence tests fail on the current code` with the output tail in the note, so the planner can re-cut it as an ordinary task that fixes the gap.
  - **Build stage.** The builder is told the tests already pass and to change nothing unless a test or judge fails. An empty diff is submitted at the current commit as usual, then the judges (task tests, drills, suite, mutation testing of the in-scope files) and the reviewer run unchanged. The reviewer is told it is an evidence task: it fails it when the tests don't prove the requirements in `covers`.
  - **Mutation targets.** An evidence task's diff is usually empty, so changed lines can't be the mutation targets. For an evidence task the targets are the changed in-scope lines (if any) plus every line of each in-scope function or method (a `def` in a tracked in-scope `.py` file, test files excluded) whose name appears as a word in the task's test files: the code the tests say they prove. If that selects nothing, every line of the in-scope `.py` files is targeted (so a test that names no in-scope code can't pass on an empty mutation run; with large modules it runs out of budget and fails, which is the honest result). Test files (anything under `tests/` or named `test_*.py`, any task's) are never targeted. A run that generates no mutants fails an evidence task (it proves nothing). The mutation budget, `mutation_min` and the incomplete-run rule are unchanged.
  - **Credit.** A passed evidence task is verified done like any other, so `core.coverage` credits its `covers`.
  - **Planner.** The planner prompt says when to use `evidence`: for requirements the existing code already meets. Requirements it doesn't meet get ordinary tasks.

## R60 review round 3 (Codex review, 2026-10-01; the last round, D-036)

- **R60k A lost accounting folder is not an empty one.** When `state/shared/meter/`, `holds/` or `mail/` is missing, its registered files (R60g) are still checked: a registered file in a lost folder is unreadable accounting (R60c), so `_send` sends nothing and the caps fail closed.
- **R60l Run history that can't be listed fails closed.** For admission (`Meter.over`), a lane's `runs/` folder that exists but can't be listed (permission or other OS error) means the runs per day can't be counted: the provider counts as capped. A `runs/` folder that doesn't exist counts 0 (a lane that never ran). The status page and `core.service status` still show a best-effort count.
  - **Not changed:** a deleted `runs/` folder (or run folders inside it) still lowers the count. Within one lane that is unchanged from before lanes; across lanes it is R60's known limit (an agent in lane A editing lane B's state), whose strong fix is Layer 3's separate Windows user. Durable per-lane run counters were not added in this last round.

- **R64 Live dashboard** (2026-10-01; Ben: "a live dashboard of who's doing what task, estimated time til completion of task, next checkpoint, and completion of project, token usage, progress, visuals of who's doing what, always live, always accurate"). Two parts: a pure data layer `core/dashboard.py` and the status page that shows it.
  - **The data layer.** `core.dashboard.snapshot(forge_root, now=None) -> dict` (standard library only). It reads Forge's files under `forge_root` (`state/`, `docs/progress.json`, `charter/limits.json`, the layer spec) and **never writes anything** (no lock files, no repairs, no caches on disk; an in-memory cache keyed by path and mtime is allowed). It **never raises** on missing, partial or corrupt files: a value it can't read is `None` (shown as "unknown"), and a lane whose files are unreadable still appears. `now` defaults to the current UTC time; every time in the result is an ISO 8601 UTC string and every duration is in seconds. `python -m core.dashboard --json [--root PATH]` prints the snapshot as JSON. The result has `generated_at`, `lanes`, `project`, `tokens`, `timeline`, `stage_medians` and `attempts`.
  - **Lanes.** `lanes` has one entry per lane in `core.lanes.listed(state)` order (main first; main's state is `state/bootstrap`, another lane's `state/lanes/<name>`). Each entry has:
    - `name`, `layer` (the queue's `layer`, or `None`), `open_questions` (the number of open items in its `questions.json`).
    - `conductor`: `{"alive", "heartbeat_age_s"}`. The heartbeat is the service heartbeat (`state/service/heartbeat.json` for main, `state/service/<lane>/heartbeat.json` otherwise; `at` is epoch seconds). Alive means a heartbeat no older than 180 s whose `phase` is not `exited`. With no service heartbeat file, `conductor.heartbeat` (`<pid> <epoch>`) is used, and alive means no older than `agent_timeout_s` + 300 s (it is written only at the start of a step). No heartbeat at all: alive `False`, age `None`.
    - `state`, the first that applies: `stopped` (its own `KILL`, the global `state/shared/KILL`, or the conductor is not alive), `paused` (`PAUSED`), `running` (an agent or the judges are running, below), `capped` (the service's `last_status` is `capped`, or every provider is at its daily cap or on hold), else `idle`.
    - `current`: what is running now, or `None`. A run folder `runs/<YYYYMMDDTHHMMSS>-<role>-<hex>` (UTC stamp; the role is everything between the first and the last `-`, so `probe-claude` is a role) starts at its stamp and ends at its `output.json` mtime. The lane's newest run folder is the running agent when it has no `output.json` and the conductor is alive. An older folder with no `output.json` is an abandoned run (a lane runs one agent at a time, so a newer run means it is over), and so is any such folder while the conductor is down. Otherwise, when the newest finished run is a `builder` run and the conductor is alive, the judges are running: `current` has role `judge`, the builder's task, and starts at the builder's end. `current` is `{"role", "task_id", "task_title", "run_id", "started", "elapsed_s"}`; `task_id` is read from the run's `prompt.md` (the first line `TASK <id>: <title>`), `None` for roles with no task (drift keeper, manager, probes).
    - `tasks`: every queue task except `superseded`, in queue order, each `{"id", "title", "kind", "status", "stage", "eta_s", "eta_basis"}`.
  - **Stages and task ETAs.** A plan task's stages are `planner`, `reviewer`. A build task's are `test_writer`, `builder`, `judge`, `reviewer`, `merge`. A task's current stage is the running role when `current` is this task and that role is one of its stages; otherwise the first stage for its status (plan task: `planner`; build `todo`: `test_writer`; `tests_ok`: `builder`; `merge_pending`: `merge`). `done` tasks have stage `None` and `eta_s` 0; `blocked` tasks have stage `None` and `eta_s` `None` (they wait for Ben). `eta_s` = (the current stage's median minus its elapsed time when it is running, never below 0) + the medians of the later stages + the expected extra attempts. **Attempts:** the judges and the reviewer send a build task back to the builder, so a task usually takes several builder attempts. `attempts` (top level, `{"median", "n", "source"}`) is the median number of builder runs per task id in this machine's history (default 1, `source` `"default"`). A build task in the `test_writer`, `builder` or `judge` stage adds `max(0, attempts.median - c)` x (builder median + judge median), where `c` is the attempt under way: the task's finished builder runs, plus 1 unless the stage is `judge`. `eta_basis` says how it was estimated, e.g. `"estimate: builder 1m (n=23) + judge 6m (n=25) + reviewer 41s (n=14) + merge 10m (default) + 3 more builder+judge attempt(s) (median 5 per task, n=8; 2 so far)"`.
  - **Stage medians.** `stage_medians` maps each stage to `{"median_s", "n", "source"}`, measured from this machine's own run history (every lane's run folders). An agent stage is the median duration of finished runs of that role (positive durations only). `judge` is the median gap between a builder run's end and the start of the next run in the same lane, whatever its role (the judges run after every builder attempt, passed or failed; gaps over 0 and under 6 h). A stage with no samples uses a default (`planner` 900, `test_writer` 600, `builder` 900, `judge` 900, `reviewer` 300, `merge` 600 s) with `n` 0 and `source` `"default"`; otherwise `source` is `"history"`. `merge` has no run of its own and always uses its default.
  - **Checkpoint.** Each lane has `checkpoint`, its layer's completion: `{"layer", "tasks_done", "tasks_total", "spec_file", "covered", "partial", "requirements_total", "requirements", "eta_s", "eta_basis"}`. Tasks count as in the queue (superseded excluded). Coverage is `core.coverage` on the lane's spec (the queue's `spec_file`, else `limits["spec_file"]`, else `docs/specs/layer-1-design.md`, read from `forge_root`): `requirements` lists `{"id", "text", "status"}` (`covered`/`partial`/`open`/`unclaimed`). Only tasks the queue marks done whose contract has a `pass` event in the lane's `ledger/events.jsonl` get credit (read-only; a torn last line is skipped; a broken hash chain gives no credit). With no usable spec the coverage fields are `None` and `requirements` is empty. `eta_s` = the sum of the remaining (not done, not blocked) tasks' `eta_s`, plus, for each unfinished plan task, the tasks it is expected to add (the median task count of this machine's finished planner outputs, default 6) times one whole build task (test writer + `attempts.median` x (builder + judge) + reviewer + merge, with at least 1 attempt), divided by the number of running lanes on that layer (at least 1). `eta_basis` names these numbers and says it is an estimate.
  - **Project.** `project` is `{"phases", "phases_done", "phases_total", "current_phase", "current_fraction", "overall", "eta_s", "eta_basis"}` from `docs/progress.json`. `current_fraction` is main's spec coverage score (`core.coverage` score / requirements) when it has a spec, else main's done/total tasks. `overall` = (phases done + `current_fraction`) / phases. `eta_s` is the current phase's remaining work: the sum of every lane's checkpoint work divided by the running lanes (at least 1). `eta_basis` says it is an estimate and that later phases are not planned yet, so they are not in it.
  - **Tokens.** `tokens` is `{"day", "resets_at", "providers", "runs_today", "runs_cap"}` for the UTC day. `providers` has `claude` and `codex`, each `{"used", "cap", "fraction", "by_lane", "burn_per_h", "time_to_cap_s", "held_until"}`. Usage is read from `state/shared/meter/<lane>.json` when that folder has files (summed; `by_lane` per file), else from `state/bootstrap/meter.json` (lane main). `cap` is `limits["<provider>_daily_token_cap"]`. `burn_per_h` is the tokens of that provider in run `output.json` files that ended in the last hour. `time_to_cap_s` = (cap - used) / burn rate per second (0 when already at the cap; `None` with no burn or no cap). `held_until` is a provider hold that has not ended (`holds.json` or `shared/holds/`), else `None`. `runs_today` counts today's run folders in every lane; `runs_cap` is `limits["agent_runs_per_day"]`.
  - **Timeline.** `timeline` is `{"from", "to", "lanes": {lane: [runs]}}` for the last 12 hours: every run that ended (or is still running) after `from`, oldest first, each `{"run_id", "role", "task_id", "start", "end", "running", "ok", "tokens", "provider"}` (`end` is `None` while running).
  - **Status page.** `GET /api/live` returns the snapshot as JSON (`application/json`, with the page's usual Host check and security headers). `GET /api/live?format=html` returns the live section's HTML, rendered by the same Python code that renders it into the page (one renderer). The page shows the live section first and keeps every existing section and control (Stop, Answer, Approve). The live section shows:
    - one card per lane: who (a role badge in the role's colour) is doing what task, an elapsed timer, and an ETA bar for the stage and the task;
    - the lane's checkpoint (tasks and coverage bars, with ETA) and the project bar (with ETA);
    - a token bar per provider with cap, burn rate and time to cap;
    - a Gantt-style SVG of the last 12 h of runs per lane, with a now line;
    - a requirement coverage grid: one cell per requirement, coloured covered/partial/open/unclaimed.

    Every ETA is labelled an estimate with its basis. Inline CSS and SVG only, no external asset, readable in light and dark. A small inline script (allowed by a per-response CSP nonce, with `connect-src 'self'`) fetches `/api/live?format=html` every 3 s, swaps the live section in place, and ticks elapsed timers every second. Without JavaScript the page still works and reloads every 30 s (a `<noscript>` meta refresh). The page still only reads state (the Stop button's KILL stays the one write).
  - **Review round 1 (Codex, 2026-10-01).**
    - **Readers never break a writer.** On Windows a file that a reader has open for a moment can make the conductor's atomic `os.replace` fail with `PermissionError` (even with delete sharing, the old name stays taken until the reader closes). The dashboard reads each file in one short call with every sharing flag (`FILE_SHARE_DELETE` included) and retries its own sharing violations, and `Conductor._write` now retries a refused rename briefly (`replace_retry`: 6 tries over about 0.4 s) before it fails, which also covers the status page and the tray.
    - **Unreadable is unknown, never zero.** A `queue.json` that exists but can't be parsed gives `tasks_total`, `tasks_done`, `remaining_work_s` and `eta_s` `None` (and the project's fraction and ETA `None` when it is main's); a missing one is an empty queue. Unreadable `questions.json`: `open_questions` `None`. A meter value that is not a finite non-negative number is unknown usage (`None`), never 0 and never a crash.
    - **Stale runs.** A newest run folder without `output.json` that started more than 2 x `agent_timeout_s` + 300 s ago is abandoned (agents are killed at their timeout). Judging is inferred only within 6 h of the builder's end.
    - **Questions stay current.** The live section carries the main lane's open question ids (`open_question_ids`); when they change, the page reloads itself unless Ben is typing an answer, in which case it says so.
  - **Review round 2 (Codex, 2026-10-01).** A queue counts as readable only when `tasks` is a list of objects that each have a string `id` (anything else is unknown, like an unparseable file); a missing `docs/progress.json` or one without a list of phase objects makes every project field `None`; a number too large for a float is unknown usage. The page and the tray show unknown as unknown ("Whole project: unknown", "Tasks unknown"; the tray falls back to reading the files), never as 0%.
  - **Review round 3 (Codex, 2026-10-01; the last round).** The status page's older sections (progress and usage; work, caps and capabilities) are each guarded: a bad state file shows a short note in that section instead of replacing the page, so the live section, Stop, Answer and Approve always render; an unreadable queue shows the old queue bar as unknown. The tray keeps the page's values when some are unknown and shows them as "?" (icon and tooltip); its file fallback treats an unreadable queue as unknown too.
  - **Tray.** `scripts/forge_tray.ps1` reads `/api/live` for its number (the main lane's checkpoint) and tooltip (state, current role and task), and falls back to reading the files as before when the page is down.

- **R65 A failing evidence task goes back to its test writer** (found 2026-10-01: L1C_1's evidence tests killed 23 of 31 mutants, below `mutation_min`). An evidence task's builder may not change tests, and the code already exists, so retrying the builder can never fix weak evidence tests or a review that says the tests don't prove `covers`. Now, when an evidence task's attempt fails the mutation gate or the review:
  - The attempt is recorded as failed in the ledger exactly as before (same `fail` payload, focus and troubleshooter rules).
  - The task then goes back to the tests stage: status `todo`, `tests_commit` cleared, and `test_feedback` holds the reasons: one line per surviving mutant (`file:line original -> replacement`) and the reviewer's reasons. `test_rejects` is reset to 0.
  - The test writer's prompt for that task includes `TEST FEEDBACK` with those lines, and asks for stronger tests that kill them. The writer rewrites only the task's `test_files`; the R63 checks (pass now, fail on an empty implementation) apply again.
  - `test_feedback` is cleared when new tests are accepted. An ordinary task is unchanged.
  - **Bounded:** an evidence task returns to its test writer at most 3 times (`evidence_rewrites`); after that a failure blocks it with the last reason, like any blocked task.
