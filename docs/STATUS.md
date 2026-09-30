# Forge: status

> Forge updates this file after every cycle once Layer 1 runs. Until then, whoever finishes a piece of work updates it. Keep it short: where things stand now, not history (history is in git and the ledger).

**Updated:** 2026-09-29 (evening)

## Now

- **First watched live cycle (D-035), 2026-09-29, 19:53-20:57 UTC. Forge was stopped afterwards, pending Ben.**
  - **Safety held:** 3 emails, all legitimate (start, T1A3 blocked, P1B blocked). No self-reads, no errors. The live smoke test passed. Tokens used: about 4.2M Claude and 1.8M Codex.
  - **The team worked as designed.** Codex wrote tests that failed before the feature existed. Claude built. Codex reviewed with real findings. The troubleshooter diagnosed the builder going in circles.
  - **T1A3 (readiness) is blocked:** the ledger parked it after 6 attempts. Reviews kept demanding guaranteed cancellation of hung checks, which Python threads can't provide. Unparking is a human-only ledger action, and the attempt count stays at 6, so this needs Ben. The recommended design (the troubleshooter's option A) is in the last troubleshooter run record.
  - **P1B (1B plan) is blocked:** the reviewer rejected the plan twice. Its main systemic point, which also applies to P1C-P1E: the plan tasks carry only short labels, not the full instructions from `docs/specs/layer-1-design.md`. The queue needs richer plan-task sections before planning resumes.
- **Earlier today:** the live-run hardening (R17-R40, PR #9) and the incident record (`docs/incidents/2026-09-29-email-flood.md`).

## Known risks

- Claude agents can run Python, and Python can read or write any file Ben's Windows account can. Writes to Forge's state are detected (the tamper alarm halts everything and emails Ben). Reads, and writes elsewhere, are not blocked. The strong fix, running agents under a separate Windows user or in a sandbox, is planned for Layer 3.

## Machine (Ben's PC)

- **Forge core:** `C:\Users\benja\Forge`. Drills 1–10 pass.
- **Repo:** `benjaminanderson0802/forge` (public). `main` is protected, including against admins.
- **Installed:** Python 3.12 and 3.13, Node LTS, Git, the GitHub command-line tool, Docker (virtualization enabled in BIOS), n8n at `http://localhost:5678`, Ollama, FFmpeg, yt-dlp, Tesseract, LibreOffice, Chrome, Bitwarden plus its command-line tool, and the Claude Code and Codex command-line tools.
- **Signed in:** GitHub (command-line tool and token), Codex, Bitwarden (logged in, vault locked), and Claude Code (headless verified).
- **Gmail:** app password verified for sending and reading. Stored in Windows Credential Manager (`forge-gmail`), with the record copy in Bitwarden.

## Open items for Ben

- **ChatGPT Dots evaluated (2026-09-30):** not adopted for now. Email stays Forge's channel. A Dot round-trip trial is part of 1E, and the criteria are in `docs/specs/layer-1-design.md`. Ben's included Dot can do research outside the trusted core.


- **T1A3 task definition was unsatisfiable** (fixed 2026-09-29, in this pull request). The task text included plan steps 5–7: the CI workflow (a protected file outside the builder's scope), the live gate on Ben's PC, and the layer PR. The reviewer rightly failed the builder for missing them, twice. The task is now Steps 1–4, and reviewers are told the judges already ran on the PC. Lesson: every task in a queue must be fully doable within its own `files_in_scope`.
- **CI does not run the core unit tests.** `.github/workflows/core-checks.yml` has no unit-test step (T1A3 plan, Step 5). It is a protected file, so it needs a pull request and Ben's approval.
- **Bug: Stop Forge during an agent run trips the tamper alarm.** The shortcut writes `state/bootstrap/KILL`, and `state/` is fingerprinted around every agent run. Forge still stops, but its email says "tampered". The fix (core, so it needs Ben) is for KILL and PAUSED appearing during a run to count as a stop, not as tampering. Until then, stop Forge between runs, or disable the task and end its process.


- Approve the live-run-hardening pull request (one "y").
