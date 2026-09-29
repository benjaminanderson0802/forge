# Forge: status

> Forge updates this file after every cycle once Layer 1 runs. Until then, whoever finishes a piece of work updates it. Keep it short: where things stand now, not history (history is in git and the ledger).

**Updated:** 2026-09-29 (evening)

## Now

- Phase F (Foundation). The bootstrap conductor is merged. Its first live start (2026-09-29) exposed faults that fake-based tests could not see:
  - its own lock crashed the tamper check (fixed: PR #8, R15–R16);
  - Codex rejected Forge's answer schemas;
  - Forge read its own emails as Ben's replies and emailed him about 27 times, the messages doubling in size.
- Forge was stopped and its scheduled task disabled. Branch `live-run-hardening` adds spec amendments R17–R40, each pinned by Codex-written tests:
  - real schemas;
  - self-mail rejected three ways, and Ben's read flags never touched;
  - a mail budget (6 an hour, 30 a day, counted per attempt);
  - KILL silences everything except one halt alert per 12 hours;
  - nothing grows without bound;
  - a live smoke test of every real agent before starting;
  - token caps enforced before every agent launch (they never were: the real agents had no provider id).
- Evidence: 170 unit tests pass on Windows and Linux, drills 1–10 pass, and the live smoke test with the real Codex and Claude agents passes. Codex review took 10 rounds and ended with a pass and no findings.
- Next: Ben approves the pull request. Then the incident's state is archived, the queue is re-initialised, the smoke test is run, and the scheduled task is re-enabled.

## Known risks

- Claude agents can run Python, and Python can read or write any file Ben's Windows account can. Writes to Forge's state are detected (the tamper alarm halts everything and emails Ben). Reads, and writes elsewhere, are not blocked. The strong fix, running agents under a separate Windows user or in a sandbox, is planned for Layer 3.

## Machine (Ben's PC)

- **Forge core:** `C:\Users\benja\Forge`. Drills 1–10 pass.
- **Repo:** `benjaminanderson0802/forge` (public). `main` is protected, including against admins.
- **Installed:** Python 3.12 and 3.13, Node LTS, Git, the GitHub command-line tool, Docker (virtualization enabled in BIOS), n8n at `http://localhost:5678`, Ollama, FFmpeg, yt-dlp, Tesseract, LibreOffice, Chrome, Bitwarden plus its command-line tool, and the Claude Code and Codex command-line tools.
- **Signed in:** GitHub (command-line tool and token), Codex, Bitwarden (logged in, vault locked), and Claude Code (headless verified).
- **Gmail:** app password verified for sending and reading. Stored in Windows Credential Manager (`forge-gmail`), with the record copy in Bitwarden.

## Open items for Ben

- Approve the live-run-hardening pull request (one "y").
