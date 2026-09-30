# Forge: status

> Forge updates this file after every cycle once Layer 1 runs. Until then, whoever finishes a piece of work updates it. Keep it short: where things stand now, not history (history is in git and the ledger).

**Updated:** 2026-09-30 (10:30 UTC)

## Now

- **Building 24/7 (D-037).** Builders label and merge their own tested, Codex-reviewed changes. The hourly supervisor's brief is `docs/SUPERVISOR.md`; its log is `C:\Users\benja\Forge-work\supervisor-log.md`.
- **Conductor fixes merged today, each with tests written first by Codex, a Codex review, and CI:**
  - **R42 (#15):** a stop during a run is a stop, not tampering.
  - **R43 (#17):** cached input counts at one tenth.
  - **R44 (#20):** the plan reviewer blocks only for blocking problems; its notes travel with the tasks; it sees the whole plan (it had been cut at 20,000 characters).
  - **R45 (#22):** plans get 3 attempts, and the planner checks itself against the rules.
  - **R46 (#23):** killed or timed-out Claude runs are metered from their session logs.
  - **R47 (#24):** the planner sees every earlier rejection.
  - **Also:** D-038, the mutation gate is a rate (#21); CI runs the core unit tests (#19).
- **Layer 1 queue:**
  - **Plans passed:** P1B1 (judges; T1B1a-e queued) and P1B3 (role files, worktrees, safe merge; T1B3a-e queued).
  - **P1B2 (readiness):** re-planning with the full rejection history after 3 attempts, each finding one real gap.
  - **Still to plan:** P1C, P1D, P1E.
  - **Then** the 10 queued build tasks run.
- **Token caps:** today's Claude meter was recounted from session logs with R43 weighting (the cap is unchanged). Planning uses about 1.7M weighted tokens an hour, so the 10M daily cap is reached around midday UTC. Raising it is Ben's call.

## Known risks

- Claude agents can run Python, and Python can read or write any file Ben's Windows account can. Writes to Forge's state are detected (the tamper alarm halts everything and emails Ben). Reads, and writes elsewhere, are not blocked. The strong fix, running agents under a separate Windows user or in a sandbox, is planned for Layer 3.

## Machine (Ben's PC)

- **Forge core:** `C:\Users\benja\Forge`. Drills 1–10 pass.
- **Repo:** `benjaminanderson0802/forge` (public). `main` is protected, including against admins.
- **Installed:** Python 3.12 and 3.13, Node LTS, Git, the GitHub command-line tool, Docker (virtualization enabled in BIOS), n8n at `http://localhost:5678`, Ollama, FFmpeg, yt-dlp, Tesseract, LibreOffice, Chrome, Bitwarden plus its command-line tool, and the Claude Code and Codex command-line tools.
- **Signed in:** GitHub (command-line tool and token), Codex, Bitwarden (logged in, vault locked), and Claude Code (headless verified).
- **Gmail:** app password verified for sending and reading. Stored in Windows Credential Manager (`forge-gmail`), with the record copy in Bitwarden.

## Needs Ben (not build approvals; building continues around these)

- Decide whether to raise `claude_daily_token_cap` (10M a day, weighted) to 20M. It shares his Claude plan allowance.

- Turn on **"Require this computer"** for the "Forge supervisor (hourly builder)" scheduled task in the Claude desktop app, so it can reach the PC.
- Keep the PC awake. In PowerShell as admin: `powercfg /change standby-timeout-ac 0`

## Open items

- **ChatGPT Dots evaluated (2026-09-30):** not adopted for now. Email stays Forge's channel. A Dot round-trip trial is part of 1E, and the criteria are in `docs/specs/layer-1-design.md`.
- **Codex runs that time out are not metered.** Codex keeps no session log under `--ephemeral` and reports usage only on a completed turn (see R46).
- **Plan size:** a planner call has 30 minutes. If a part still times out, split it further rather than raising the timeout.
