# Layer 1B part 3: role files, worktrees and safe merge

> Plan task P1B3. Serves `docs/specs/layer-1-design.md` §2 (roles), §3 steps 3, 4 (exact-commit judging), 6 (merge) and 7 (drift after every merge). Parts P1B1 (judges) and P1B2 (readiness) are planned separately; the T1B1 and T1B2 tasks run before these in queue order. Builds on `core/bootstrap.py`, `core/ledger.py`, `core/protect.py` and spec rules R1–R40 in `docs/specs/bootstrap-conductor.md`.

## What the conductor lacks today

| Design requirement | Today | This plan |
|---|---|---|
| Role instructions are protected files | hard-coded strings in `core/bootstrap.py` | T1B3a: `agents/<role>.md`, read only from the main checkout; `agents/` protected in `core/protect.py` and `CODEOWNERS` |
| Per-task git worktrees | every agent works in the single layer worktree | T1B3b: `core/worktrees.py`; the builder works in `work/tasks/<tid>`; judges run in a throwaway worktree at the exact commit |
| Crash-safe ledger | `apply` writes `contracts.json` before the event; `replay()` truncates `events.jsonl` first | T1B3c: event-backed `completion()`, `reconcile()`, torn-tail repair, a crash-safe `rebuild()`, a head marker that makes every `apply` heal first |
| Merge, then finalize, crash-safe | pass → `done` → push (errors ignored) → `drift_due`; nothing journaled | T1B3d: `core/finalize.py`, a durable per-task merge journal; T1B3e wires it in |
| Blocked merges wait for Ben | n/a | T1B3d/e: a blocked journal is never resumed until its question is answered |
| Divergence merges judged and reviewed | n/a | T1B3d/e: a candidate merge commit is built in a throwaway worktree, judged at its exact SHA, reviewed, recorded as approved, and only then moved onto the layer branch and pushed |

## Fixes for the earlier review findings

- **Done too early.** The ledger `pass` (which is what makes a contract `done`) is now the second-to-last finalization phase. It runs only after the journal durably records the push (or push disabled), the `drift_due` mark and the cleanup. The queue's `done` is the last phase. Both are tested across a crash at every boundary (T1B3d, T1B3e).
- **Interrupted ledger writes.** `apply` writes the state cache before the event. T1B3c adds a head marker so every `apply` and `reconcile()` detects a cache that is ahead of the event log and rebuilds it from events. Completion is decided only by `Ledger.completion(cid)`, which reads the `pass` event, never `contracts.json`. The rebuild never truncates `events.jsonl`.
- **Merge intent and reconciliation.** Each Git operation is preceded by a durable intent in the journal and followed by a journal save; on restart every phase first reconciles against Git (ancestry checks, `refs/forge/candidates/*`, `git ls-remote`) before acting. Named crash points before and after every Git operation and durable write are tested.
- **Blocked tasks are never retried in a loop.** `Journal.active()` excludes `blocked`; only answering the question (`Journal.unblock(qid)`) makes a blocked journal runnable again, and then it runs once more. One question per conflict; other journals blocked on the same remote state reuse its qid.
- **Unreviewed merges can never be pushed.** Candidates are created and judged away from the layer branch. The layer ref moves to a candidate only after it is recorded in `state/approved_merges.json` with its judge run and review. Every push, from any task or stage, goes through `safe_push`, which refuses if any merge commit not yet on origin is missing from that registry.
- **Final SHA relationship.** The contract's `commit` stays the submitted, judged, reviewed task commit S. The ledger `pass` payload carries `task_commit` (S), `final_sha` (the pushed layer tip containing S) and `merges` (each candidate's SHA, parents, kind, own CI run id, verdict and reasons). The ledger itself checks each merge's CI run. The queue records `done_commit` = S and `final_sha`. A plain fast-forward merge adds no merge commit, so nothing is labelled judged or reviewed except what actually was.
- **Exact-commit judging.** All judge commands for S and for each candidate run in a throwaway worktree checked out at exactly that SHA (T1B3b, T1B3e). P1B1's mutation judge is reused unchanged for S; it is not rerun on a merge commit, which adds no builder-written lines, and this is recorded in the candidate evidence.

## Tasks (in order; each depends on the ones before it)

1. **T1B3a** role files `agents/<role>.md`, `core/roles.py`, protection. Test: `tests/core/test_roles.py`.
2. **T1B3b** `core/worktrees.py`; build attempts in a task worktree, judges in a throwaway worktree. Test: `tests/core/test_worktrees.py`.
3. **T1B3c** `core/ledger.py` crash recovery and merge evidence. Test: `tests/core/test_ledger_recovery.py`.
4. **T1B3d** `core/finalize.py`: journal, approved-merge registry, safe push, finalizer. Test: `tests/core/test_finalize.py`.
5. **T1B3e** `core/bootstrap.py` wiring, blocked handling, end-to-end crash tests. Test: `tests/core/test_merge_pipeline.py`.

The full task sections below are the only text the test writer and builder see.

### T1B3a

TITLE: Role instructions in protected files agents/<role>.md
FILES: agents/*.md, core/roles.py, core/protect.py, CODEOWNERS, core/bootstrap.py
TESTS: tests/core/test_roles.py

Move every role's standing instructions out of `core/bootstrap.py` into protected files, per `docs/specs/layer-1-design.md` §2 and D-025/D-028.

Create `core/roles.py`:
- `ROLE_NAMES = ("test_writer", "builder", "reviewer", "troubleshooter", "drift_keeper", "planner")`.
- `DEFAULTS: dict[str, str]` with one entry per role: the built-in fallback text. Each default starts with the sentence the conductor uses today: `You are the TEST WRITER.`, `You are the BUILDER.`, `You are the REVIEWER (read-only).`, `You are the TROUBLESHOOTER.`, `You are the DRIFT KEEPER (read-only).`, `You are the PLANNER.`
- `role_text(repo: Path, role: str) -> str`. `role` not in `ROLE_NAMES` raises `ValueError`. It reads `<repo>/agents/<role>.md` as UTF-8 and returns the text with trailing whitespace stripped. It returns `DEFAULTS[role]` instead when the file is missing, is not a regular file, cannot be read (`OSError`), is not valid UTF-8, is empty or whitespace-only, or is longer than 20000 characters. It never raises for file problems.

Create six files `agents/test_writer.md`, `agents/builder.md`, `agents/reviewer.md`, `agents/troubleshooter.md`, `agents/drift_keeper.md`, `agents/planner.md`. Each file's first line is exactly the matching default's first sentence (above), followed by that role's standing rules from the design §2 table: what it may write (test writer: only its task's test files; builder: only files_in_scope, never tests, cannot mark itself done; reviewer, drift keeper: nothing, read-only; troubleshooter: only its scratch worktree; planner: only the plan file), its output (JSON matching the schema given in the prompt), and the D-031 blocker-evidence rule for the builder (at least 2 routes tried plus the real error). Plain language, LF line endings, under 3000 characters each.

Protection: add `"agents/*"` and `"agents/**"` to `PROTECTED` in `core/protect.py` (keep every existing entry). Add the line `/agents/ @benjaminanderson0802` to `CODEOWNERS` (keep every existing line).

Wire into `core/bootstrap.py`: every agent prompt the conductor builds for the test writer (`_tests_stage`), builder (`_build_stage`), reviewer (build review and plan review), troubleshooter (`_troubleshoot`), drift keeper (`_drift_check`) and planner (`_plan_stage`) starts with `role_text(self.repo, <role>)` followed by a blank line, replacing the hard-coded "You are the ..." sentence. The stage-specific instructions (the task prompt, file lists, the diff, REVIEW FEEDBACK / TROUBLESHOOTER NOTES / KNOWN DEAD ENDS blocks, and the "Answer with JSON" line) stay in code, after the role text. The role text is read from `self.repo` (the main checkout, which changes only with Ben's approval, D-027), never from the layer worktree or any task worktree, so an agent editing `agents/` on the layer branch cannot change its own instructions. The smoke test prompts are unchanged.

Acceptance criteria the tests must check:
- each of the six files exists in the repository, is non-empty, and its first line equals the default's first sentence;
- `role_text` returns the file text when present, and the default for: missing file, empty file, whitespace-only file, invalid UTF-8 bytes, a directory at that path, and a file over 20000 characters; an unknown role raises `ValueError`;
- `core.protect.violations(["agents/builder.md", "src/x.py"]) == ["agents/builder.md"]`, and a `human-approved` label still clears it; existing protected paths are still protected;
- `CODEOWNERS` contains `/agents/ @benjaminanderson0802`;
- a Conductor (built like `tests/core/test_bootstrap.py` builds one, with fake agents that record their prompts) whose `repo` contains `agents/builder.md` with a unique marker sends that marker at the start of the builder prompt; a different marker written into `<work>/<layer>/agents/builder.md` never appears in any prompt; with no `agents/` folder in `repo` the default text is sent; the same holds for the test writer and reviewer prompts.
The builder must keep `python -m unittest discover -s tests/core` and `python drills/run_drills.py` passing.

### T1B3b

TITLE: Per-task worktrees; judges at the exact commit in a throwaway worktree
FILES: core/worktrees.py, core/bootstrap.py
TESTS: tests/core/test_worktrees.py

Give each build task its own git worktree and run the judges at the exact commit in a throwaway worktree (`docs/specs/layer-1-design.md` §1, §3 step 4).

Create `core/worktrees.py` with `class Worktrees`:
- `__init__(self, repo: Path, work: Path)`: `repo` is the main checkout; worktrees live under `work`.
- `task_path(tid) -> Path` = `work/tasks/<tid>`; `task_branch(tid) -> str` = `forge-task/<tid>`. A `tid` not matching `^[A-Za-z0-9_-]{1,40}$` raises `ValueError` in every method that takes one.
- `prepare_task(tid, base: str) -> Path`: makes `task_path(tid)` a clean worktree on branch `task_branch(tid)` whose HEAD is exactly `base` (a SHA). If the folder is not a registered worktree it runs `git worktree prune`, removes any leftover folder, then `git worktree add -f -B <branch> <path> <base>`; if it is one, it runs `git checkout -q -f -B <branch> <base>` and `git clean -q -fd` there. An unknown `base` raises `RuntimeError`. Calling it twice is safe.
- `remove_task(tid) -> None`: idempotent. `git worktree remove --force` if registered; if the folder still exists, delete it (retry 5 times, 0.5 s apart, for Windows file locks); `git worktree prune`; `git branch -D <branch>` if the branch exists. It never touches any other branch or worktree. Nothing to remove is not an error.
- `throwaway(sha: str)`: a context manager. It creates `work/tmp/<12 hex chars>` with `git worktree add --detach <path> <sha>` and yields the path, whose `git rev-parse HEAD` equals the full SHA of `sha`. On exit (normal or exception) it removes the worktree as `remove_task` does; a removal failure is swallowed (left for `sweep`) and never masks the body's exception. An unknown SHA raises `RuntimeError` before yielding.
- `sweep(keep_tasks: set[str]) -> list[str]`: removes every folder under `work/tmp`, and every task worktree under `work/tasks` whose tid is not in `keep_tasks` (with its `forge-task/<tid>` branch), then prunes; returns the removed paths relative to `work` in POSIX form. It never touches `work/<layer>` or `repo`.
All git calls run without a shell, with `stdin=DEVNULL` and `CREATE_NO_WINDOW` on Windows (reuse `core.bootstrap.NOWIN`-style flags locally; do not import `core.bootstrap`). Commits made in these worktrees use `-c user.name=Forge -c user.email=forge@localhost`.

Change `core/bootstrap.py`:
- `Conductor.trees` property returning `Worktrees(self.repo, self.work)`.
- `_changed`, `_commit`, `_run_tests` and `_run_cmd` gain an optional keyword `cwd: Path | None = None` (default `self.wt`); existing callers are unchanged.
- `_build_stage`: after the claim, reset the layer worktree (`_reset_wt`), take `base = git rev-parse HEAD` of the layer worktree, and `twt = self.trees.prepare_task(tid, base)`. The builder runs with `cwd=twt`. The protected-test restore, `_changed`, scope check and commit all use `twt`. Every reset in this stage (the `fail` helper, the Capped paths) resets `twt` to `base` (`reset --hard base`, `clean -fd`) instead of the layer worktree to `tests_commit`. Any step added by P1B1 (mutation judge, weak-test helpers) that ran on `self.wt` inside the build attempt runs on `twt` instead, unchanged otherwise.
- **Exact-commit judges:** after the commit S, run the task `test_cmd` and then each `judge_cmds` entry inside `with self.trees.throwaway(S) as jw:` using `cwd=jw`. Failure signatures and ledger `test_run` evidence are as before.
- Reviewer diff is `git diff base..S` (taken in `twt`).
- **Troubleshooter scratch worktree:** `_troubleshoot` runs the troubleshooter with `cwd` inside `with self.trees.throwaway(<layer HEAD>) as tw:`, so its edits are discarded.
- **Interim merge (replaced by T1B3e):** after the reviewer passes, run `git merge -q --ff-only S` in the layer worktree before the ledger `pass`. If that fails, the attempt fails with reason `layer moved during the attempt` (submitted=True). Then pass, `status done`, `done_commit=S`, push, `drift_due` as today, and `self.trees.remove_task(tid)`.
- **Interrupted attempts:** at the start of a build attempt (after R38 pending troubleshooting), if the contract is `claimed` apply `release` (`forge-core`), and if it is `submitted` apply `fail` (`forge-auditor`) then `reopen` (`forge-manager`), with proposal ids `<cid>-recover-<attempts>-release|fail|reopen`; add the note `interrupted attempt recovered`; do not append a failure signature; then continue with the normal attempt in the same step.

Acceptance criteria the tests must check (real git in temp folders; conductor tests may import helpers from `tests/core/test_bootstrap.py`):
- `prepare_task` gives HEAD == base, a clean tree and branch `forge-task/<tid>`; calling it again with another base moves it; dirty/untracked files are gone after re-prepare; bad tid raises `ValueError`; unknown base raises `RuntimeError`.
- `remove_task` removes folder and branch and is idempotent; `throwaway` yields a worktree at exactly the SHA and removes it on exit and on an exception (the exception propagates); `sweep` removes stale tmp and task worktrees not kept and leaves kept ones, the layer worktree and repo alone.
- In the conductor, the builder's `cwd` is `work/tasks/T1`, not the layer worktree; the layer branch tip does not change during a failed attempt; after a passing attempt the layer tip equals the task commit S, the task worktree and branch are gone, and the task is `done`.
- A `judge_cmds` entry that records its working directory and `git rev-parse HEAD` shows a folder under `work/tmp` and exactly S, and that folder no longer exists afterwards.
- The troubleshooter's file edits appear neither in the layer worktree nor on any branch.
- A contract left `submitted` (or `claimed`) by a simulated crash is failed and reopened (or released) at the next attempt, with the note, and the attempt then proceeds.
- `tests/core/test_bootstrap.py` keeps passing unchanged.

### T1B3c

TITLE: Ledger crash recovery, event-backed completion and merge evidence
FILES: core/ledger.py
TESTS: tests/core/test_ledger_recovery.py

Make `core/ledger.py` recoverable at every crash boundary of `apply`, make completion event-backed, and let the `pass` event carry verified merge evidence. Keep every existing rule, action, field and file format (`contracts.json` stays a dict of contracts); `drills/run_drills.py` and all tests in `tests/core` must keep passing.

1. **`_append_event(self, event: dict) -> None`**: move the append-and-fsync of one event line out of `apply` into this method (tests patch it to simulate a crash after the cache files were written).
2. **Head marker:** `apply` writes `ledger/head.json` = `{"hash": <hash of the event being appended>}` together with the other cache files (before `_append_event`). `rebuild` writes it too. `snapshot()` keeps hashing only its current five files.
3. **`repair_tail(self) -> bool`**: if the last line of `events.jsonl` is not newline-terminated or does not parse as JSON, truncate the file to the end of the previous complete line (that append never completed, so the proposal was not applied) and return True. A bad line anywhere before the last raises `Rejected("event log corrupt")`; nothing is dropped silently. Otherwise False.
4. **`derive(self) -> dict`**: replays every event into a fresh ledger in a temporary folder (with a copy of `roles.json`, replay mode on) and returns `{"contracts", "test_runs", "reports", "spec_state", "head"}`; it never writes to this ledger.
5. **`rebuild(self) -> None`**: `repair_tail`, `derive`, then atomically write `contracts.json`, `test_runs.json`, `run_reports.json`, `spec.json` and `head.json`. It never truncates or rewrites `events.jsonl`, so a crash at any point leaves the events intact. `replay()` becomes a thin wrapper that calls `rebuild()`.
6. **`reconcile(self) -> bool`**: raises `Rejected` if `verify_chain()` fails (after `repair_tail`). If `head.json` is missing or differs from the last event's hash (or `"genesis"` for an empty log), or any cache file differs from `derive()`, it calls `rebuild()` and returns True; else False.
7. **Self-healing apply:** `apply` first runs `repair_tail()` and, when `head.json` is missing or does not match the last event hash, `rebuild()`, before any other check. So a cache that got ahead of the log (crash between the cache writes and the event append) is corrected before the next decision, and the interrupted proposal can be applied again under its own `proposal_id` (it is not a duplicate, because its event never landed).
8. **`completion(self, cid: str) -> dict | None`**: the `pass` event for `cid` read from `events.jsonl` (after `repair_tail`), or None. It never reads `contracts.json`. Callers must use it, not the contract status, to decide whether a contract is complete.
9. **Merge evidence on `pass`** (all optional, so old events and callers still work): `task_commit` (if present must equal the contract's `commit`), `final_sha` (a 40-character lowercase hex string), `merges` (a list). Each merge entry must be a dict with `sha` (40-hex), `parents` (list of str), `kind` (`"local"` or `"divergence"`), `run_id` (str), `verdict` == `"pass"` and `reasons` (list of str), and `run_id` must be a CI `test_run` already recorded for this contract with `commit == sha` and `passed == true`. A non-empty `merges` requires `final_sha`. Any violation raises `Rejected("pass merge evidence incomplete: ...")`.

Acceptance criteria the tests must check (temporary ledgers built as the drills build them, with `roles.json`):
- Crash between cache write and event append for a `pass` (patch `_append_event` to raise once): afterwards `contracts.json` says `done` but `completion(cid)` is None; `reconcile()` returns True and the contract is `submitted` again; applying the same proposal again succeeds (`applied`, not `duplicate`) and `completion(cid)` then returns the event; exactly one `pass` event exists.
- The same crash for `create`, `claim`, `submit` and `test_run`: the next `apply` of any proposal heals first (the phantom contract/run disappears or is re-applied cleanly) and `verify_chain()` stays True.
- A torn last line is truncated and the chain verifies; a corrupt middle line raises `Rejected`.
- A crash inside `rebuild` (patch the atomic writer to raise on its second file) leaves `events.jsonl` byte-identical, and a later `reconcile()` restores the correct state.
- `replay()` on a normal ledger gives the same contracts as before and does not change `events.jsonl`.
- `pass` with valid `merges` (a recorded passing `test_run` for the merge sha) is applied and the event payload contains `task_commit`, `final_sha` and `merges`; each of: missing run, run for another sha, failed run, verdict `fail`, bad `kind`, `merges` without `final_sha`, wrong `task_commit`, malformed `final_sha` is rejected and changes nothing (`snapshot()` unchanged).
- A `pass` without merge fields still works exactly as before.

### T1B3d

TITLE: Merge journal, approved-merge registry, safe push and the crash-safe finalizer
FILES: core/finalize.py
TESTS: tests/core/test_finalize.py

Create `core/finalize.py`: plain code (no AI, no imports from `core.bootstrap`) that takes a reviewed task commit S into the layer branch and finalizes it so that every crash boundary is recoverable. It uses `core.worktrees.Worktrees` (T1B3b). All JSON writes are atomic: temp file, flush, `os.fsync`, `os.replace`.

**`Journal(state: Path)`**, one file per task at `state/merges/<tid>.json`:
- `begin(tid, cid, task_sha, base, ci_run_id, review: dict, evidence: dict) -> dict`: creates the record `{"tid","cid","task_sha","base","ci_run_id","review","evidence","pass_pid": "<cid>-pass-<task_sha[:12]>","phase": "merge","status": "active","blocked": None,"intent": None,"candidates": [],"final_sha": None,"pushed": None,"drift_marked": False,"cleaned": False,"notes": [],"answers": 0,"rounds": 0}`. If a record for `tid` exists with the same `task_sha` it is returned unchanged; with a different `task_sha` raise `ValueError`.
- `load(tid)`, `save(rec)`, `all()` (sorted by tid), `active()` (status `active` only; never `blocked` or `finished`), `blocked_on(qid)`, and `unblock(qid, answer) -> list[str]`: for each record blocked with that qid set status `active`, `blocked` None, `rounds` 0, `answers` +1, append `"Ben: <answer>"` to notes (each note capped at 2000 chars, keep the last 30); return the tids.

**`ApprovedMerges(state: Path)`** at `state/approved_merges.json`: `add(sha, record: dict)`, `has(sha) -> bool`, `get(sha)`.

**`unapproved_merges(wt: Path, layer: str, approved) -> list[str]`**: merge commits (`git rev-list --merges`) reachable from `layer` but not from `refs/remotes/origin/<layer>` (or, if that ref does not exist, not from any `refs/remotes/origin/*`), that `approved.has()` rejects.
**`safe_push(wt, layer, approved) -> tuple[str, str]`**: if `unapproved_merges` is non-empty return `("refused", <shas>)` without pushing; else `git push origin refs/heads/<layer>:refs/heads/<layer>`; return `("ok", out)`, `("rejected", out)` for a non-fast-forward rejection, or `("error", out)` otherwise.

**`Hooks`** dataclass (callables supplied by the conductor): `judge(sha) -> {"passed": bool, "run_id": str, "output": str}` (judges exactly that SHA in a throwaway worktree and records the CI run); `review(rec, cand) -> {"verdict", "reasons"}` (may raise; the exception propagates and nothing is recorded); `ask(kind, subject, body, tid) -> qid`; `question_open(qid) -> bool`; `mark_drift(tid)` and `drift_marked(tid) -> bool`; `ledger_pass(rec)` (raises on refusal) and `ledger_completed(cid) -> bool` (event-backed); `set_task(tid, changes: dict)`; `crash(point: str)` (default no-op; tests raise from it).

**`Finalizer(layer_wt, layer, trees, journal, approved, hooks, *, push: bool, max_rounds: int = 3)`**, with `run(tid) -> str` returning `"finished"` or `"blocked"` (a record that is `blocked` returns `"blocked"` immediately, touching nothing: no git, no hooks, no write). `run` executes phases in order, saving the record after each; every phase first reconciles against Git. `hooks.crash(p)` is called at these points, where `before:X` is just before Git/durable operation X (after its intent was saved) and `after:X` is just after X and before the journal save that records it:
1. **merge**: reset the layer worktree (`reset --hard`, `clean -fd`); tip = HEAD. S ancestor-or-equal of tip → next phase. Tip ancestor of S → save intent `{"op": "ff", "from": tip, "to": S}`, `before:ff`, `git merge -q --ff-only S`, `after:ff`. Otherwise → candidate(kind `local`, base tip, other S). On resume with an intent, if tip is neither a descendant of S nor `from`, block (`layer moved unexpectedly`).
2. **sync**: push off → `final_sha` = tip, `pushed` False, next phase. Else R = `git ls-remote origin refs/heads/<layer>` (a failed command raises `RuntimeError`, not blocked); if R exists, `git fetch -q origin <layer>` (failure raises `RuntimeError`). R absent or R ancestor-or-equal of tip → push phase; else candidate(kind `divergence`, base tip, other R).
3. **candidate(kind, base, other)**: `rounds` +1; above `max_rounds` → block (`too many merge rounds`). Append `{"n", "kind", "base", "other", "sha": None, "state": "creating"}` and save, `before:candidate-merge`; in `trees.throwaway(base)` run `git -c user.name=Forge -c user.email=forge@localhost merge --no-ff --no-edit -m "Forge: merge <other[:12]> into <layer> for <tid>" <other>`; on success `git update-ref refs/forge/candidates/<tid>-<n> HEAD` (keeps M alive), `after:candidate-merge`, save sha and parents, state `created`. On conflict record the conflicted files, `git merge --abort`, state `rejected`, and block with reason `merge_conflict`. On resume from `creating`, adopt `refs/forge/candidates/<tid>-<n>` if its parents are exactly [base, other], else recreate. Then `before:judge`, `hooks.judge(M)`, `after:judge`, save (`judged`); failed → `rejected`, block. `before:review`, `hooks.review`, `after:review`, save (`reviewed`); verdict not `pass` → `rejected`, block. `before:approve`, `approved.add(M, {tid, kind, base, other, run_id, verdict, reasons})`, `after:approve`, save (`approved`). Move the layer: tip == M or M ancestor of tip → done; tip == base → intent ff, `before:candidate-ff`, `git merge -q --ff-only M`, `after:candidate-ff`; else block. Then back to sync. The layer ref never points at M before M is approved.
4. **push**: require S ancestor-or-equal of tip (else block). If the remote already equals tip, record it. Else `before:push`, `safe_push`, `after:push`: `ok` → `final_sha` = tip, `pushed` True; `rejected` → back to sync (counts a round); `refused` → block; `error` → raise `RuntimeError`.
5. **drift**: unless `hooks.drift_marked(tid)`, call `hooks.mark_drift(tid)`; `after:drift`; save `drift_marked` True.
6. **cleanup**: `trees.remove_task(tid)`, delete `refs/forge/candidates/<tid>-*`; `after:cleanup`; save `cleaned` True.
7. **ledger**: unless `hooks.ledger_completed(cid)`: `before:ledger`, `hooks.ledger_pass(rec)`, `after:ledger`; a refusal blocks (`ledger refused completion`).
8. **queue**: `hooks.set_task(tid, {"status": "done", "done_commit": S, "final_sha": F, "merge_candidates": [approved shas]})`, `after:queue`, then status `finished`.
**Blocking** saves status `blocked` and `blocked: {"reason", "qid"}` with the qid from `hooks.ask("merge", ...)` (subject names the task and reason; body gives conflicted files or the judge/review reasons and says other work continues). If another record is blocked with the same reason and the same `other` SHA and `hooks.question_open(qid)` is True, reuse that qid instead of asking again.
**`reconcile(tasks: list[dict])`**: for each record: status active/blocked and task status `tests_ok` → `set_task(tid, {"status": "merge_pending"})`; status `finished` and task not `done` → if `ledger_completed` set it done, else reopen at phase `ledger`; task `done` and record not finished → finish it if `ledger_completed`, else phase `ledger`. A task `merge_pending` with no record → `set_task(tid, {"status": "tests_ok"})`.

Acceptance criteria (real git: a bare `origin`, a main repo, a layer worktree; fake hooks that record calls):
- Fast-forward path with push: origin and layer tip == S; `merges` empty; hook order is judge/review never called, then mark_drift, cleanup, ledger_pass, set_task done; at the moment `ledger_pass` and `set_task` run, the journal on disk already has `pushed` True, `drift_marked` True and `cleaned` True.
- For every crash point on the fast-forward path and on the divergence path, a hook raising a `BaseException` subclass once, then a fresh `Finalizer` on the same folders: the run finishes; `ledger_pass` takes effect exactly once and `set_task(done)` happens exactly once and only after `pushed`/`drift_marked`/`cleaned` are durable; the remote never contains a merge commit missing from the registry (checked right after each crash).
- Divergence (origin has an extra commit): the candidate is judged at exactly M and reviewed while the layer tip is still the old tip and M is not yet approved; afterwards layer and origin == M, the record's candidate has run_id, verdict and reasons, `final_sha` == M, `task_sha` == S.
- Conflict: `blocked`, one `ask`, layer tip and origin unchanged; calling `run` again several times and on fresh objects makes no git merge, no hook call and no write; a second record hitting the same conflict reuses the qid; after `unblock(qid)` and the remote resolved, both finish.
- Judge fail, review fail, `safe_push` refusal of an unapproved merge commit, `max_rounds` exceeded: each blocks without pushing. A review hook raising leaves the candidate `judged`; the next run calls only review.
- Push off: no `ls-remote`/fetch/push; `pushed` False; `final_sha` is the local tip.
- `Journal.begin` idempotence and `ValueError`; `active()` excludes blocked; `reconcile` fixes each queue/journal mismatch listed above.

### T1B3e

TITLE: Wire safe finalization into the conductor; blocked merges wait for Ben
FILES: core/bootstrap.py
TESTS: tests/core/test_merge_pipeline.py

Wire `core/finalize.py` (T1B3d), `core/worktrees.py` (T1B3b) and the ledger recovery (T1B3c) into `core/bootstrap.py`, replacing the interim merge from T1B3b.

**Build stage end.** After the reviewer passes S (and after every P1B1 check that must pass has passed), instead of the interim ff/pass/done/push/drift: build `evidence` (the payload P1B1 prepares for the `pass` event: verdict, reasons, mutation evidence; keep it identical), call `Journal(self.state).begin(tid, cid, S, base, ci_run_id, {"verdict": "pass", "reasons": reasons}, evidence)`, call `self._crash("after:journal-begin")`, set the task status to `merge_pending`, then `self._finalizer().run(tid)` in the same step. The ledger `pass` is not applied anywhere else.

**Hooks** built by `_finalizer()`:
- `judge(sha)`: in `self.trees.throwaway(sha)`, run this task's `test_cmd`, the `test_cmd` of every task whose status is `done`, and every `judge_cmds` entry (cwd = that worktree); apply a ledger `test_run` (`ci`) with proposal id and run id `ci-merge-<tid>-<sha[:12]>`, commit sha, passed; return passed, run_id and the last 20 lines of the first failing output. Mutation testing is not rerun on a merge commit; `evidence` for the candidate records `"mutation": "not applicable: merge commit adds no builder lines"`.
- `review(rec, cand)`: `_call("reviewer", ...)` with `role_text(self.repo, "reviewer")`, the words "You are reviewing a MERGE COMMIT created because the layer branch moved", the task prompt, `git diff <base>..<M>` and `git diff <other>..<M>` (each capped at 30000 chars), the judge result and Ben's notes from the record, schema `S_REVIEW`, `cwd` a throwaway worktree at M. `ok=False` output counts as verdict `fail` with reason `reviewer output unusable: <error>`. `Capped`/`Tampered` propagate.
- `ask` → `self._ask("merge", subject, body, task=tid)`; `question_open` → the question's status is `open`.
- `mark_drift(tid)`: set queue `drift_due` True and append tid to queue `drift_marks` (a list, no duplicates) in one queue write; `drift_marked(tid)`: tid in `drift_marks`.
- `ledger_pass(rec)`: `self._apply(rec["pass_pid"], "pass", cid, "forge-auditor", payload)` where payload = `rec["evidence"]` plus `run_id` = `rec["ci_run_id"]`, `task_commit`, `final_sha`, `pushed`, and `merges` (each approved candidate: sha, parents, kind, run_id, verdict, reasons). Raise if it is refused and `completion(cid)` is still None. `ledger_completed(cid)` → `self._ledger().completion(cid) is not None`.
- `set_task` → `self._update`; `crash` → `self._crash`, a method that does nothing (tests override it).

**`step()` order** after the KILL/PAUSED/capped checks: (1) `self._ledger().reconcile()`; (2) `self._finalizer().reconcile(tasks)`; (3) `self.trees.sweep(keep_tasks={ids with status tests_ok or merge_pending})`; (4) `drift_due` → drift check; (5) the first record (queue order) in `Journal.active()` → `run(tid)` → `"worked"`; blocked records are never run; (6) the next `todo`/`tests_ok` task as today (`merge_pending` and `blocked` tasks are skipped); (7) gate only when every task is `done`, no journal is active or blocked, and `drift_due` is off. In (5), `Capped` → `"capped"`, `Tampered` → `"killed"`, `RuntimeError`/`OSError` → log and `"error"` (R13), never `_after_failure`, never a counted attempt.
**Answers:** a `merge` question answered (valid code, R2) calls `Journal.unblock(qid, body)` and is marked answered; nothing else retries a blocked record.
**Every push** (`_push`, used by the plan stage and the gate) goes through `finalize.safe_push`; `refused` or `error` logs and raises `RuntimeError` (stage error); push off does nothing.
**Build start:** a task with a journal record never starts a build attempt.

Acceptance criteria (tests build a Conductor like `tests/core/test_bootstrap.py`, but with a bare `origin` remote and `push=True`; fake agents):
- Happy path: the task becomes `done` only after origin's layer branch contains S; the ledger `pass` event (via `completion`) has `task_commit` S and `final_sha`; the contract `commit` is S; the task worktree is gone; the drift keeper runs.
- Crash at each of `after:journal-begin` and every T1B3d crash point (a Conductor subclass whose `_crash` raises a `BaseException` subclass once), and a crash inside the ledger `pass` (patch `Ledger._append_event` to raise once): a fresh Conductor stepped until idle/gate ends with the task `done`, exactly one `pass` event, origin containing S, the drift keeper run, no task worktree; and at every intermediate point, queue `done` implies an existing `pass` event, and an existing `pass` event implies `pushed`/`drift_marked`/`cleaned` in the journal.
- Divergence with a clean merge: the candidate is judged in a throwaway worktree at M and reviewed before the layer moves; origin == M; the `pass` payload's `merges` holds M's run_id, verdict and reasons.
- Divergence conflict: one `merge` email; repeated steps and a restart (new Conductor) never retry the blocked task (no git merge, journal file byte-identical) while a second task runs its test and build stages; the second task reuses the same question if it hits the same conflict; after the owner's coded reply and the remote resolved, both finish and the gate opens.
- A merge commit placed on the layer branch outside the registry makes the plan-stage and gate pushes refuse (nothing pushed).
- `tests/core/test_bootstrap.py` keeps passing unchanged.

## Checks against the rules

- **R1:** every `test_cmd` is `python -m unittest tests/core/<file>.py` naming only that task's own test file.
- **R8/R13:** Git and network failures during finalization are stage errors (`"error"`, back-off, email after 3), never a counted attempt and never "no changes".
- **R9/R14:** journals, the approved-merge registry and ledger repairs are conductor writes made outside agent runs; the candidate reviewer runs through `_call`, so the tamper guard covers it.
- **R19/R28:** journal notes are capped (2000 chars, last 30); journals and registry entries are bounded by the number of tasks and `max_rounds`.
- **R2/R20/R23-R24:** merge questions use coded replies and `_send`'s budget; a blocked merge asks once (qid reused), never on every step.
- **R37:** a capped candidate reviewer changes nothing and is retried when the cap resets; the record stays `judged`, not `blocked`.
- **D-025:** judges are plain code; the reviewer stays read-only (throwaway worktree); the builder still can't touch tests or mark itself done; completion is decided by the ledger's event log.
- **D-027:** only the layer branch is merged and pushed; `main` changes only through the gate.
- **D-028 / protected files:** role files join the protected set and are read only from the main checkout.
- **D-035:** nothing here switches on a new sending/spending/posting behaviour; pushes already existed. The first real divergence recovery is watched before being trusted unattended.
- **D-036:** each task is sized for one agent session; the finalizer's `max_rounds` bounds its own loop.

## Reviewer notes

- T1B3b: Run the ordinary build reviewer in the task worktree at S, or a throwaway worktree at S. Its current default cwd is the layer checkout, which will no longer contain the submitted implementation before merge.
- T1B3c: Write the new head marker before modifying any cache, or compare derived cache contents on every apply. Otherwise a crash after a cache write but before the marker write leaves the old marker matching the event log and bypasses self-healing. Test crashes between individual cache writes.
- T1B3d: Make queue completion idempotent across after:queue. A fresh Finalizer.run must consult durable queue state or use an idempotent completion hook; otherwise set_task(done) repeats despite the exactly-once acceptance criterion.
- T1B3d: Persist or recover the association between a merge question and its journal across a crash after ask but before saving blocked status. Reuse that question on restart so this boundary cannot create duplicate questions.
- T1B3d: On unblock, recompute the current local and remote tips and retire rejected candidate state before retrying. Also enforce max_rounds for push rejections that return to sync without creating a candidate.
- T1B3e: Make the queue write containing drift_due and drift_marks durable before recording drift_marked in the journal. The existing bootstrap JSON writer needs its durability checked; atomic replacement alone does not establish the required flush ordering.
- T1B3e: Preserve P1B2 readiness checks when changing step ordering. Handle safe_push's rejected result explicitly at plan-stage and gate call sites so a rejected push cannot be treated as success.
