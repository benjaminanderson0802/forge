# Forge — trusted core (Phase 0)

**Agents and new sessions: read `CLAUDE.md` first.** It points to the purpose, decisions, status and roadmap in `docs/`.

This is the part of Forge that contains **no AI**. Everything the agents do later
has to pass through it, and nothing Forge does can change it without you.

## What's here

| Path | What it does |
| --- | --- |
| `core/ledger.py` | The ledger. Agents submit *proposals*; the core accepts or rejects them. Enforces schema, roles, allowed state changes, evidence for "done", token/attempt budgets, the kill switch, idempotency and crash-safe writes. Every accepted change is an event in a hash-chained, append-only log. |
| `core/protect.py` | Protected paths Forge may never change: `core/`, `drills/`, `tests/acceptance/`, `charter/`, `spec/`, `.github/`, `roles.json`, `CODEOWNERS`. |
| `core/runner.py` | Takes an agent through one attempt: claim, run, put back any protected file it touched (ledger included), record what changed and what it claimed, submit. `resume` recovers after a crash without redoing finished work. |
| `core/cli.py` | `verify`, `apply`, `protect`, `run-acceptance`, `status`, `approve-spec`, `resume`. |
| `drills/run_drills.py` | Sabotage drills 1–21 (1–10 trusted core, 11–21 Layer 1 gates). Each plants a failure and checks the core catches it. |
| `.github/workflows/core-checks.yml` | Runs on every pull request: protected-path check, ledger verify, drills, acceptance tests. |
| `charter/authority.md` | Draft Authority Charter (you fill in budgets). |
| `roles.json` | Maps identities to roles. |
| `scripts/setup_github.sh` | One-time: creates the private repo and turns on branch protection. |

## Trust model

- Agents never edit `ledger/` directly. They write proposal files; the
  orchestrator (plain code, built in the next layer) launches each agent under
  one identity and applies its proposals with that identity. An agent can't
  choose its own role.
- A contract reaches **done** only when CI recorded a passing acceptance run for
  the exact commit the executor submitted, the runner's report shows that attempt
  touched no protected or out-of-scope files, and an auditor (a different
  identity) passes it.
- Once you run `python -m core.cli approve-spec`, the spec is frozen: if
  `spec/spec.md` changes, every agent is refused until you approve it again.
- An executor that says "done" and then fails its audit is logged as a false
  claim (`Ledger.false_claims()`), which the Learner uses later.
- On GitHub, branch protection with `enforce_admins` on means nothing reaches
  `main` without the `core` check passing — not even with your own credentials.
  Protected-path changes pass only if **you** (the `FORGE_OWNER` login) applied
  the `human-approved` label; an agent adding the label doesn't count.
- Agents should push with a separate fine-grained token (contents + pull
  requests only), never your personal login.

## Run the drills

```
python drills/run_drills.py
```

Verified in the build environment: all 10 drills pass. 22 deliberately broken
versions of the core (8 for drills 1–5, 14 for drills 6–10: spec check removed,
run report not required, protected files not restored, ledger not restored,
scope check removed, false claims not recorded, crash releases not counted, and
more) were each caught by at least one drill.

## Set up GitHub (about 5 minutes)

Needs Python 3.10+, Git and the GitHub CLI (`gh auth login` first).

```
cd forge
git init -b main && git add . && git commit -m "Forge trusted core"   # skip if already a git repo
bash scripts/setup_github.sh <your-github-username>
```

Then run the **gate test** to prove GitHub is enforcing it:
1. `git checkout -b gate-test`, edit any file in `tests/acceptance/`, commit, push, open a PR.
2. The `core` check must fail and the Merge button must be blocked.
3. Close the PR and delete the branch.

## Not verified yet

- GitHub-side enforcement (branch protection, owner-only label) runs only on
  GitHub; the gate test above is how you verify it.
- Drills 11 and up belong to later layers (Auditor, Challenger, Account
  Factory, Learner, Scout) and are added as those layers are built.
