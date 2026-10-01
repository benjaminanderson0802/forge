# Forge: status

> Forge updates this file after every cycle once Layer 1 runs. Until then, whoever finishes a piece of work updates it. Keep it short: where things stand now, not history (history is in git and the ledger).

**Updated:** 2026-10-01 (14:45 UTC)

## Now

- **Building 24/7 (D-037).** Builders label and merge their own tested, Codex-reviewed changes. The supervisor's brief is `docs/SUPERVISOR.md`; its log is `C:\Users\benja\Forge-work\supervisor-log.md`.
- **Phase 1 gate project passed (PR #32, 2026-10-01).** Forge built `projects/entry_ledger` from its spec with no human code: plan, 3 build tasks, judges, Codex reviews, the full-suite layer gate and CI.
- **Layer 1 coverage was 0 of 40.** Most Layer 1 code was built through reviewed pull requests before the coverage ledger, and the plan stage dropped `covers`, so nothing could be credited. The drift keeper paused Forge for it. Fixes:
  - **R62 (#34):** planned tasks name the requirements they cover; the reviewer checks each claim.
  - **R63:** evidence tasks prove requirements the existing code already meets (tests pass now, fail on an empty implementation, mutation-test the code they name, Codex review).
- **Next queue (layer `layer-1r`):** one plan task per sub-plan, 1A to 1E, covering all 40 requirements of `docs/specs/layer-1-design.md`. Evidence tasks where the code exists, ordinary tasks for gaps. Spec coverage on the status page is the Layer 1 progress measure.
- **Also today:** R55 (parallel suite judge, task ordering), R57 to R59 (fast per-task judges, layers track main, skip tests of unbuilt tasks), the taskbar tray icon (#33), Claude daily cap 20M (Ben, #27). Lanes (parallel conductors, PR #31) are paused by Ben.

## Known risks

- Claude agents can run Python, and Python can read or write any file Ben's Windows account can. Writes to Forge's state are detected (the tamper alarm halts everything and emails Ben). Reads, and writes elsewhere, are not blocked. The strong fix, running agents under a separate Windows user or in a sandbox, is planned for Layer 3.

## Machine (Ben's PC)

- **Forge core:** `C:\Users\benja\Forge`. Drills 1–10 pass.
- **Repo:** `benjaminanderson0802/forge` (public). `main` is protected, including against admins.
- **Installed:** Python 3.12 and 3.13, Node LTS, Git, the GitHub command-line tool, Docker (virtualization enabled in BIOS), n8n at `http://localhost:5678`, Ollama, FFmpeg, yt-dlp, Tesseract, LibreOffice, Chrome, Bitwarden plus its command-line tool, and the Claude Code and Codex command-line tools.
- **Signed in:** GitHub (command-line tool and token), Codex, Bitwarden (logged in, vault locked), and Claude Code (headless verified).
- **Gmail:** app password verified for sending and reading. Stored in Windows Credential Manager (`forge-gmail`), with the record copy in Bitwarden.

## Needs Ben (not build approvals; building continues around these)

- Turn on **"Require this computer"** for the "Forge supervisor (hourly builder)" scheduled task in the Claude desktop app, so it can reach the PC.
- Keep the PC awake. In PowerShell as admin: `powercfg /change standby-timeout-ac 0`

## Open items

- **Flaky test:** `test_live_run.R39SmokeCleanupTests.test_R39_guarded_smoke_logs_busy_folder_without_failing` and `test_R40_cli_waits_for_unpause_before_stale_smoke_and_run` each failed once in CI and passed on rerun. Likely cause: they patch `core.bootstrap.time.sleep`, which is the global `time.sleep`, so a background thread left running by another test (the R39 failure saw millions of sleep calls) is counted too. Fix: patch a module-local sleep hook, or find and stop the leaked thread.
- **Flaky on Windows:** `test_service.HealthTests.test_thread_beats_until_stopped_then_marks_exited` hit PermissionError reading heartbeat.json while the thread replaced it (passed on rerun). Fix: retry the read, or write the heartbeat via a temp file and `os.replace` with a read retry.
- **tests/acceptance vs tests/core:** the design (2.2) puts the test writer's files in `tests/acceptance/`; the conductor and Manager use `tests/core/`. Plans are told it is a known open item, not a reason to fail.
- **ChatGPT Dots evaluated (2026-09-30):** not adopted for now. Email stays Forge's channel. A Dot round-trip trial is part of 1E, and the criteria are in `docs/specs/layer-1-design.md`.
- **Codex runs that time out are not metered.** Codex keeps no session log under `--ephemeral` and reports usage only on a completed turn (see R46).
- **Plan size:** a planner call has 30 minutes. If a part still times out, split it further rather than raising the timeout.

## Layer 1 integrated (branch `layer-1`, 2026-09-30)

One branch now holds all of Layer 1: `python -m unittest discover -s tests/core` passes (916 tests, 1 skipped) and `python drills/run_drills.py` passes drills 1–21.

- **1B conductor pipeline:** role files, readiness gating and capability routing, blocker claims, judges with the weak/empty-implementation check and the mutation gate, task worktrees, crash-safe finalization (merge journal).
- **R42–R48 (main):** stop during a run, cache weighting, plan reviewer notes, 3 plan attempts, metering failed Claude runs, planner memory, provider limit holds. Also D-037/D-038, the CI unit-test step and `docs/SUPERVISOR.md`.
- **1C planning and drift:** coverage map, drift and stall rules, the read-only Manager, focus time. Drills 11–13.
- **1D always-on service:** service loop, heartbeat, activity awareness, stop mid-cycle, limit holds, runs-per-day cap, watchdog. Spec R49–R53 (renumbered from 1D's R41–R45). Its stop and holds are one mechanism each with R42 and R48. Drills 14–16.
- **1E Ben's channel:** `queue.jsonl`, instant vs digest, quiet hours, status page, drop-folder answers (spec E1–E5). Drills 17–21.
- **Integration fixes (R54–R56):** a reviewer outage never costs an attempt (ledger `withdraw`), and builders wait for the reviewer; a finalization awaiting review no longer blocks other work or the capability email; line endings (CRLF role files, byte-exact restore). A Stop pressed during an agent run is a clean stop everywhere.
- **Open:** a builder that is itself capped or stopped still uses the ledger's `release`, which counts an attempt (unchanged by design; only reviewer outages use `withdraw`). The Windows fixes were reproduced and checked on Linux with `core.autocrlf=true`; the suite still has to be run on Ben's PC.

## Layer 1 contents (integrated 2026-09-30)

- **1A readiness:** a capability map, checked every cycle. No launch without evidence. Failed checks are routed to the Troubleshooter or to Ben's queue.
- **1B pipeline:**
  - **Tests:** written by Codex, and rejected if they pass on the current code or on an empty implementation.
  - **Build:** the builder works in a per-task worktree.
  - **Judges:** at the exact commit, in a throwaway worktree: the task tests, the drills, and mutation testing (kill rate ≥ `mutation_min`).
  - **Review:** by Codex; verdicts are recorded in the ledger. Blocker claims must show evidence.
  - **Finish:** a crash-safe finalizer (merge journal, approved-merge registry, safe push). The ledger recovers from crashes.
- **1C planning and drift:**
  - The Manager plans from the spec and ledger only.
  - A coverage map must rise: 3 merges with no gain, or 2 active hours with no merge, triggers a re-plan.
  - The 20-minute builder focus rule applies.
- **1D always-on:**
  - A service loop with a heartbeat thread and a watchdog task.
  - Active vs idle modes.
  - A mid-cycle stop (R42/R49).
  - Limit-window holds (R48/R50) and a runs-per-day cap (R51).
- **1E Ben's channel:**
  - queue.jsonl;
  - instant email for urgent kinds, a daily digest for the rest (off until `digest_hour` is set, per D-035);
  - quiet hours;
  - a local status page (127.0.0.1:8765) with Stop and Answer.
