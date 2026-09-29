# Incident: first live run flooded Ben's inbox (2026-09-29)

## What happened

- **12:12:** Ben started the bootstrap conductor. Its first agent run crashed at once, because its own lock broke the tamper check (Windows only). PR #8 fixed that.
- **17:15:** On restart, the test writer (Codex) failed twice, because Codex rejected Forge's answer schemas. The task was marked blocked and Ben was emailed.
- The conductor then read its own outgoing email in Ben's inbox as Ben's reply. The same Gmail account sends and receives, and the subject carried a valid reply code. It pasted the whole email into the task notes and retried. The next email included those notes.
- The result was 15 "blocked" emails in 4 minutes, doubling in size up to 8 MB, plus a "conductor started" email on every 5-minute watchdog restart (about 12).
- Claude stopped the conductor, then disabled its scheduled task.

## Root causes

1. **Evidence was fake-only.** Every test used fake agents, fake email and fake IMAP. The conductor was called "built, tested, reviewed to a pass" and switched on with nothing proving that real Codex, real Claude and real Gmail behave the way the fakes did. That is a false claim of readiness: the exact failure PURPOSE.md says Forge must never repeat.
2. **Nothing bounded the damage.** There was no email budget, no size caps, and nothing kept Forge from reading its own mail. One bug could therefore multiply without limit.
3. **A stop meant only a pause.** KILL still let the inbox be processed and "started" emails be sent.
4. **Token caps were never enforced.** The real agents had no provider id, so the cap check always passed. Found in review.

## What now prevents it (structural, not promises)

- **Live smoke test (R23, R29):** before the conductor starts, every real agent runs once against the real services. It is repeated daily, and nothing runs if it fails.
- **Mail budget (R20, R25):** a hard cap, counted per attempt, set in `charter/limits.json`. A loop cannot send more than the budget, whatever the bug.
- **Self-mail rejected three ways (R18, R26),** and a first-start baseline, so no old email can count as an answer.
- **Size caps (R19, R28),** so nothing can grow without bound.
- **KILL silences everything except one halt alert per 12 hours (R21, R24);** STOP works from any reply (R27, R31).
- **Token caps checked before every agent launch (R37).**
- **Process rules (D-035, D-036, CLAUDE.md):**
  - Nothing that sends, spends, posts or runs unattended is switched on without a live test against the real services, and without its damage capped.
  - Its first cycle is watched.
  - Fix-and-review loops are time-boxed, with a check-in with Ben.

## What went wrong in the response

- **The time box was broken.** CLAUDE.md sets a rabbit-hole limit: stop after 3 failed approaches or about 45 minutes, then hand back. The hardening ran for about 2 hours 15 minutes, across 10 Codex review rounds, before a status update, and only because Ben asked. The work stayed on the goal, and every round found a real defect, but Ben should have been told at about 45 minutes and given the choice. D-036 makes this explicit.
- **Tooling mistakes:** dependent tool calls were sent in parallel, and patches were built from a stale copy. Two Codex runs had to be restarted. The rule now is to sync to the branch before building a patch, and to never run dependent steps in parallel.
