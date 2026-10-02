# Phase 6A: the venture spec, the Interviewer and the venture ledger (implementation plan)

Source of truth: `docs/specs/phase-6-design.md`, sections 2 and 3 (proposed D-064 to D-068). This plan covers only
P6A. The action gate, Executor and money (P6B), the loop, Check and calls (P6C), reports and surfaces (P6D), the
GitHub App (P6E) and the gate drills (P6F) are planned separately and build on this.

## Ground rules for every task

- Test first: each task's tests are new files under `tests/core/`, written before the build. `test_cmd` is always
  `python -m unittest <that file>` (R1). No existing test file is edited.
- Every existing test in `tests/core/` and every drill in `drills/run_drills.py` keeps passing. In particular:
  `Team` keeps its six dataclass fields (`test_live_run` asserts `vars(team)` holds exactly the six roles),
  `core.roles.ROLE_NAMES` and `core.roles.DEFAULTS` stay exactly as they are (`test_roles` asserts them), a
  contract without a `kind` behaves exactly as today, and a conductor whose repo has no `projects/ventures/`
  folder behaves exactly as today.
- Nothing in this plan performs an external effect, spends, creates an account or messages anyone other than
  Ben. Messages to Ben go only through the conductor's existing `_ask` (Ben's channel). Approvals (spec approval,
  blocker clearing) are accepted only from Ben's own email (`channel.EMAIL_ONLY_KINDS`), never from the drop
  folder.
- Venture events live in the conductor's own ledger, `Ledger(state, ventures_dir=<repo>/projects/ventures)`.
  Plain-code events are applied as `forge-core`; Ben's are applied as `benjamin` only when they come from his
  authenticated email reply (exactly as the existing gate answer does).
- Spec hashes everywhere are the canonical hash: sha256 of
  `json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")`, hex.
- Line endings: write files as LF.

## Task order

T6Aa (ledger) first. T6Ab (validator, schema, venture repo) needs T6Aa. T6Ac (Interviewer role, schema, smoke) is
independent. T6Ad (interview flow, approval, freeze, amendment) needs T6Aa, T6Ab and T6Ac. T6Ae (blockers,
waiting, the runner) needs T6Aa, T6Ab and T6Ad. Each task lists `depends_on` (R55).

| Task | Covers |
|---|---|
| T6Aa | 1.1 |
| T6Ab | 3.1, 3.2, 3.3 |
| T6Ac | 2.1 |
| T6Ad | 3.4 |
| T6Ae | 3.5 |

Not claimed here (owned by later parts): 4.11 (approval scope, caps and expiry are validated by P6B; T6Aa only
makes `approval_granted` and `approval_revoked` human-only), 2.9 (the Operator and the auditor venture mode are
P6B/P6C), 14.1 (the part as a whole).

---

## T6Aa: venture contracts and the ledger action `venture`

- files_in_scope: `core/ledger.py`, `roles.json`
- test_files: `tests/core/test_venture_ledger.py`
- test_cmd: `python -m unittest tests/core/test_venture_ledger.py`
- covers: 1.1
- depends_on: none

### Section

Extend the ledger (`core/ledger.py`, plain code) so a venture can be a ledger contract and every venture fact is a
hash-chained event (Phase 6 design 2 and 3). Add the identities to the protected `roles.json`. Nothing else
changes in this task.

1. `roles.json`: add `"forge-interviewer": "interviewer"` and `"forge-operator": "operator"`. Every existing
   mapping stays; `benjamin` stays the only identity whose role is `human`.
2. Module additions: `CONTRACT_KINDS = ("task", "venture")`; `VENTURE_SLUG_RE =
   re.compile(r"^[a-z0-9][a-z0-9-]{0,37}$")`; `VENTURE_TEXT_MAX = 2000`; `canonical_hash(obj) -> str` (sha256 hex
   of `json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")`);
   `VENTURE_KINDS`, a dict from event kind to the roles that may write it:
   `approval_granted`, `approval_revoked`, `blocker_cleared`, `resumed`, `killed`: `{"human"}`;
   `paused`: `{"human", "core"}`; `request`: `{"operator", "interviewer", "core"}`;
   `spec_amend`: `{"interviewer", "core"}`; `decision`, `executed`, `refused`, `revenue`, `cost`, `cycle`, `report`,
   `first_customer`: `{"core"}`.
3. `Ledger.__init__(self, root, ventures_dir=None)`: `self.ventures_dir` is `Path(ventures_dir)` or
   `root/"projects"/"ventures"`. `derive()` keeps building its replay ledger with `Ledger(tmp)` (disk checks are
   skipped while replaying).
4. Contracts: a contract may carry an optional field `kind` (str in `CONTRACT_KINDS`); a missing `kind` means a
   task contract and behaves exactly as today. `_check_contract` accepts `kind` and rejects any other value. On
   `create`: a contract with `kind == "venture"` must have id `"V-" + slug` with `slug` matching
   `VENTURE_SLUG_RE`, and `spec_ref == f"projects/ventures/{slug}/venture.json"`; an id starting with `V-` without
   `kind == "venture"` is rejected. `create` is now allowed for roles `manager`, `human` and `core`, but `core`
   may create only `kind == "venture"` contracts (a core-created task contract is rejected).
5. New action `"venture": ({"human", "core", "interviewer", "operator"}, None, None)`. It is valid only on an
   existing contract of kind `venture` and never changes the contract (status, attempts, tokens, commit stay;
   no budget guard runs). Payload checks (raise `Rejected` with a clear message): `payload["kind"]` in
   `VENTURE_KINDS`; the identity's role is in that kind's set (so an agent identity can never write
   `approval_granted`, `approval_revoked`, `blocker_cleared`, `resumed` or `killed`); every string anywhere in
   the payload is at most 2000 characters. Per kind: `approval_granted` needs `approval_id` matching
   `^[A-Za-z0-9_-]{1,60}$` (not already granted for this venture) and a non-empty str `class` (the other approval
   fields are P6B's and are not checked here); `approval_revoked` needs an `approval_id` previously granted for
   this venture; `blocker_cleared` needs non-empty str `blocker` and `evidence` (non-empty after strip);
   `request` needs non-empty str `request_id`; `revenue` and `cost` need non-empty str `provider_ref` and int
   (not bool) `amount_cents`, `cost` with `amount_cents >= 0`; `spec_amend` needs `spec_hash` (64 lowercase hex)
   and a non-empty str `reason`; `paused`, `resumed`, `killed` need a non-empty str `reason`. Any other action on
   a venture contract except `approve_spec` is rejected (claim, submit, pass, fail, usage, run_report, test_run,
   park, unpark, release, withdraw, reopen); `venture` on a task contract is rejected.
6. `approve_spec` on a venture: when `contract_id` names an existing `kind == "venture"` contract, the payload is
   `{"spec_hash": h}`; outside replay `h` must equal `canonical_hash` of the JSON in
   `self.ventures_dir/<slug>/venture.json` (a missing or unparseable file is rejected). It stays human-only and
   does **not** change `ledger/spec.json` (Forge's own spec freeze is untouched). `approve_spec` on any other
   contract id behaves exactly as today.
7. Venture freeze: outside replay, a `venture` event from a role other than `human` on a venture whose spec has an
   approved hash is rejected (`"venture spec is frozen: ..."`) while the canonical hash of its `venture.json` on
   disk differs from the latest approved hash (missing or unparseable counts as different).
8. Readers (from the event log, after `repair_tail()` and `verify_chain()`): `venture_events(slug) -> list[dict]`
   (events whose `contract_id` is `V-<slug>`, oldest first) and `approved_venture_spec(slug) -> str | None` (the
   `spec_hash` of the latest `approve_spec` event for `V-<slug>`).

Acceptance criteria the tests must check (a temporary ledger root whose `roles.json` is the repo's own
`roles.json` content; `ventures_dir` a temporary folder):

1. The repo `roles.json` maps `forge-interviewer` to `interviewer` and `forge-operator` to `operator`, keeps every
   earlier identity, and `benjamin` is the only identity with role `human`.
2. `forge-core` creates `V-demo` (kind venture, the spec_ref above); rejected: `forge-core` creating a task
   contract, a venture with id `V-Bad_Slug`, a venture whose spec_ref names another path, a task contract with id
   `V-x`, a contract with `kind: "other"`. A contract without `kind` is created and claimed as today.
3. Every kind in `VENTURE_KINDS` is applied by an allowed identity with a valid payload; the contract is unchanged
   after each. `approval_granted`, `approval_revoked`, `blocker_cleared`, `resumed` and `killed` are rejected from
   `forge-core`, `forge-interviewer`, `forge-operator`, `forge-executor` and `forge-manager`, and accepted from
   `benjamin`. Rejected too: an unknown kind, `blocker_cleared` with blank evidence, `revenue` without
   `provider_ref`, `amount_cents` as a bool or float, a 2001-character string, `approval_revoked` for an id never
   granted, `venture` on a task contract, `claim` on a venture contract. After every rejection `snapshot()` is
   unchanged.
4. `approve_spec` by `benjamin` on `V-demo` with the canonical hash of the `venture.json` on disk is applied and
   `approved_venture_spec("demo")` returns it; a wrong hash, a missing file and the same proposal from
   `forge-core` are rejected; `ledger/spec.json` is unchanged by it.
5. After approval, editing `venture.json` makes a `request` from `forge-operator` and a `cycle` from `forge-core`
   rejected as frozen, while `benjamin`'s `paused` is still applied; restoring the file makes them accepted.
6. With venture events in the log, `verify_chain()` is true, `reconcile()` returns False, `rebuild()` reproduces
   the same contracts, and a re-applied proposal id returns `duplicate` and adds no event.
   `canonical_hash({"b": 1, "a": "é"})` equals the sha256 of `'{"a":"é","b":1}'` in UTF-8.

---

## T6Ab: the venture spec validator, its schema and the venture's own repo

- files_in_scope: `core/venture_spec.py` (new), `.gitignore`
- test_files: `tests/core/test_venture_spec.py`
- test_cmd: `python -m unittest tests/core/test_venture_spec.py`
- covers: 3.1, 3.2, 3.3
- depends_on: T6Aa

### Section

Create `core/venture_spec.py` (plain code, standard library only, no AI): the `venture.json` format, its validator
(design 3.2, 3.3) and the function that starts a venture as a ledger contract plus its own git repo (design 3.1).
It builds on T6Aa (`core.ledger.Ledger`, `canonical_hash`, `VENTURE_SLUG_RE`, contract kind `venture`).

1. Constants: `REJECTED_APPROACHES` (the ten D-015 tags: `marketplace_bot`, `fake_persona`, `captcha_bypass`,
   `ai_cold_call`, `third_party_dispute_filing`, `unlicensed_customs_brokerage`, `competitor_price_pooling`,
   `automated_engagement`, `scripted_web_posting`, `resale_against_platform_rules`); `ACTION_CLASSES = ("draft",
   "research", "publish_public", "message_person", "account", "spend", "collect_payment", "legal",
   "irreversible")`; `BLOCKER_KINDS = ("legal", "account", "partner", "date", "data")`; `BLOCKER_OWNERS = ("ben",
   "third_party")`; `KILL_OPS = ("<", "<=", ">", ">=", "==")`; `PRICING_RULE = "minimize_profit_maximize_acceptance"`
   (D-012); `FIELDS` (the top-level keys below); `FORGE_REPO = "benjaminanderson0802/forge"`.
2. `SCHEMA`: a JSON Schema (the forms `core.agents.schema_ok` understands: type, enum, required, properties,
   items, `additionalProperties: false` at the top level) requiring every top-level field below. `example_spec(slug
   = "demo", order = 1) -> dict` returns a fresh spec that passes both `schema_ok(spec, SCHEMA)` and `validate`.
3. `validate(spec) -> list[str]`: empty when valid; never raises for any JSON value. Each message starts with its
   rule id and a colon. Rules: `shape` (not an object, unknown or missing top-level key); `slug` (must match
   `VENTURE_SLUG_RE`); `name` (non-empty str); `source_doc` (str under `docs/source/`, ending `.md`, no `..`, no
   backslash, not absolute); `order` (int >= 1, not bool); `metric_target` (`metric` must be an object with
   non-empty str `name` and `unit`, a number `target` > 0 that is not a bool, int `window_days` >= 1; a missing
   target is this rule); `stage_class` (each of the non-empty `stages` list is an object with unique non-empty
   `name`, non-empty `actor`, and `class` in `ACTION_CLASSES`); `stage_canary` (each stage has a non-empty str
   `canary`); `cadence` (object with int `every_hours` >= 1 and optional `stages` mapping existing stage names to
   int >= 1); `official_api` (each channel object has non-empty `name` and `account` and `official_api` that `is
   True`; the string `"true"` fails); `d015` (any stage or channel whose optional `tags` list holds a tag in
   `REJECTED_APPROACHES`, named in the message); `rules` (non-empty list of objects with unique `id` matching
   `^[A-Za-z0-9_.-]{1,40}$` and non-empty `text`); `budget` (exactly `{"startup_cents": 0, "spend_rule":
   "venture_profit_only"}`: int 0, not `False`, not `0.0`, no other key); `pricing` (object with `rule ==
   PRICING_RULE` and `band` an object with ints `min_cents` >= 0 and `max_cents` >= `min_cents` (not bools) and a
   non-empty str `unit`); `kill_criteria` (non-empty list of objects with non-empty str `metric`, `op` in
   `KILL_OPS`, a number `value` (not bool), int `window_days` >= 1; missing or empty is this rule); `blockers`
   (list of objects with unique `id` (the rules id pattern), `kind` in `BLOCKER_KINDS`, `owner` in
   `BLOCKER_OWNERS`, non-empty str `clears_on`); `human_calls` (list of non-empty str); `repos` (list of
   `owner/name` strings matching `^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$`, never `FORGE_REPO` in any letter case);
   `self_refs` (non-empty list of non-empty str).
4. `spec_hash(spec) -> str` is `core.ledger.canonical_hash(spec)`.
5. `start_venture(forge_root, ledger, slug, source_doc, order) -> Path` (design 3.1): validates `slug` and `order`
   as above and that `source_doc` is a `.md` file under `forge_root/docs/source/` that exists (else `ValueError`,
   nothing created). Creates `forge_root/projects/ventures/<slug>/` as its own git repo (`git init -b main`) with
   `runbooks/`, `requests/`, `outbox/`, `reports/` (each holding an empty `.gitkeep`) and `interview.json`, exactly
   `{"slug": slug, "source_doc": source_doc, "order": order, "status": "interviewing", "answers": {}, "pending":
   {}, "rounds": [], "invalid_rounds": 0}` (sorted keys, indent 2, LF), then commits them as author
   `Forge <forge@localhost>` with message `venture <slug>: start`. It then applies, as `forge-core`, a `create` for
   `V-<slug>` with `kind: "venture"`, title `Venture <slug>`, spec_ref `projects/ventures/<slug>/venture.json`,
   acceptance `metric target and rules in venture.json; ventures have no acceptance tests`, files_in_scope
   `["projects/ventures/<slug>/"]`, `max_attempts` 1, `token_budget` 1. It is idempotent: an existing repo is
   never overwritten or re-committed, an existing contract is not re-created, and a repo without its contract
   gets the contract. `venture.json` is not written here (the Interviewer's validated draft is, in T6Ad).
6. `.gitignore`: add the line `projects/ventures/` so venture repos never enter the Forge repo.

Acceptance criteria the tests must check:

1. `example_spec()` passes `validate` (empty list) and `schema_ok(.., SCHEMA)`; `spec_hash` equals
   `canonical_hash` and is stable under key order.
2. For each of the ten D-015 tags, a stage tagged with it and a channel tagged with it each give a `d015:` message
   naming the tag. A channel with `official_api` false, missing or `"true"` gives `official_api:`. A stage without
   `class`, with class `"post"`, gives `stage_class:`; a stage without `canary` gives `stage_canary:`.
3. Budgets `{"startup_cents": 100, ...}`, `{"startup_cents": False, ...}`, `{"startup_cents": 0.0, ...}`, spend
   rule `"ben_pays"` and an extra key each give `budget:`. A metric without `target`, with target 0 or `True`
   gives `metric_target:`. Missing and empty `kill_criteria`, and op `"!="`, give `kill_criteria:`. Pricing
   without `rule`, with another rule, without `band`, or with `min_cents` > `max_cents` gives `pricing:`. A blocker
   with kind `"vibes"` or owner `"forge"` gives `blockers:`; empty `self_refs` gives `self_refs:`; repo
   `BenjaminAnderson0802/Forge` gives `repos:`; an unknown top-level key gives `shape:`. `validate(None)`,
   `validate([])` and `validate("x")` return a `shape:` message without raising.
4. `start_venture` in a temporary git repo with `docs/source/venture-demo.md` creates the venture repo with its own
   `.git`, the four folders, `interview.json` as specified and one commit; the ledger has `V-demo` with kind
   `venture` created by `forge-core`. A second call changes nothing (same commit, one create event). A bad slug,
   order 0, and a source doc outside `docs/source/` or missing raise `ValueError` and create nothing.
5. The repo's `.gitignore` contains the line `projects/ventures/`.

---

## T6Ac: the Interviewer role, `S_INTERVIEW`, its team member and smoke

- files_in_scope: `core/interviewer.py` (new), `core/roles.py`, `agents/interviewer.md` (new), `core/bootstrap.py`
- test_files: `tests/core/test_venture_interviewer_role.py`
- test_cmd: `python -m unittest tests/core/test_venture_interviewer_role.py`
- covers: 2.1
- depends_on: none

### Section

Create the Interviewer as a role (design 2, row Interviewer): Claude, read-only (it writes no files itself; plain
code writes `interview.json` and the draft `venture.json` from its JSON answer in T6Ad), talking to Ben only
through the conductor's channel. No interview is scheduled here (T6Ad does that).

1. `core/interviewer.py` (plain code): `QUESTIONS_MAX = 8`, `TEXT_MAX = 2000`, `QID_RE =
   re.compile(r"^[A-Za-z0-9_.-]{1,40}$")`, and the agent schema `S_INTERVIEW = {"type": "object", "properties":
   {"status": {"type": "string", "enum": ["questions", "draft"]}, "questions": {"type": "array", "items":
   QUESTION}, "draft": {"type": "object"}, "prefilled": {"type": "array", "items": {"type": "string"}}},
   "required": ["status", "questions"]}` with `QUESTION = {"type": "object", "properties": {"id": str, "field":
   str, "question": str, "default": str}, "required": ["id", "field", "question", "default"]}`. A question
   without `default` therefore makes the whole answer schema-invalid (D-023: every question carries a proposed
   default).
2. `parse_interview(data) -> dict` returns `{"ok", "status", "questions", "dropped", "draft", "problem"}`. If
   `schema_ok(data, S_INTERVIEW)` is false: `ok` False, `problem` `"schema"`, nothing kept. For `status
   "questions"`: a question is kept only if `id` matches `QID_RE` and `field`, `question` and `default` are
   non-empty after strip; duplicates by `id` after the first are dropped; at most `QUESTIONS_MAX` are kept (the
   rest dropped); strings are cut to `TEXT_MAX`; `dropped` counts every dropped question; if none is kept, `ok` is
   False with `problem` `"no usable questions"`. For `status "draft"`: `ok` is True only if `draft` is a dict
   (else `problem` `"draft missing"`); questions are ignored; `draft` is returned as given.
3. Role text: in `core/roles.py` add `EXTRA_ROLES` containing `"interviewer"` and `EXTRA_DEFAULTS["interviewer"]`
   (starts `"You are the INTERVIEWER (read-only)."` and ends `"Answer with JSON matching the schema given in the
   prompt."`). If `EXTRA_ROLES`/`EXTRA_DEFAULTS` already exist (Phase 2A's auditor), add to them. `role_text(repo,
   "interviewer")` reads `agents/interviewer.md` exactly like the other roles (same CRLF, size and fallback rules)
   and falls back to `EXTRA_DEFAULTS["interviewer"]`. `ROLE_NAMES` and `DEFAULTS` stay unchanged; unknown roles
   still raise `ValueError`.
4. `agents/interviewer.md` (LF, under 3000 characters, first line `You are the INTERVIEWER (read-only).`): it
   changes no files; it grills Ben to produce a venture spec (`venture.json`) starting from the venture's
   `docs/source/venture-*.md`; it pre-fills every field it can from the source doc and the answers so far and asks
   only what is missing; every question names the spec field it fills and carries a proposed default; it never
   proposes a D-015 approach, a channel without an official API, or any budget other than
   `venture_profit_only` with `startup_cents` 0; when every field is known it answers `status` `draft` with the
   full spec; it answers with JSON matching the schema.
5. Team member and smoke in `core/bootstrap.py`: give `Team` a plain class attribute `interviewer = None` with no
   type annotation (not a dataclass field). `real_team(limits)` sets `team.interviewer = ClaudeAgent(t,
   permission_mode="plan", allowed_tools=["Read", "Glob", "Grep"])`. Add `"interviewer": (S_INTERVIEW, False,
   '{"status": "questions", "questions": [{"id": "q1", "field": "name", "question": "What is the venture called?",
   "default": "Smoke venture"}]}')` to `SMOKE_ROLES`; in `smoke()` skip (no call, no problem) any role whose member
   `getattr(team, role, None)` is None, so a six-member team smokes exactly as today.

Acceptance criteria the tests must check:

1. `schema_ok` with `S_INTERVIEW`: true for a questions answer whose question has all four fields; false for an
   answer whose one question lacks `default` (and `parse_interview` of it gives `ok` False, problem `"schema"`, no
   questions); false without `status`; false with status `"done"`.
2. `parse_interview` on a schema-valid answer: a question with default `"   "` is dropped and counted while a
   valid one is kept; a bad id (`"a b"`) and a duplicate id are dropped; 10 valid questions keep 8 and count 2; a
   3000-character question is cut to 2000; an answer whose only question has a blank default gives `ok` False,
   problem `"no usable questions"`; a draft answer with a dict draft gives `ok` True and the draft; a draft answer
   without `draft` gives problem `"draft missing"`.
3. `role_text(repo, "interviewer")` returns the file text when `agents/interviewer.md` exists and
   `EXTRA_DEFAULTS["interviewer"]` when missing; `ROLE_NAMES` is unchanged; `role_text(repo, "manager")` raises.
   The repo's `agents/interviewer.md` exists, has no `\r`, is under 3000 characters, starts with the default's
   first line and mentions `default`, `read-only` and `json`.
4. `real_team({}).interviewer` is a `ClaudeAgent` with `permission_mode == "plan"` and only Read, Glob and Grep;
   `Team(...)` from six members has `interviewer is None` and `"interviewer" not in vars(team)`.
5. `SMOKE_ROLES["interviewer"][:2] == (S_INTERVIEW, False)`; `smoke()` with a fake team plus a fake interviewer
   that answers the example returns `[]` and calls it once; an interviewer that writes a file gives a problem
   naming `interviewer`; one answering `{}` gives a problem naming `interviewer`; a six-member team makes six calls.

---

## T6Ad: the interview flow, Ben's spec approval, the freeze and spec amendments

- files_in_scope: `core/venture_interview.py` (new), `core/bootstrap.py`, `core/channel.py`
- test_files: `tests/core/test_venture_interview_flow.py`
- test_cmd: `python -m unittest tests/core/test_venture_interview_flow.py`
- covers: 3.4
- depends_on: T6Aa, T6Ab, T6Ac

### Section

Connect the Interviewer to Ben's queue and the ledger (design 3.4). Builds on T6Aa (`Ledger(state,
ventures_dir=...)`, venture `approve_spec`, `spec_amend`, `approved_venture_spec`), T6Ab (`validate`,
`spec_hash`, `start_venture`, `interview.json` seed) and T6Ac (`S_INTERVIEW`, `parse_interview`,
`role_text(repo, "interviewer")`, `Team.interviewer`).

1. `core/venture_interview.py` (plain code). `ventures_dir(forge_root)` is `forge_root/"projects"/"ventures"`.
   `ANSWER_KINDS = ("venture_question", "venture_spec")`, `DEFAULT_AFTER_H = 24`, `INVALID_LIMIT = 3`,
   `RETRY_AFTER_H = 24`. `interview.json` is read and written atomically (sorted keys, indent 2, LF).
2. `build_prompt(forge_root, slug) -> str`: `role_text(forge_root, "interviewer")`, then `VENTURE: <slug>`, the
   source doc path and its text (cut to 40000 characters), `ANSWERS SO FAR:` the JSON of `answers`, `LAST DRAFT
   PROBLEMS:` the last validator messages (or `none`), the list of spec fields to fill, and, when amending,
   `AMENDMENT REQUESTED: <reason>` plus the approved `venture.json`.
3. `tick(forge_root, ledger, *, ask, call, is_open, close, now) -> str`: works on at most one venture per call
   (slugs in sorted order) whose `interview.json` status is `interviewing` or `amending`, whose `retry_after` (if
   any) has passed, and that has no open pending question younger than `DEFAULT_AFTER_H`. Pending questions
   older than that are settled first: their proposed default is recorded in `answers[field]` with source
   `default` and `close(qid, default)` is called. Then one round: `call(prompt, S_INTERVIEW, venture_repo)` (an
   `AgentResult`); a failed or unparseable answer, or one whose questions are all for fields already answered by
   Ben, is an invalid round (`invalid_rounds += 1`; at `INVALID_LIMIT`, `retry_after = now + RETRY_AFTER_H` and the
   counter resets). Questions for fields Ben already answered are never asked again. Each kept question is asked
   with `ask("venture_question", subject, body, default=<its default>, venture=slug, field=..., question_id=...)`
   and recorded in `pending` as `{qid: {"field", "default", "asked_at"}}`. A draft is checked with `validate`:
   with any problem it is never shown to Ben (the messages go to `last_errors` for the next round, an invalid
   round); a valid initial draft is written to `venture.json`, a valid amendment to `venture.amend.json`
   (`venture.json` untouched), committed in the venture repo as `Forge <forge@localhost>`, status becomes
   `draft_ready` (or `amend_ready`), and plain code asks `ask("venture_spec", "Approve the <name> venture spec",
   body, default="Not approved.", venture=slug, spec_hash=<spec_hash(draft)>, amend=<bool>)`; for an amendment it
   also applies a `spec_amend` event (`forge-core`, the new hash and the reason). Returns `"worked"` when it called
   the agent, else `"idle"`. Every round is appended to `rounds` (time, outcome, dropped count).
4. `request_amend(forge_root, slug, reason)`: allowed only when the venture has an approved spec; sets status
   `amending` with the reason (the approved spec stays in force until Ben approves the amendment).
5. `on_answer(forge_root, ledger, q, body, now) -> bool` (True closes the question). `venture_question`: the first
   line becomes `answers[field]` (a first line of `default` or `ok` takes the proposed default), the qid leaves
   `pending`. `venture_spec`: a first word `y`, `yes` or `approve(d)` approves only if the file Ben was asked about
   (`venture.json`, or `venture.amend.json` copied over `venture.json` and committed) still validates and its
   `spec_hash` equals `q["spec_hash"]`; then plain code applies `approve_spec` as `benjamin` with that hash and
   status becomes `approved` (an amendment file is removed after approval). A mismatch or invalid file approves
   nothing, restores `venture.json`, and sets status back to `interviewing`/`amending` so a fresh draft is asked
   for. Any other reply approves nothing; the reply text is stored as `answers["_ben_notes"]` for the next round.
6. `core/channel.py`: add `"venture_spec"` to `EMAIL_ONLY_KINDS`; `DEFAULTS["venture_spec"] = "The venture spec
   is not approved."`; `DEFAULTS["venture_question"] = "Forge uses the proposed default."`.
7. `core/bootstrap.py`: add `"forge-interviewer": "interviewer"` and `"forge-operator": "operator"` to `ROLES`. A
   method `_venture_ledger()` returns `Ledger(self.state, ventures_dir=self.repo / "projects" / "ventures")`.
   `_venture_step() -> str | None`: None when that folder does not exist or `getattr(self.team, "interviewer",
   None)` is None; otherwise runs `tick` with `ask=self._ask`, `call` running `self._call("interviewer", ...)`,
   `is_open` reading `questions.json`, `close` marking the question answered with the default, `now=self.clock()`;
   returns `"worked"` when it worked; `Capped` returns `self._held()`, `NotReady` `"not_ready"`, `Tampered`
   `"killed"`, `RuntimeError`/`OSError`/`Rejected`/`ValueError` are logged and return `"error"`. `step()` calls it
   only where it would otherwise return `"idle"`/`"not_ready"` at the very end (build work keeps priority). In
   `_answer`, kinds in `ANSWER_KINDS` call `on_answer` and stay open when it returns False.

Acceptance criteria the tests must check (the `tests.core.test_bootstrap.Harness` conductor with an empty task
queue, `docs/source/venture-demo.md` in its repo, `start_venture`, and a fake interviewer set as
`team.interviewer`; no network, no real agent):

1. A questions round asks each kept question once through the queue (kind `venture_question`, its own default in
   `questions.json`); `interview.json` records them as pending; while they are open and younger than 24 hours
   `step()` makes no further interviewer call. Ben's email reply with the reply code records the answer; a field
   already answered is not asked again; a question unanswered for 24 hours (fake clock) is settled with its
   default and closed.
2. A draft that fails `validate` is never written to `venture.json` and no `venture_spec` question exists; its
   messages appear in the next prompt. Three invalid rounds in a row stop rounds for 24 hours.
3. A valid draft is written and committed to `venture.json` and one `venture_spec` question with default `Not
   approved.` and the draft's hash exists; the prompt contained the role text and the source doc text.
4. Ben's email `yes` applies `approve_spec` by `benjamin` with that hash (`approved_venture_spec` returns it); the
   same answer through the drop folder approves nothing; `no, raise the price` approves nothing and the note is in
   the next prompt; a `yes` after `venture.json` was edited approves nothing.
5. Amendment: after approval, `request_amend` and a valid new draft leave `venture.json` and the approved hash
   unchanged, write `venture.amend.json`, record a `spec_amend` event and ask a new `venture_spec`; until Ben's
   email `yes` the approved hash is the old one; after it, `venture.json` is the amendment and the approved hash is
   the new one.
6. A conductor whose repo has no `projects/ventures/` makes no interviewer call and `step()` returns what it
   returned before.

---

## T6Ae: blockers, the `waiting` state and the runner that skips waiting ventures

- files_in_scope: `core/venture_runner.py` (new), `core/bootstrap.py`, `core/channel.py`
- test_files: `tests/core/test_venture_waiting.py`
- test_cmd: `python -m unittest tests/core/test_venture_waiting.py`
- covers: 3.5
- depends_on: T6Aa, T6Ab, T6Ad

### Section

A venture is `waiting` while any blocker in its approved spec is uncleared; a blocker is cleared only by a ledger
event from `benjamin` naming the evidence; the runner skips a waiting venture and works the next in order (design
3.5). Builds on T6Aa (`blocker_cleared` human-only, `venture_events`, `approved_venture_spec`, `canonical_hash`),
T6Ab (`validate`, `spec_hash`, `example_spec`, `start_venture`) and T6Ad (`_venture_ledger`, `_venture_step`, the
`_answer` hook).

1. `core/venture_runner.py` (plain code). `status(forge_root, ledger, slug) -> dict` with keys `slug`, `order`,
   `state`, `blockers_open` (ids), `cleared` (id to evidence) and `reason`. `state` is, in this precedence:
   `interviewing` (no `venture.json`), `invalid` (it fails `validate`), `awaiting_approval` (no approved hash),
   `drifted` (its `spec_hash` differs from `approved_venture_spec`: an unapproved edit never runs; the reason says
   a `spec_amend` needs Ben), `killed` (any `killed` event), `paused` (the latest of `paused`/`resumed` is
   `paused`), `waiting` (any blocker in the approved spec not cleared), else `ready`. A blocker counts as cleared
   only by a `venture` event of kind `blocker_cleared` with identity `benjamin`, role `human`, a `blocker` that is
   an id in the spec and non-empty evidence.
2. `ordered(forge_root, ledger) -> list[dict]`: statuses of every folder under `projects/ventures/` that holds a
   `.git` and has a valid slug name, sorted by (`order` from `venture.json` or `interview.json`, slug).
3. `run_next(forge_root, ledger, work) -> dict`: returns `{"ran": slug | None, "result": ..., "skipped": [{"slug",
   "state", "reason"}]}`; calls `work(slug)` for the first `ready` venture in order and lists every venture before
   it as skipped; `work` None or no ready venture dispatches nothing.
4. `blocker_tick(forge_root, ledger, ask, is_open, state_dir, now) -> int`: for each approved, non-drifted venture
   with open blockers, asks once per open blocker `ask("blocker_cleared", "<name>: is '<blocker id>' cleared?",
   body naming `clears_on` and how to answer ("cleared " followed by the evidence), default="The venture stays
   waiting.", venture=slug, blocker=id)`, recording `{qid, asked_at}` in `state_dir/ventures/<slug>/blockers.json`;
   asks again only after 7 days when the earlier question is closed and the blocker is still open. Returns the
   number asked.
5. `on_blocker_answer(forge_root, ledger, q, body) -> bool`: first word `cleared`, `yes`, `y` or `done` followed
   by evidence text (the rest of the reply, stripped, non-empty, cut to 2000) applies, as `benjamin`, a `venture`
   event `{"kind": "blocker_cleared", "blocker": id, "evidence": text}` and returns True; a clearing word without
   evidence clears nothing and returns False (the question stays open); anything else clears nothing and returns
   True.
6. `core/channel.py`: add `"blocker_cleared"` to `EMAIL_ONLY_KINDS`; `DEFAULTS["blocker_cleared"] = "The venture
   stays waiting."`.
7. `core/bootstrap.py`: `Conductor` gets a class attribute `venture_cycle = None` (a callable `slug -> str`
   installed by P6C). `_venture_step()` (from T6Ad) runs, when the ventures folder exists: the interview tick
   first (as before, only when the interviewer exists), then `blocker_tick`, then `run_next` with
   `self.venture_cycle`, returning `"worked"` when a venture was dispatched and logging each skipped venture with
   its state and reason. `_answer` routes kind `blocker_cleared` to `on_blocker_answer`.

Acceptance criteria the tests must check (temporary Forge root with `docs/source/` docs; `start_venture`; specs
from `example_spec` written to `venture.json`; approvals applied as `benjamin` with `spec_hash`):

1. Venture `alpha` (order 1, one blocker owned by `third_party`) and `beta` (order 2, no blockers), both approved:
   `status` gives `waiting` with the blocker open for `alpha` and `ready` for `beta`; `run_next` with a fake work
   calls `work("beta")` only and lists `alpha` as skipped with state `waiting`.
2. A `blocker_cleared` from `forge-core` or `forge-operator` is rejected by the ledger and `alpha` stays waiting;
   one from `benjamin` with evidence clears it and `run_next` then calls `work("alpha")`.
3. An approved `beta` whose `venture.json` is then edited is `drifted` and is skipped; restoring the file makes it
   `ready`. An unapproved venture is `awaiting_approval`; a `paused` event makes it `paused`, a later `resumed`
   from `benjamin` makes it `ready` again; a `killed` one is skipped.
4. `blocker_tick` asks one `blocker_cleared` question per open blocker and none on a second tick; through the
   Harness conductor, Ben's email `cleared broker agreement signed 2026-10-01` clears it with that evidence;
   `cleared` alone clears nothing and leaves the question open; the same answer through the drop folder clears
   nothing.
5. Through the Harness conductor with an empty task queue and `c.venture_cycle` set to a fake, `step()` returns
   `"worked"` and the fake was called with `beta` while `alpha` is waiting; with `venture_cycle` None nothing is
   dispatched.

## Reviewer notes

- T6Ad: After a rejected spec, return draft_ready/amend_ready to interviewing/amending so the acceptance criterion requiring Ben's notes in the next prompt can pass.
- T6Ad: Give request_amend access to the conductor's actual ledger, preferably through an explicit ledger parameter. forge_root alone does not identify the lane-specific state containing the approved hash.
- T6Ad: Validate and hash-check an amendment before replacing venture.json. Preserve the approved version for rollback if committing or applying approve_spec fails.
- T6Ab: Exercise malformed nested JSON values as well as malformed top-level values to verify validate never raises.
- T6Ae: Handle unreadable or malformed venture.json as invalid and skip it without preventing later ready ventures from running.
