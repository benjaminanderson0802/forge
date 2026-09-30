# Layer 1B part 2: readiness, capabilities and blocker claims

> Plan task P1B2. Serves `docs/specs/layer-1-design.md` §4 (readiness check, capability map, blocker claims, known dead ends) and §3 step 2 (the Builder's blocker path), implementing D-030, D-031 and D-033. Parts P1B1 (judges) and P1B3 (role files, worktrees, merge) are planned separately. Builds on `core/readiness.py` (T1A3R), `core/bootstrap.py` and rules R1–R40 in `docs/specs/bootstrap-conductor.md`.

## What the conductor lacks today

| Design requirement | Today | This plan |
|---|---|---|
| Readiness check before every session and every cycle | only `python -m core.readiness` by hand; the conductor never runs it | T1B2b: every `step()` and every `main run` start refreshes the map; caching only inside individual checks |
| AI probes are metered and capped (D-020, R37) | `core.readiness` launches Claude/Codex directly, unmetered | T1B2b: probes go through the conductor's guard (cap check first, tamper guard, meter, run record) |
| Every agent reads the capability map (D-030) | nobody sees it | T1B2b: a `CAPABILITY MAP:` block in every prompt `_call` sends, smoke included |
| Known dead ends in every Builder and Troubleshooter prompt | Builder only, never the Troubleshooter, never smoke | T1B2b: added in `_call` for both roles, every prompt |
| No Builder starts while a needed capability is broken; missing evidence blocks | nothing is gated | T1B2c: one `broken()` predicate; role requirements plus the task's declared `needs`; clean undo (`NotReady`) at every launch point |
| Tasks declare the capabilities they need | no field | T1B2c: optional `needs` in `S_PLAN`, planner prompt, `validate_task`, preserved into the queue |
| A failing check goes to the Troubleshooter or to Ben with the exact fix | nothing | T1B2d: bounded routing of every failed check; diagnosed fix lines; re-check after a fix; D-023-compliant delivery |
| Blocker claims need 4 pieces of evidence and must not contradict the map; the Reviewer rejects claims without real attempts; rejections are `easy_out` events | only `tried` + `error` checked | T1B2e |

## Fixes for the earlier review findings

- **No fail-open path.** `Conductor(checks=None, probes=None)` means the *real* checks and probes (`real_checks()`, `real_probes(limits)`), never "skip". Tests pass healthy fakes explicitly; the existing harnesses are migrated in T1B2b's test scope. Missing, malformed, failing, future-dated or stale evidence for a required capability all block (one predicate, `readiness.broken`, used by gating, routing, blocker claims and post-fix success).
- **Every cycle, every session.** `step()` refreshes the map after the KILL/PAUSED/cap checks and before anything else runs; `main run` calls `session_start()` (refresh, then routing) before the smoke test, so a broken capability is routed even when the smoke test then fails and `main` exits. Smoke agents go through the gated `_call`.
- **Caching only inside checks.** Ok results of expensive checks (Claude, Codex, Gmail, browser) are reused for a per-check TTL; failed AI/Gmail results are not re-probed for `fail_retry_s` (default 900 s) unless forced. Forced re-checks happen after a fix job and after Ben replies.
- **Caps.** A probe for a capped provider is never launched; `step()` returns `"capped"` before readiness; the stale-evidence refresh inside `_call` also never launches a capped probe. Probe tokens are metered.
- **Requirements declared and preserved.** `needs` is in `S_PLAN`, in the planner prompt, validated, and copied into queued tasks. An accepted blocker claim appends its capability to `needs` even when the map has no such check (e.g. `vpn`), so the task waits and routing asks Ben.
- **Pending troubleshooting first, with its own requirements.** A task's `troubleshoot_pending` is gated on the Troubleshooter's requirements only, and runs before the Builder's gate is considered.
- **Clean undo everywhere.** `NotReady` is handled at every place `Capped` is (test writer, planner, builder, reviewer after submit, blocker reviewer, task troubleshooter, drift keeper, smoke), with the same no-penalty undo.
- **All failed checks are routed, boundedly.** One Troubleshooter job per step at most, per capability at most `cap_trouble_max` (3) rounds, at least `cap_retry_h` (1 h) apart; everything else (not troubleshootable, rounds used up, Troubleshooter itself unavailable, e.g. git or Claude broken) becomes one open Ben-queue item per capability. Simultaneous failures and an empty map are covered.
- **Exact fixes from diagnosed conditions.** `readiness.diagnose` picks the fix from the failure (missing executable vs logged out vs daemon down vs container missing/stopped, …), not a fixed line per capability.
- **D-023.** Capability items are stored with `hold: true` and are not emailed while other work can progress. They are emailed instantly only when Ben alone can unblock all progress. The digest itself stays in 1E.
- **An arbitrary reply never makes a capability ready.** Only fresh ok evidence closes a capability item; the reply forces a re-check. The only reply that changes requirements is exactly `not needed`, and only for a task's declared `needs` (never git or an agent provider).

## Tasks (in order; each depends on the ones before it)

1. **T1B2a** `core/readiness.py`: `evaluate`, map I/O, `broken`, `max_age_for`, `diagnose`, probe helpers. Test: `tests/core/test_readiness_gate.py`.
2. **T1B2b** `core/bootstrap.py`: readiness every cycle and session, guarded AI probes, capability map and dead ends in prompts; test harness migration. Tests: `tests/core/test_readiness_cycle.py`, `tests/core/test_bootstrap.py`, `tests/core/test_live_run.py`.
3. **T1B2c** `core/bootstrap.py`: `needs`, gating, `NotReady`. Test: `tests/core/test_readiness_gating.py`.
4. **T1B2d** `core/bootstrap.py`: routing, capability fix jobs, Ben-queue items, replies. Test: `tests/core/test_capability_routing.py`.
5. **T1B2e** `core/bootstrap.py`: blocker claims (D-031). Tests: `tests/core/test_blocker_claims.py`, `tests/core/test_bootstrap.py`.

The full task sections below are the only text the test writer and builder see.

### T1B2a

TITLE: Readiness helpers: evaluate, evidence predicate, diagnosed fixes
FILES: core/readiness.py
TESTS: tests/core/test_readiness_gate.py

Extend `core/readiness.py` (plain code; D-030, design §4) with the helpers the conductor will use. Nothing here launches an AI agent or touches the conductor. Keep every existing name (`CHECKS`, `AI_TTL`, `run_checks`, `main`, `_fresh`) working: `tests/core/test_readiness.py` must pass unchanged.

Add:
- `NAME_RE = re.compile(r"^[a-z0-9_]{1,40}$")` and `AI_NAMES = ("claude", "codex")`.
- `PLAIN_CHECKS`: `CHECKS` without the AI names (same callables).
- `evaluate(checks: dict[str, Callable[[], tuple[bool, str]]], timeout_s: float, now: datetime) -> dict`: runs every check in its own daemon thread with the same deadline rules `run_checks` uses today (exceptions become `ok: False` with `"<Type>: <msg>"`; a result reported after the deadline, or none, becomes `ok: False`, detail `check timed out after <timeout_s>s`). Returns `{name: {"ok": bool, "detail": str[:300], "checked_at": now.isoformat()}}`. No file I/O. Refactor `run_checks` to use it without changing its behaviour.
- `read_map(path) -> dict`: `{}` if the file is missing, unreadable, invalid JSON or not an object. `write_map(path, m)`: atomic (temp file + `os.replace`), sorted keys, UTF-8, LF.
- `broken(entry, now: datetime, max_age_s: float) -> str | None`: `None` only for usable ok evidence. Otherwise a reason starting with one of: `no evidence` (entry is None), `malformed evidence` (not a dict, or `ok` is not a bool), `failing: <detail>` (`ok is False`), `bad checked_at` (missing, not a string, unparsable, or timezone-naive), `evidence from the future` (more than 60 s after `now`), `stale evidence` (older than `max_age_s`). `ok` must be exactly `True`: `1`, `"true"` are malformed.
- `ok_ttl_s(name, limits) -> float`: Claude/Codex `limits.get("ai_check_ttl_h", 6) * 3600`; gmail and browser `1800`; others `0`. `fail_retry_s(name, limits) -> float`: Claude, Codex, gmail `limits.get("readiness_fail_retry_s", 900)`; others `0`. `max_age_for(name, limits) -> float`: `max(limits.get("readiness_max_age_s", 900), ok_ttl_s(name, limits))`.
- `requirements(provider: str | None, needs=None) -> set[str]`: `{"git"}`, plus `provider` if truthy, plus every string in `needs` (kept even if not a known check, so it fails closed).
- `probe_agents(limits) -> dict`: `{"claude": ClaudeAgent(timeout_s=t, permission_mode="plan"), "codex": CodexAgent(timeout_s=t)}` with `t = max(5, int(limits.get("check_timeout_s", 60)) - INNER_MARGIN_S)`; constructing launches nothing. `PROBE_PROMPT = "Reply with exactly: ok"`. `probe_ok(result) -> tuple[bool, str]`: `(True, "ok (<tokens> tokens)")` when `result.ok` and the stripped, lower-cased text starts with `ok`; else `(False, result.error or "unexpected reply: <first 100 chars>")`.
- `diagnose(name, entry, cap_map, which=shutil.which, probe=None) -> dict` with keys `condition` (str), `fix` (one exact line starting `PowerShell: ` or `Win + R: `, or a reply instruction for `no_check`), `then` (str, may be empty), `troubleshoot` (bool: a Troubleshooter job can plausibly fix it) and `depends_on` (str or None). `probe(args) -> (exit_code, output)` runs a command hidden; default is a helper using `core.agents.launch`. Rules, first match wins, matched case-insensitively against `entry["detail"]`:
  - entry None and `name` is a known check or AI name → `no_evidence`, troubleshoot True, fix `PowerShell: python -m core.readiness`. Unknown name (no check exists) → `no_check`, troubleshoot False, fix `Reply to this email with: not needed`.
  - git: `not installed|not on PATH` → `missing`, False, `PowerShell: winget install --id Git.Git -e`; else `error`, True, `PowerShell: git --version`.
  - github: missing → `missing`, False, `PowerShell: winget install --id GitHub.cli -e`; `not logged|auth login|no oauth|token|authentication` → `logged_out`, False, `PowerShell: gh auth login --hostname github.com --git-protocol https --web`; else `error`, True, `PowerShell: gh auth status`.
  - claude: `not installed|not recognized|FileNotFound|no such file` → `missing`, False, `PowerShell: npm install -g @anthropic-ai/claude-code`; `log ?in|401|unauthori|authentication|api key` → `logged_out`, False, `PowerShell: claude.cmd` with then `type /login and follow the browser sign-in`; `rate|usage limit|429|overloaded` → `rate_limited`, False, `PowerShell: claude.cmd -p ok`; else `error`, True, same fix.
  - codex: same pattern with `PowerShell: npm install -g @openai/codex`, `PowerShell: codex login`, `PowerShell: codex exec ok`.
  - gmail: `no app password` → `no_password`, False, `PowerShell: python -c "import keyring,getpass; keyring.set_password('forge-gmail','benjaminanderson0802@gmail.com',getpass.getpass('Gmail app password: '))"`, then `create the app password at https://myaccount.google.com/apppasswords first`; `SMTPAuthentication|not accepted|535` → `auth_rejected`, False, same fix, then says the saved password was rejected; `No module named` → `missing_lib`, True, `PowerShell: python -m pip install keyring`; `gaierror|getaddrinfo|timed out|refused|unreachable` → `network`, True, `PowerShell: Test-NetConnection smtp.gmail.com -Port 465`; else `error`, True, `PowerShell: python -m core.readiness`.
  - docker: missing → `missing`, False, `PowerShell: winget install --id Docker.DockerDesktop -e`; `error during connect|cannot connect|daemon|pipe` → `daemon_down`, True, `Win + R: C:\Program Files\Docker\Docker\Docker Desktop.exe`; else `error`, True, `PowerShell: docker info`.
  - n8n: if `broken(cap_map.get("docker"), now, inf)` is not None → `docker_down`, False, `depends_on="docker"`. Else `probe(["docker", "ps", "-a", "--filter", "name=^/n8n$", "--format", "{{.Status}}"])`: non-zero exit → `probe_failed`, True, `PowerShell: docker ps -a`; empty output → `container_missing`, True, `PowerShell: docker run -d --name n8n --restart unless-stopped -p 127.0.0.1:5678:5678 -v n8n_data:/home/node/.n8n docker.n8n.io/n8nio/n8n`; starts `Exited` or `Created` → `container_stopped`, True, `PowerShell: docker start n8n`; starts `Up` → `unhealthy`, True, `PowerShell: docker restart n8n`.
  - ollama: `which("ollama")` is None → `missing`, False, `PowerShell: winget install --id Ollama.Ollama -e`; else `not_running`, True, `PowerShell: Start-Process ollama -ArgumentList serve -WindowStyle Hidden`.
  - python_libs: `missing: a, b` → `missing_libs`, True, `PowerShell: python -m pip install <names>` with `yaml` mapped to `pyyaml`, others unchanged, in the order listed; else `error`, True, `PowerShell: python -m core.readiness`.
  - browser: `No module named` → `missing_lib`, True, `PowerShell: python -m pip install playwright; python -m playwright install chromium`; `Executable doesn't exist|playwright install` → `no_chromium`, True, `PowerShell: python -m playwright install chromium`; else `error`, True, same fix.
  - any other known name: `timed out` → `timeout`, True, `PowerShell: python -m core.readiness`; else `error`, True, same.
  Keep the rules in one data table so they are easy to correct. `diagnose` never raises: a probe that raises gives `probe_failed`.

Acceptance criteria the tests must check (no real network, processes or agents: pass fake `which`/`probe`, fake check callables and a fixed `now`):
- `evaluate` returns entries for every check, isolates exceptions, times out slow checks, writes no file; `run_checks` still behaves as before;
- `broken` returns None for a fresh ok entry and the documented reason prefix for: None, a list, `{"ok": "true", ...}`, `{"ok": 1, ...}`, `ok False`, missing / non-string / unparsable / naive `checked_at`, 2 minutes in the future, older than `max_age_s`; exactly at the age limit is ok;
- `max_age_for`, `ok_ttl_s`, `fail_retry_s` values and limit overrides; `requirements(None)`, `requirements("claude", ["docker", "vpn"])`;
- `read_map` on missing / invalid / array files; `write_map` round-trips and leaves no temp file;
- `diagnose` picks different fixes for different conditions of the same capability: gh missing vs logged out; docker missing vs daemon down; n8n with docker broken (`depends_on == "docker"`), container missing, stopped, running-but-unhealthy, probe failure, probe raising; ollama missing vs not running; python_libs names (`yaml` → `pyyaml`); gmail no password vs rejected vs network; claude missing vs logged out vs rate limited; `no_evidence` for a known name with no entry; `no_check` for `vpn`; every `fix` starts with `PowerShell: `, `Win + R: ` or `Reply `, and no `fix` puts quotes around a path;
- `probe_agents` constructs without launching (patch `core.agents.launch` to raise); `probe_ok` accepts `"ok"`/`" OK."` and rejects other text and failed results.
Keep `python -m unittest discover -s tests/core` and `python drills/run_drills.py` passing.

### T1B2b

TITLE: Readiness every cycle and session; guarded probes; map and dead ends in every prompt
FILES: core/bootstrap.py
TESTS: tests/core/test_readiness_cycle.py, tests/core/test_bootstrap.py, tests/core/test_live_run.py

Make the conductor in `core/bootstrap.py` run the readiness check before every cycle and session (D-030, design §4) and give every agent the capability map. Uses T1B2a's `core.readiness` helpers (`PLAIN_CHECKS`, `evaluate`, `read_map`, `write_map`, `broken`, `ok_ttl_s`, `fail_retry_s`, `max_age_for`, `probe_agents`, `PROBE_PROMPT`, `probe_ok`). The map lives at `<state>/capabilities.json` (the bootstrap ledger is rooted at `state`).

1. **Constructor.** `Conductor.__init__` gains keyword-only `checks: dict | None = None` (name → callable returning `(ok, detail)`) and `probes: dict | None = None` (capability name → agent with `.run(prompt, cwd, schema)` and `.provider`). `None` means the real ones: module functions `real_checks() -> dict(readiness.PLAIN_CHECKS)` and `real_probes(limits) -> readiness.probe_agents(limits)`. There is no "no readiness" mode. `main()` builds the Conductor as today (so it gets the real ones).
2. **Guarded run.** Factor the body of `_call` after its cap check (run record, R14/R15 fingerprint before/after, tamper KILL + halt alert + `Tampered`, metering, `output.json`) into `_guarded_run(label, agent, prompt, cwd, schema)`. `_call` keeps its cap check and then uses it. Behaviour of `_call` is unchanged apart from item 5.
3. **`_refresh_readiness(names=None, force=frozenset()) -> dict`.** Reads the old map, `now = self.clock()`. Plain checks (`self.checks`, or only those in `names`) run through `readiness.evaluate` with `limits.get("check_timeout_s", 60)`, except that an old entry with `broken(...)` None and age < `ok_ttl_s(name)` is reused (caching lives inside individual checks only). For each probe (only those in `names` if given): reuse the old entry if it is usable ok and younger than `ok_ttl_s`, or if it is a failed entry with a valid `checked_at` younger than `fail_retry_s` (neither when the name is in `force`); otherwise, if `self.meter.over(agent.provider, self.limits)`, launch nothing and keep the old entry (possibly none); otherwise launch it through `_guarded_run(f"probe-{name}", agent, PROBE_PROMPT, self.work / "_probe", None)` (folder created) and store `probe_ok`'s result with `checked_at = now`. The result keeps only names in `self.checks` or `self.probes`, is written with `write_map`, and returned. `Tampered` propagates. It never runs while `KILL` exists.
4. **Every cycle and session.** In `step()`, right after the `_capped()` check and before `drift_due` or any task, call `_refresh_readiness()`; `Tampered` → return `"killed"`. Add `session_start() -> dict`: returns `{}` and launches nothing when `KILL` or `PAUSED` exists or `_capped()`; otherwise returns `_refresh_readiness()`. In `main()`'s `run` path, call `c.session_start()` after the R31/R40 inbox and pause handling and before the smoke-stale check (and so before `_guarded_smoke`); a `Tampered` there exits 0.
5. **Prompt blocks in `_call`** (so every role, every stage, plan review and the smoke test get them; appended at the end of the prompt, never at the start): a block starting `CAPABILITY MAP (plain-code readiness check; blocker claims that contradict it are rejected):` with one line per capability, sorted, `- <name>: OK|BROKEN (<reason>) - <detail> (checked <checked_at>)` using `broken(entry, now, max_age_for(name))`, or `(no readiness evidence yet)`; at most 4000 characters. For roles `builder` and `troubleshooter` only, a `KNOWN DEAD ENDS:` block with the last 50 non-empty lines of `<state>/dead_ends.jsonl`, at most 20000 characters, when any exist. Remove the separate dead-ends block from `_build_stage` so it appears once.
6. **Migrate the existing harnesses (in the test files of this task).** In `tests/core/test_bootstrap.py` add module-level `HEALTHY_CHECKS` (git, github, gmail, docker, n8n, ollama, python_libs, browser each `lambda: (True, "ok")`) and `healthy_probes()` (FakeAgents for `claude` and `codex` answering `("ok", 0)` with those providers); `Harness.make_conductor(agents=None, limits=None, checks=None, probes=None)` passes copies of them when not given and keeps them on `self.checks` / `self.probes`. In `tests/core/test_live_run.py` pass the same to every direct `Conductor(...)` (`timed_conductor`, `RealTeamTokenCapTests`) and patch `core.bootstrap.real_checks` / `core.bootstrap.real_probes` to return them in `ReviewRoundTwoRunTests`. Adjust only what the new probe run records and prompt blocks break (e.g. exact run-record counts), keeping each test's intent.

Acceptance criteria the tests must check (fakes only; nothing real is launched: patch `core.agents.launch` to raise where real agents exist):
- each `step()` that gets past the cap check calls every plain check again (no multi-minute skip) and writes `ok`, `detail`, `checked_at` for every check and probe; a check that raises is stored `ok: False`;
- an ok probe is launched once across two steps inside `ok_ttl_s` and again after it; a failed probe is not relaunched inside `fail_retry_s` and is after; `force` relaunches; a capped provider's probe is never launched (probe FakeAgent has no prompts, meter unchanged) and `step()` returns `"capped"` without calling any check;
- probe tokens are added to the provider's meter, and a probe run writes `runs/<id>/prompt.md` and `output.json`; a probe that writes into `state/` sets `KILL` and `step()` returns `"killed"`;
- `session_start()` refreshes on a clean start and calls nothing with KILL, PAUSED or a cap; `main(["run"])` calls it after the inbox read and before `_guarded_smoke` (event order), and not at all when a STOP arrives;
- a Conductor built without `checks`/`probes` uses `real_checks()` / `real_probes(limits)` (patched to sentinels);
- prompts of the test writer, builder, reviewer, troubleshooter, drift keeper and planner, and of every role in `_guarded_smoke` with a fake team, contain `CAPABILITY MAP` and the current entries (a broken one shown as `BROKEN`); builder and troubleshooter prompts (smoke included) contain `KNOWN DEAD ENDS:` and the dead-end line once when `dead_ends.jsonl` has one; other roles' prompts do not.
The whole of `tests/core/test_bootstrap.py` and `tests/core/test_live_run.py` must pass, as must `python -m unittest discover -s tests/core` and `python drills/run_drills.py`.

### T1B2c

TITLE: Gate every launch on readiness; tasks declare needs
FILES: core/bootstrap.py
TESTS: tests/core/test_readiness_gating.py

No agent may start while a capability it needs lacks usable evidence (D-030, design §4). Builds on T1B2a (`readiness.broken`, `max_age_for`, `requirements`, `NAME_RE`) and T1B2b (`_refresh_readiness`, the map at `<state>/capabilities.json`, the `checks=`/`probes=` constructor arguments, the healthy `Harness` in `tests/core/test_bootstrap.py`). If other 1B tasks have moved code (e.g. the builder into a task worktree), apply the same rules at the equivalent launch points.

1. **Declared needs.** `S_PLAN` task items gain optional `"needs": {"type": "array", "items": {"type": "string"}}`. The planner prompt in `_plan_stage` adds: `Each task may also list "needs": capability names from the capability map (git, github, claude, codex, gmail, docker, n8n, ollama, python_libs, browser, or a new name) that its Builder needs beyond git and its own AI.` `validate_task`: `needs` is optional; if present it must be a list of at most 10 strings each matching `readiness.NAME_RE`, else the problem `bad needs` (so `init_queue` raises `ValueError` and a plan is rejected with `plan rejected: bad needs`). `_plan_stage` copies a valid `needs` into each queued task; `_new_task` defaults `needs` to `[]`; old tasks without the key count as `[]`.
2. **Requirements.** `_requirements(role, task=None) -> set[str]` = `readiness.requirements(provider of self.team.<role>, task["needs"] if role == "builder" and task else None)`. `ready_for(role, task=None, cap_map=None) -> dict[str, str]` returns `{name: reason}` for every required name whose entry `broken(entry, now, max_age_for(name, limits))` rejects (reading the map file when `cap_map` is None). This is the only predicate; later tasks reuse it.
3. **`NotReady(Exception)`** with attribute `names: dict[str, str]`. `_call(role, prompt, schema, cwd=None, needs=None)`: after the existing cap check and before anything is written or launched, compute the unready names for `{"git", provider} | set(needs or [])`. If any are only `stale evidence` or `no evidence` for a name that has a check or probe, call `_refresh_readiness(names=those)` once (it never launches a capped probe) and recompute. If still unready, raise `NotReady` (no run record, no launch). `_build_stage` passes the task's `needs` for the builder.
4. **Undo without penalty**, exactly like `Capped` (R37) at every place `Capped` is caught: test writer and planner → worktree reset, status unchanged; builder → claim released, worktree reset to the tests commit; build reviewer after submit → auditor `fail` then manager `reopen`, no failure note or signature, `fails_since` unchanged, task stays `tests_ok`; plan reviewer → reset, stays `todo`; task troubleshooter in `_after_failure` → `troubleshoot_pending` stored as for `Capped`; drift keeper → nothing changes. `_guarded_smoke` turns `NotReady` into the problem `not ready: <name>: <reason>; ...`.
5. **Choosing work in `step()`** (after the readiness refresh): a `tests_ok` task with `troubleshoot_pending` is checked against the troubleshooter's requirements only and runs first when they are met, even if the task's own `needs` are broken. Otherwise each `todo`/`tests_ok` task in queue order is checked with `ready_for` for its stage's role (plan → planner, `todo` build → test_writer, `tests_ok` → builder with needs); an unready task is skipped (its status is unchanged; it records `waiting_on` = sorted names) and the next one is tried. `drift_due` runs only if the drift keeper is ready; otherwise the loop continues to tasks (the gate stays closed while `drift_due`). The gate additionally needs `git` and `github` ready. When nothing ran because of readiness, `step()` returns `"not_ready"`; a `NotReady` raised inside a stage also returns `"not_ready"`. `run()` sleeps `idle_sleep_s` on `"not_ready"` like `"idle"`.

Test fixtures (so these tests stay valid once T1B2d routes failures): break capabilities with details that `readiness.diagnose` marks not troubleshootable, e.g. `docker` → `(False, "docker not installed or not on PATH")`, or with a need that has no check (`vpn`); build Conductors with `Harness.make_conductor(..., checks=..., probes=...)`; don't assert on emails.

Acceptance criteria the tests must check:
- with a task needing `docker` and docker failing, the test writer still runs, but the builder is never launched (its FakeAgent has no prompts), no ledger claim exists, the task stays `tests_ok` with `waiting_on == ["docker"]`, and `step()` returns `"not_ready"`; once the check passes the next step builds;
- `ready_for` blocks on every malformed-evidence case written straight into the map (missing entry, `ok: "true"`, `ok: 1`, missing / invalid / naive / future `checked_at`, stale) and passes fresh ok evidence;
- a need with no check (`vpn`) blocks the builder forever (missing evidence);
- a second task whose needs are healthy runs while the first waits;
- `troubleshoot_pending` runs when the task's need is broken but git and claude are healthy, and the builder still does not run;
- git failing: no agent of any role is launched and `step()` returns `"not_ready"`;
- a builder FakeAgent that advances the clock past `readiness_max_age_s` makes the reviewer's gate refresh (the plain check's call count rises) and continue when ok; when that refresh finds codex failing, the reviewer is not launched, the ledger contract is `open` again, the task is `tests_ok` with unchanged `fails_since` and no new signature, and a later healthy step finishes the task `done`;
- a plan returning `needs: ["docker"]` puts `needs` on the queued task; `needs: ["Bad Name!"]` is rejected with `plan rejected: bad needs`; `init_queue` raises `ValueError` for bad needs; the planner prompt mentions `needs`;
- all tasks done with github failing: no `gh pr create`, `step()` returns `"not_ready"`;
- `_guarded_smoke` with git failing launches nothing and reports `not ready: git`.
Keep `python -m unittest discover -s tests/core` and `python drills/run_drills.py` passing.

### T1B2d

TITLE: Route every failed check to the Troubleshooter or Ben's queue
FILES: core/bootstrap.py
TESTS: tests/core/test_capability_routing.py

A failing readiness check starts a Troubleshooter job or puts an item in Ben's queue with the exact fix (D-030, design §4), with contact rules D-023. Builds on T1B2a (`broken`, `diagnose`, `max_age_for`), T1B2b (`_refresh_readiness(names, force)`, `session_start`, prompt blocks) and T1B2c (`ready_for`, `NotReady`, `"not_ready"`, task `needs`, healthy `Harness` fixtures with `checks=`/`probes=`).

1. **Candidates.** `_route_capabilities(cap_map) -> str | None`: candidates are every name in the map that `broken` rejects, plus every required name missing from the map (git, each team role's provider, and the `needs` of every task that is not `done` or `blocked`). A candidate whose `diagnose(...)["depends_on"]` is itself a candidate is left to its dependency (no own job or item). Order: git first, then names that block a role or a waiting task, then the rest alphabetically. Routing state lives in `<state>/cap_routing.json`: `{name: {"rounds": int, "last_job": iso|None}}`.
2. **Resolved.** For each name whose evidence is now usable: close its open capability item (`status` `resolved`, `closed_at`), and reset its routing state.
3. **One Troubleshooter job per step at most.** The Troubleshooter is available when its own requirements (`ready_for("troubleshooter")`) are met and its provider is not capped. The first candidate with `troubleshoot` True, `rounds < limits.get("cap_trouble_max", 3)` and no job in the last `limits.get("cap_retry_h", 1)` hours gets a job: `_call("troubleshooter", prompt, S_TROUBLE, cwd=self.work / "_capfix")` (folder created; never a task worktree or `state/`). The prompt starts `You are the TROUBLESHOOTER. CAPABILITY FIX JOB: <name>` and gives the detail, the diagnosed condition and the fix line, and the rules: free vetted tools only (D-032), no paid services or new accounts, never read or print secrets, nothing visible on Ben's screen, and answer `{"kind": "fix" | "dead_end" | "suggestion", "notes": "...", "alternative": "..."}` (`_call` adds the map and KNOWN DEAD ENDS). Then: increment `rounds`, set `last_job`; a `dead_end` appends `{"capability": name, "notes", "alternative"}` to `dead_ends.jsonl`; always re-check with `_refresh_readiness(names={name}, force={name})`; if now usable, resolve as in 2. Return `"worked"`. `Capped` propagates (step returns `"capped"`), `NotReady` or an unusable answer ends the job with nothing recorded except the round.
4. **Ben's queue for everything else.** Every other candidate that is not troubleshootable, has used its rounds, or cannot get a job because the Troubleshooter is unavailable (e.g. git or claude broken, which also covers simultaneous git + github failures) gets exactly one open question of kind `capability` with fields `capability`, `condition`, `tasks` (ids whose needs include it) and `hold: true`, created through `_ask(..., hold=True)`: stored with `delivered: false`, not sent. `_handle_inbox`'s retry of undelivered questions skips held ones. Subject `Forge needs <name> fixed (<condition>)`. Body: the detail, `Paste this into PowerShell:` or `Press Win + R and paste:` followed by the fix line without its prefix, the `then` text, what waits on it, the default (`If you don't answer, Forge keeps skipping work that needs <name> and re-checks it every cycle.`), and for `no_check` how to reply `not needed`. If the condition changes, the stored subject/body are updated.
5. **Instant email only when Ben alone can unblock all progress (D-023).** After routing, if no task stage and no drift check is ready to run, no candidate can still get a Troubleshooter job now or later, and at least one capability item is open, clear `hold` on every open capability item and deliver it (mail budget R20 still applies). Otherwise they stay held (the 1E digest will carry them); `python -m core.bootstrap status` lists open capability items.
6. **Where it runs.** `step()`: after `_refresh_readiness()` call `_route_capabilities`; if it returns `"worked"`, return `"worked"`. `session_start()`: refresh, then route, then the instant-email check, so `main run` routes before the smoke test can fail and exit.
7. **Replies** (`_answer`, kind `capability`, code checked as R2): the reply is kept (`replies`, R19 caps) and the name is forced on the next refresh (`<state>/readiness_force.json`, consumed by `_refresh_readiness`). The item stays open: only usable evidence closes it. The single exception: a first line that is exactly `not needed` (case and trailing punctuation ignored) removes the capability from the `needs` of the item's `tasks` and closes the item as `answered`. It never removes git or a role's provider.

Test fixtures: `Harness.make_conductor(checks=..., probes=...)`, a controllable clock, and a troubleshooter FakeAgent that can flip a check to ok.

Acceptance criteria the tests must check:
- git failing (`git not installed or not on PATH`) with claude healthy: no Troubleshooter or other agent launched; one open `capability` item for git, and it is emailed at once (every task waits); git + github failing together: two items, no exception;
- the same through `main(["run"])` with a stale smoke stamp: the git item is emailed before `_guarded_smoke` runs and `main` returns 0;
- docker `daemon_down`, needed by the only task, troubleshooter healthy: step 1 runs one capability job (prompt has `CAPABILITY FIX JOB: docker`, the map and `KNOWN DEAD ENDS:` when present) and re-checks docker with force; if the job fixed it, the item-free next step launches the builder; if not, no second job inside `cap_retry_h`, and after 3 rounds (clock advanced) one held item exists;
- gmail `no app password` while a task is runnable: a held item exists, no email is sent, and the task proceeds in the same or next step;
- an empty map with no checks or probes configured: items for git, claude and codex (`no_evidence`), no agent launched, no exception;
- n8n failing with docker failing: only docker is routed; the fix text differs for n8n container missing vs stopped, gh missing vs logged out;
- a need `vpn` with no check: `no_check` item; a reply `still broken` keeps it open and the task waiting; a reply `not needed` removes `vpn` from the task's needs and closes it; a reply to a git item never makes git ready;
- an item closes (`resolved`) when its check passes; held items are not retried by `_handle_inbox`; a capped troubleshooter provider launches no job.
Keep `python -m unittest discover -s tests/core` and `python drills/run_drills.py` passing.

### T1B2e

TITLE: Blocker claims need evidence, may not contradict the map, and are reviewed
FILES: core/bootstrap.py
TESTS: tests/core/test_blocker_claims.py, tests/core/test_bootstrap.py

A Builder's blocker claim must show real work before it is accepted (D-031, D-033, design §4). Builds on T1B2a (`broken`, `max_age_for`, `NAME_RE`), T1B2b (map in prompts), T1B2c (`needs`, `NotReady`, gating) and T1B2d (routing of broken or unknown capabilities).

1. `S_BUILD` gains optional string properties `capability` and `meanwhile`. The builder prompt's closing instruction says a blocked answer must include `tried` (at least 2 different routes actually tried), `error` (the real error output), `capability` (the capability-map name it needs, or a new short name) and `meanwhile` (what it will work on instead), or it is rejected as an easy way out.
2. When the builder answers `status: blocked`, in this order:
   a. **Evidence:** `tried` is a list with at least 2 distinct non-empty strings, `error` is a non-empty string, `capability` matches `NAME_RE`, `meanwhile` is non-empty. Otherwise reject with reason `blocker rejected: no evidence (easy out)` (text unchanged).
   b. **Map:** if `broken(map.get(capability), now, max_age_for(capability))` is None, the claim contradicts the map: reject with `blocker rejected: contradicts capability map (<capability> is ok: <detail>, checked <checked_at>)`.
   c. **Reviewer:** `_call("reviewer", prompt, S_REVIEW)` with the task prompt, the claim fields, the map entry for the capability (or `no automatic check exists`) and the attempt's diff; the prompt tells it to fail claims without real attempts. A `fail` (or unusable answer) rejects with `blocker rejected by reviewer: <reasons>`. `Capped` or `NotReady` here undoes like a capped builder (claim released, worktree reset to the tests commit, no failure, no easy-out) and propagates.
   d. **Accepted:** append `capability` to the task's `needs` if absent (also when the map has no such check, so gating holds the builder and routing asks Ben), add a note `blocker accepted: needs <capability>; meanwhile: <meanwhile>`, then fail the attempt as today with `blocker: <summary> (tried: ...; error: ...)` (troubleshooter rules unchanged).
3. **Every rejection is an `easy_out` event:** a line `{"task", "agent": "builder", "kind": "easy_out", "reason", "capability", "summary", "at"}` in `<state>/easy_outs.jsonl`, and a ledger `run_report` (`forge-core`) whose payload has `run_id`, `claim: "blocked"`, `commit: null`, empty `changed`/`violations`/`out_of_scope` lists and `easy_out: {"reason", "capability"}`, applied before the claim is released; then the attempt fails with that reason (signature `easy-out`).
4. **Migrate** `test_builder_blocked_result_is_failed_attempt` in `tests/core/test_bootstrap.py`: the claim adds `capability: "docker"` and a `meanwhile`, and the conductor's checks make docker fail with `docker not installed or not on PATH` (not troubleshootable, so routing only files a held item and the build step still runs); it still expects `blocker: cannot proceed` in the notes. `test_r5_blocker_easy_out` stays unchanged.

Acceptance criteria the tests must check (Harness with `checks=`/`probes=`):
- claims missing each of `tried` (or only 1 route, or 2 identical), `error`, `capability` (or an invalid name), `meanwhile` are rejected with the unchanged easy-out reason, logged in `easy_outs.jsonl` with `kind: easy_out`, recorded in a ledger `run_report` with `easy_out`, and the reviewer is not called;
- a complete claim naming a capability whose evidence is fresh and ok is rejected as contradicting the map, logged the same way, reviewer not called;
- a complete claim for a failing capability goes to the reviewer (its prompt contains the claim and the map entry); a reviewer `fail` rejects and logs an easy-out; a `pass` accepts: `needs` now contains the capability, the note is present, the next step does not launch the builder while it is broken;
- an accepted claim for `vpn` (no check) is persisted into `needs` and the builder stays gated;
- a capped reviewer during claim review leaves no failure, no easy-out, the claim released and the task `tests_ok`;
- the builder prompt names all four required fields.
The whole of `tests/core/test_bootstrap.py` must pass, as must `python -m unittest discover -s tests/core` and `python drills/run_drills.py`.

## Checks against the rules

- R1: every `test_cmd` is `python -m unittest` over the task's own `test_files`, all under `tests/core/`.
- R8/R13: readiness errors inside a stage stay stage errors; nothing is silently treated as ready.
- R9/R14/R15: probes and capability jobs run through the same guarded run; the conductor writes `capabilities.json`, `cap_routing.json` and `readiness_force.json` only outside agent runs.
- R19/R20/R28: replies, notes and question bodies keep their caps; every email still goes through `_send` and its budget.
- R21/R24/R40: nothing is refreshed or launched while KILL or PAUSED is set.
- R29/R31: the smoke test runs through the gated `_call`; `session_start` runs after the R31 inbox read.
- R37/R38: a cap is checked before every launch, probes included; `NotReady` uses the same no-penalty undo; deferred troubleshooting is kept.
- D-020: capped providers are never probed; the team waits rather than swapping engines.
- D-023: capability items are held unless Ben alone can unblock all progress. Quiet hours and the daily digest are 1E work (no email path implements them yet).
- D-025/D-027: the builder still can't touch tests; nothing reaches `main` except through the gate.
- D-029: no check result or prompt contains a secret; the Gmail fix line prompts Ben for the password, it is never shown.
- D-030/D-031/D-032/D-033: covered by T1B2b–T1B2e as described.

## Reviewer notes

- New limit keys (`readiness_max_age_s`, `readiness_fail_retry_s`, `cap_trouble_max`, `cap_retry_h`) are read with `.get` defaults. Adding them to `charter/limits.json` (protected) is an open item for Ben's PR.
- The design names `ledger/capabilities.json` and `ledger/dead_ends.jsonl`; the bootstrap conductor's ledger root is `state/bootstrap`, so they live there, beside `queue.json`.
- If P1B3's tasks run first and construct Conductors outside `Harness`, those tests must pass `checks=`/`probes=` too; T1B2b's test writer can only migrate the files in its scope, so a failure there goes to the Troubleshooter or Ben.

## Reviewer notes

- T1B2a: Match actual adapter errors in diagnosis: ClaudeAgent and CodexAgent report 'agent command not found: ...' for missing executables. Recognize this as missing and return the installation command.
- T1B2b: Apply force to plain checks as well as AI probes, so post-fix and reply-triggered refreshes bypass Gmail/browser caching. Preserve unrelated entries during a targeted refresh. Clarify that the every-plain-check acceptance assertion excludes explicitly permitted per-check caching.
- T1B2c: Recheck the provider cap after an inline readiness refresh and immediately before launching the role: a metered probe can exhaust the remaining budget. For the stale-Codex test, advance beyond max_age_for('codex', limits), or override its TTL; the default six-hour TTL exceeds readiness_max_age_s.
- T1B2d: Correct the exhausted-retries test expectation: when Docker blocks the only remaining task and no troubleshooting remains possible, item 5 requires delivery with hold cleared. Expect a held item only when other progress remains possible.
- T1B2d: Complete routing of other failures before returning 'worked' for the selected repair job. Ensure unusable repair answers still receive the specified forced recheck, and make the no_check email present a reply instruction rather than a command to paste into PowerShell.
- T1B2e: Preserve the accepted-blocker handoff when adding its capability to needs. Existing _after_failure invokes task troubleshooting only after two failures; ensure the new readiness gate cannot strand the first accepted blocker before its Troubleshooter handoff.
