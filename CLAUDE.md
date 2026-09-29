# Forge — read this first

Forge is Ben's autonomous build-and-venture system. This repo is its memory. **No agent should rely on chat history.** Everything that matters is written here.

## Before any substantive work, read in this order

1. `docs/PURPOSE.md`: why Forge exists and what it must never do. Every task must trace back to it.
2. `docs/DECISIONS.md`: every standing decision. Follow the latest entry on each topic.
3. `docs/STATUS.md`: where things stand right now.
4. `docs/ROADMAP.md`: phases, their gates, and venture order.
5. The plan for the phase you're working on, in `docs/superpowers/plans/`.
6. `PLUGINS_HANDOFF.md` and `GITHUB_ACCESS.md` (on Ben's PC): which tools to use, and how to reach GitHub.

`docs/source/` holds snapshots of the claude.ai planning docs (the Forge research plan, the master build plan, ventures 1–8). The claude.ai versions are the editable originals.

## Rules

- **Evidence before claims.** Never say done, fixed or passing without running the check and showing its output.
- **Stay on purpose.** If the work in front of you doesn't serve the current phase's plan, stop. Don't wander into it. Note it in `docs/STATUS.md` under "Open items", or ask Ben.
- **Rabbit-hole limit.** Stop after 3 failed approaches to the same problem, or about 45 minutes. Write down what you tried and hand it back. Don't keep digging.
- **Protected files:** `core/`, `drills/`, `tests/acceptance/`, `charter/`, `spec/`, `.github/`, `roles.json`, `CODEOWNERS`, `docs/PURPOSE.md`, `docs/DECISIONS.md`. Changes go through a pull request with Ben's `human-approved` label. Never apply that label yourself unless Ben has approved in this conversation.
- **Secrets:** never print, commit, log or ask for tokens or passwords. Credentials live in Bitwarden, the Windows keyring and user environment variables.
- **Money:** follow `charter/authority.md`. Uncovered spend and new subscriptions go to Ben.
- **Honest and legal:** real identity, official APIs, platform rules respected, no CAPTCHA bypass. When an idea crosses a line, build the compliant version.
- **Don't interfere with Ben.** Nothing may take over his screen, mouse or keyboard while he's using the PC.
- **Workflow:** Superpowers (brainstorming → writing-plans → executing → verification-before-completion). Test first. Commit often. Keep drills and core unit tests passing: `python drills/run_drills.py`, `python -m unittest discover -s tests/core`.
- **Talking to Ben:** plain language. Any step he must do comes as an exact line to paste into Win + R or PowerShell, never just a folder or file name. No quotes around paths in those lines.
- **Before ending a session,** update `docs/STATUS.md`. Record any new decision Ben made as a proposed entry in `docs/DECISIONS.md`, in a pull request he approves.

## Environment (Ben's Windows PC)

- **Repo:** `C:\Users\benja\Forge`. Python is on PATH. Call Claude Code as `claude.cmd` from automation shells.
- **Shells:** from PowerShell, run `gh` and `git` as `cmd /c "..."` when stderr would be treated as an error.
- **Line endings:** write files as LF bytes. Drills must pass on both Windows and Linux.
