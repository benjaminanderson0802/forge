# Forge: status

> Forge updates this file after every cycle once Layer 1 runs. Until then, whoever finishes a piece of work updates it. Keep it short: where things stand now, not history (history is in git and the ledger).

**Updated:** 2026-09-30 (07:50 UTC)

## Now

- **Building 24/7 (D-037, PR #14).** Ben's approval to build is standing: builders label and merge their own tested, Codex-reviewed changes. The hourly "Forge supervisor" scheduled task keeps building from `docs/SUPERVISOR.md`, whether or not a chat is open. Its log is `C:\Users\benja\Forge-work\supervisor-log.md`.
- **Merged today:**
  - **PR #12, R41:** plans carry complete tasks.
  - **PR #15, R42:** Stop Forge during a run is a stop, not tampering.
  - **PR #17, R43:** token caps count cached input at one tenth, the real cost. Before this, one plan run registered as about 4.6M tokens.
  - **PR #16:** the 1B plan is split into P1B1 (judges), P1B2 (readiness) and P1B3 (role files, worktrees, merge). The whole-1B plan timed out at 30 minutes, and its review found 10 real gaps. Each part carries its share of those findings.
- **Conductor:** restarted 07:43 UTC on the merged code, with the queue split applied.
  - Queue: P1B1, P1B2, P1B3, P1C, P1D and P1E to plan.
  - 1A readiness is done (T1A3R). T1A3 is kept as a blocked record.
  - P1C's first plan was rejected by review with fair findings, which its next attempt carries.

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

- **ChatGPT Dots evaluated (2026-09-30):** not adopted for now. Email stays Forge's channel. A Dot round-trip trial is part of 1E, and the criteria are in `docs/specs/layer-1-design.md`.
- **CI does not run the core unit tests.** `.github/workflows/core-checks.yml` has no unit-test step. Add one (supervisor, under D-037).
- **Plan size:** a planner call has 30 minutes. If a part still times out, split it further rather than raising the timeout.
