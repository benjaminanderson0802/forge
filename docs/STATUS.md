# Forge: status

> Forge updates this file after every cycle once Layer 1 runs. Until then, whoever finishes a piece of work updates it. Keep it short: where things stand now, not history (history is in git and the ledger).

**Updated:** 2026-09-29

## Now

- Phase 0 is done. Phase F (Foundation): repo memory written; grilling finished (D-018 to D-034).
- **Bootstrap conductor** (`core/bootstrap.py`) is built:
  - Codex wrote the tests (38 conductor tests; 57 unit tests in all), and they pass on Windows and Linux. Drills 1–10 pass.
  - Mutation testing caught every single-rule breakage of the safeguards.
  - Codex review: fail, fail, fail, then **pass**. 17 findings fixed, each pinned by a test.
- Next: Ben approves. Then `scripts/start_conductor.ps1` starts it hidden on the PC, and it builds the rest of Layer 1 from `docs/specs/layer-1-queue.json` with no human relay.

## Known risks

- Claude agents can run Python, and Python can read or write any file Ben's Windows account can. Writes to Forge's state are detected (the tamper alarm halts everything and emails Ben). Reads, and writes elsewhere, are not blocked. The strong fix, running agents under a separate Windows user or in a sandbox, is planned for Layer 3.

## Machine (Ben's PC)

- **Forge core:** `C:\Users\benja\Forge`. Drills 1–10 pass.
- **Repo:** `benjaminanderson0802/forge` (public). `main` is protected, including against admins.
- **Installed:** Python 3.12 and 3.13, Node LTS, Git, the GitHub command-line tool, Docker (virtualization enabled in BIOS), n8n at `http://localhost:5678`, Ollama, FFmpeg, yt-dlp, Tesseract, LibreOffice, Chrome, Bitwarden plus its command-line tool, and the Claude Code and Codex command-line tools.
- **Signed in:** GitHub (command-line tool and token), Codex, Bitwarden (logged in, vault locked), and Claude Code (headless verified).
- **Gmail:** app password verified for sending and reading. Stored in Windows Credential Manager (`forge-gmail`), with the record copy in Bitwarden.

## Open items for Ben

- Approve the bootstrap conductor (one "y"), then run the start script.
