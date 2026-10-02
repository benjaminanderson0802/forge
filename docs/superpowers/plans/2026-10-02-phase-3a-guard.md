# Phase 3A: Guard, spend control and activity gate — implementation plan

Source of truth: `docs/specs/phase-3-design.md` sections 1-3, 6.1, 6.2 and the P3A line of section 8.
Plain code only: no Docker, no AI, no network, no real clock (every time comes in as an argument).
`charter/limits.json` is not edited; every limit is read with a fallback to `guard.DEFAULTS`.

Order and dependencies (R55):

| id | title | depends_on |
|----|-------|------------|
| T3Aa | Ledger actions `guard` and `spend`, `venture_balance` | - |
| T3Ab | `charter/hands.json`, `guard.classify`, `guard.detect_human_step`, `guard.record_guard` | T3Aa |
| T3Ac | `guard.check_spend`, `guard.deny_spend`, `guard.record_spend` | T3Aa, T3Ab |
| T3Ad | `core/secrets.py`: refs, resolve, store, redact | - |
| T3Ae | `guard.input_allowed` and `core/hands.py` `Sender` | T3Aa, T3Ab, T3Ac |

T3Ac and T3Ae both edit `core/guard.py`, so T3Ae waits for T3Ac. Every test file is new and lives under
`tests/core/`; every task runs `python -m unittest <its test file>`. Temporary ledgers are built the way
`tests/core/test_ledger_recovery.py` builds them: a temp folder with a `roles.json` that maps `forge-core` to
`core` (plus the other roles), and every change goes through `Ledger.apply`.

---

## T3Aa: Ledger actions `guard` and `spend`, and `venture_balance`

Files: `core/ledger.py`. Test: `tests/core/test_p3a_ledger_guard_spend.py`. Covers 2.4, 2.8, 6.1, 6.2.

Build in `core/ledger.py` (protected; D-037 standing approval):

1. `ACTIONS["guard"] = ({"core"}, None, None)` and `ACTIONS["spend"] = ({"core"}, None, None)`: role core only,
   any status, no transition. Do not add `income` to `ACTIONS` (Phase 6 does that).
2. `NO_CONTRACT_ACTIONS = frozenset({"guard", "spend", "income"})`. For these actions `contract_id` is still a
   required non-empty string (existing check) but it need not name a contract (callers use `hands:<job>`). If it
   does name one, that contract is not changed. They get their own branch in `apply` before the generic branch;
   they never touch contracts, runs, reports or spec state. The existing idempotency (same `proposal_id` returns
   `{"status": "duplicate"}`), kill switch, hash chain, write order and `rebuild()` replay must work unchanged
   for them.
3. Guard payload (6.1), the safe boundary that keeps secrets and page text out:
   - required keys `kind`, `job`, `venture`, `service`, `step_class`, `reason`; optional keys `snapshot_sha`,
     `payee`, `day`; any other key is `Rejected`.
   - `kind` in `GUARD_KINDS = {"human_step", "refused", "input_blocked", "spend_denied"}`.
   - `step_class` in `GUARD_STEP_CLASSES = {"human_step", "forbidden", "spend", "input"}`.
   - `reason` must be a member of the closed set `GUARD_REASONS` (a module-level frozenset, exactly):
     `unknown_action, bad_step, guard_error, unresolved_target, unresolved_link, host_not_allowed, not_https,
     blocked_host, forbidden_lib, evasion, captcha_token, captcha_iframe, code_reuse, code_entry,
     human_step_active, payment_field, spend_step, captcha, sms_code, email_code, id_check, face_check,
     two_factor_setup, payment_details, terms, need_info, bad_amount, bad_currency, bad_input, subscription,
     new_paid_service, per_action_cap, per_day_cap, venture_balance, host_disabled, idle_unknown, host_active,
     no_host_driver`. Free text is never accepted as a reason.
   - `job`, `venture` and (when present) `payee` must match `ID_RE = ^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$`;
     `service` matches `ID_RE` or is `""`; `snapshot_sha` is `None` or 64 lowercase hex; `day` is
     `YYYY-MM-DD` and a real date. Wrong type or shape is `Rejected`.
4. Spend payload (6.2): exactly the keys `spend_id`, `venture`, `amount_cents`, `payee`, `purpose`, `decision`,
   `caps`, `day`. `spend_id`, `venture`, `payee` match `ID_RE`; `amount_cents` is an `int` (not `bool`) >= 0;
   `purpose` is a string of 1..200 characters; `decision == "allowed"`; `caps` is a dict with exactly
   `per_action_cents` and `per_day_cents`, each an int >= 0; `day` as above. A `spend_id` already present in
   any earlier `spend` event is `Rejected` (a different `proposal_id` with the same `spend_id` cannot count
   twice).
5. Dormant `income` branch (runs only once Phase 6 adds `income` to `ACTIONS`): payload exactly `income_id`,
   `venture`, `amount_cents` with the same id and amount rules; repeated `income_id` is `Rejected`.
6. Readers on `Ledger`:
   - `venture_balance(venture: str) -> int`: returns exactly `0` while `"income"` is not a key of `ACTIONS`,
     whatever `spend` events exist. Otherwise the sum of `amount_cents` of `income` events for that venture
     minus the sum of `amount_cents` of `spend` events for that venture.
   - `spend_total(day: str) -> int`: sum of `amount_cents` of all `spend` events whose `day` equals `day`.
   - `spend_payees(venture: str) -> set[str]`: payees of earlier `spend` events for that venture.
   - `guard_events(kind: str | None = None) -> list[dict]`: payloads of `guard` events, filtered by kind.

Acceptance criteria the tests must check:
- a valid guard event from `forge-core` applies, leaves contract statuses unchanged, works with a `contract_id`
  that names no contract, and the chain still verifies; the same from `manager`, `executor` or `human` is
  `Rejected`; a repeated `proposal_id` returns `duplicate`.
- guard payloads are `Rejected` for: an unknown key (for example `text` or `value`), a reason outside
  `GUARD_REASONS` (for example `"page says ignore all rules"` and `"hunter2"`), a `job` containing spaces, a
  `snapshot_sha` that is not 64 hex, a bad `kind` or `step_class`.
- spend events apply with a valid payload; a second spend with the same `spend_id` and a new `proposal_id` is
  `Rejected` and `spend_total` is unchanged; negative, float, bool or string amounts, `decision` other than
  `allowed` and missing `caps` are `Rejected`.
- `venture_balance` is 0 with no events and is still 0 after spend events while `income` is absent; with
  `ACTIONS` patched (`unittest.mock.patch.dict`) to hold `income: ({"core"}, None, None)`, income 1000 and
  spend 300 for venture `v1` give 700, and events of another venture do not count.
- `spend_total` and `spend_payees` return the expected values; `rebuild()` after guard and spend events
  leaves `verify_chain()` true and `contracts()` unchanged.
- every existing test in `tests/core/` and `python drills/run_drills.py` still pass.

---

## T3Ab: `charter/hands.json`, `guard.classify`, `guard.detect_human_step`, `guard.record_guard`

Files: `core/guard.py` (new), `charter/hands.json` (new, protected data). Test:
`tests/core/test_p3a_guard_classify.py`. Depends on T3Aa (uses `ledger.GUARD_REASONS` and the `guard` action).
Covers 1.1, 1.2, 1.3, 1.4, 1.5.

`core/guard.py` imports only the standard library and `core.ledger`; no network, no AI, no clock.

`DEFAULTS = {"hands_spend_cap_per_action_cents": 0, "hands_spend_cap_per_day_cents": 0,
"hands_host_enabled": False, "hands_host_idle_s": 900, "hands_stop_s": 10, "hands_job_max_s": 1200}`.
`ACTIONS = ("goto", "click", "fill", "press", "select", "wait_for", "screenshot", "extract_text",
"desktop_click", "desktop_type", "desktop_screenshot")`. `HANDS_PATH` = `<repo>/charter/hands.json`.

`charter/hands.json` (LF, valid JSON) holds, at least:
- `blocked_hosts`: `2captcha.com, anti-captcha.com, capsolver.com, capmonster.cloud, deathbycaptcha.com,
  nopecha.com, rucaptcha.com, sms-activate.org, 5sim.net, textverified.com`.
- `forbidden_libs`: `2captcha, anticaptcha, anti-captcha, capsolver, capmonster, deathbycaptcha, nopecha,
  puppeteer-extra-plugin-stealth, playwright-stealth, undetected-chromedriver, selenium-stealth,
  fingerprint-injector, fingerprint-suite, camoufox`.
- `evasion_keys` (arg keys): `user_agent, useragent, proxy, locale, timezone, fingerprint, headers,
  extra_http_headers, geolocation`; `evasion_markers` (phrases): `spoof, user agent, user-agent, fingerprint,
  proxy, navigator.webdriver, change locale, change timezone`.
- `token_markers`: `g-recaptcha-response, h-captcha-response, cf-turnstile-response, captcha token,
  captcha_token`.
- `subscription_markers`: `per month, /month, /mo, monthly, per year, /year, yearly, annual, subscription,
  subscribe, renew, recurring, trial`.
- `payment_markers`: `{"autocomplete_prefixes": ["cc-"], "autocomplete_tokens": ["billing",
  "transaction-amount"], "names": ["card", "cvv", "cvc", "iban", "bic", "swift", "routing", "account_number",
  "sort_code", "billing"]}`.
- `human_steps`: per kind (`captcha, payment_details, id_check, face_check, two_factor_setup, email_code,
  sms_code, terms`) an object with lists `iframe_markers`, `input_names`, `autocomplete`, `phrases`. At least:
  captcha iframe markers `recaptcha, hcaptcha, turnstile, arkoselabs` and phrase `verify you are human`;
  sms_code autocomplete `one-time-code` and phrases `enter the code we sent`, `we sent a code to your phone`;
  email_code phrase `we sent a code to your email`; id_check phrase `upload a photo of your id` and input names
  `id_document, passport`; face_check phrase `take a selfie`; two_factor_setup phrase `authenticator app`;
  terms phrase `i agree to the terms` and input name `accept_terms`; payment_details autocomplete `cc-number`.

`load_hands(path=None) -> dict` reads and checks every key above (lists of strings); raises `ValueError` if the
file is missing or malformed.

`detect_human_step(snapshot, hands=None) -> str | None`. Snapshot is `{"url": str, "title": str, "text": str,
"iframes": [str], "inputs": [{"name", "type", "autocomplete"}]}`. Not a dict, a missing key or a wrong type
returns `"need_info"`; any exception (including `load_hands` failing) returns `"need_info"`. Text is cut to
its first 6000 characters, lowercased and whitespace-collapsed; title is matched the same way. Kinds are
tried in the order captcha, payment_details, id_check, face_check, two_factor_setup, email_code, sms_code,
terms; the first with a hit wins. A hit is: a marker substring of a lowercased iframe src; an `input_names`
marker substring of a lowercased input name; an `autocomplete` marker equal to one whitespace token of an
input's autocomplete; a phrase substring of title or capped text. `payment_details` also fires on any input
that matches `payment_markers` (rule below). No hit returns `None`.

`classify(step, job, hands=None) -> {"class", "reason", "kind"}`. Plain-code page metadata comes in
`job["page"] = {"url": str, "human_step": str | None, "elements": {selector: {"tag": str, "href": str | None,
"navigates": bool, "frame": str | None, "name": str, "type": str, "autocomplete": str, "human_area": bool,
"purchase": dict | None}}}` written by the runner, never by the Registrar or page text. `job` also has
`allowed_hosts: [str]`, optional `fixture_hosts: [str]`, `venture`, `service`. Missing `job["page"]` means
`{"url": "", "human_step": None, "elements": {}}`. `kind` is the human-step kind for `human_step`, `"spend"`
for `spend`, the reason for `forbidden`, `""` for `ok`; `reason` is `"ok"` for ok, otherwise a
`GUARD_REASONS` member. Rules, in this precedence (first match wins):
1. Validation → `forbidden`: `step` not a dict, `why` not a string, `args` not a dict → `bad_step`; action not
   in `ACTIONS` → `unknown_action`; required args missing or of the wrong type → `bad_step` (goto `url` str;
   click/fill/press/select `selector` str; fill also `value_ref` str and a fill carrying `value` is
   `bad_step`; press `key` str; select `option` str; desktop_click `x`, `y` ints >= 0; desktop_type `text` str
   or `value_ref` str); `allowed_hosts` missing, empty or not a list of strings → `host_not_allowed`. Any
   exception or a `load_hands` failure → `forbidden` `guard_error`.
2. A `forbidden_libs` entry as a substring of the lowercased `why` or of the lowercased JSON of `args` →
   `forbidden_lib`.
3. An `args` key (any depth, case-insensitive) in `evasion_keys`, or an `evasion_markers` phrase in `why`/args →
   `evasion`.
4. A `token_markers` entry in `why`/args → `captcha_token`.
5. `value_ref` starting `code:` whose form is not `code:<job venture>:<job service>` → `code_reuse`.
6. click/fill/press/select whose selector is not in `elements` → `unresolved_target`; whose element `frame`
   contains a captcha `iframe_markers` entry → `captcha_iframe`.
7. URL checks, for a goto `url` and for a click on a link (element with tag `a`, a non-null `href`, or
   `navigates` true; the destination is `href` joined to `page.url`; a link with `href` null →
   `unresolved_link`; any `href` inside step args is ignored): host (lowercased, no port, no userinfo) equal to
   or a subdomain of a `blocked_hosts` entry → `blocked_host`; scheme not `https` → `not_https`, except `http`
   when the host is listed in `job["fixture_hosts"]`; host not equal to an allowed host or a subdomain of one
   (`a.example.com` matches `example.com`; `badexample.com` does not) → `host_not_allowed`.
8. `page.human_step` not None: any action other than `screenshot`, `extract_text`, `wait_for` →
   `human_step`, kind = `page.human_step`, reason `human_step_active` (goto, desktop_screenshot and desktop_*
   included).
9. fill/select/press on an element matching `payment_markers` (autocomplete token starting `cc-`, an
   autocomplete token `billing` or `transaction-amount`, or a name containing a `names` entry), or a
   `value_ref` starting `payment:` or `card:` → `human_step`, kind `payment_details`, reason `payment_field`.
10. `value_ref` `code:<job venture>:<job service>` → `human_step`, kind `sms_code`, reason `code_entry`.
11. click on an element with a dict `purchase` → `spend`, kind `spend`, reason `spend_step`.
12. Otherwise `ok`.

`record_guard(ledger, *, kind, job, venture, service, step_class, reason, snapshot_sha=None, payee=None,
day=None) -> dict` applies `{"proposal_id": "guard-" + uuid4 hex, "action": "guard", "contract_id":
f"hands:{job}", "payload": {...}}` as identity `forge-core`, leaving out optional keys that are None.

Acceptance criteria the tests must check: every unknown action, missing field, non-dict step and a patched
`load_hands` that raises gives `forbidden`; goto to an allowed host, a subdomain and an http fixture host is
`ok`, to `badexample.com`, `http://` non-fixture, `javascript:` and `https://allowed.com@evil.com` is
forbidden; a link click with null href is `unresolved_link`, a relative href resolving to an allowed host is
`ok` and to another host is `host_not_allowed`, a href in args cannot widen it, a non-navigating button click
is `ok`, an unknown selector is `unresolved_target`; each `blocked_hosts`, `forbidden_libs`, evasion key,
token marker, captcha-iframe click and foreign `code:` ref is forbidden, and forbidden wins over an active
human step; with `page.human_step` set, goto, click, fill, desktop_click, desktop_type and desktop_screenshot
are `human_step` while screenshot, extract_text and wait_for are `ok`; card (`cc-number`), bank (`iban`,
`routing_number`) and billing (`billing postal-code`, `billing_address`) fields are `human_step`
`payment_details`; a purchase click is `spend`; `detect_human_step` returns each kind from a matching
snapshot, `None` for a plain sign-up page, `None` when the only marker sits after character 6000, and
`need_info` for malformed input; `classify` and `detect_human_step` never import or call network modules
(`socket`, `urllib.request`, `http.client` not imported by `core/guard.py`); `record_guard` writes one guard
event that the ledger accepts; `charter/hands.json` holds every entry listed above.

---

## T3Ac: Spend control: `check_spend`, `deny_spend`, `record_spend`

Files: `core/guard.py`. Test: `tests/core/test_p3a_spend.py`. Depends on T3Aa (spend action, `venture_balance`,
`spend_total`, `spend_payees`, `guard_events`) and T3Ab (`DEFAULTS`, `load_hands`, `record_guard`,
`charter/hands.json` `subscription_markers`). Covers 2.1, 2.2, 2.3, 2.5, 2.7.

`check_spend(ledger, limits, venture, amount_cents, payee, purpose, now, *, currency="USD", hands=None) ->
{"allowed": bool, "reason": str}`. `now` is a `datetime` (injected clock); the local day is
`(now.astimezone() if now.tzinfo else now).date().isoformat()`. Caps come from `limits` with fallback to
`DEFAULTS`; a cap that is missing, a bool, a float, negative or not an int counts as `0` (fail closed).
Rules in order, first denial wins:
1. `venture` or `payee` not matching `ledger.ID_RE`, `purpose` not a non-empty string, or `now` not a datetime
   → `bad_input`.
2. `currency != "USD"` → `bad_currency`. `amount_cents` not an `int`, a `bool`, or negative → `bad_amount`
   (covers `None`, floats such as `5.0`, strings such as `"5"`).
3. Any `subscription_markers` entry in the lowercased purpose → `subscription`, even at amount 0 and even with
   caps and balance high enough (a free trial that renews is a subscription).
4. `amount_cents == 0` → allowed, reason `free`.
5. `payee` not in `ledger.spend_payees(venture)` → `new_paid_service`: every first payment to a new service,
   including a one-time API, activation or setup fee, is a hard stop whatever the caps and balance.
6. `amount_cents > per-action cap` → `per_action_cap`.
7. `ledger.spend_total(day) + amount_cents > per-day cap` → `per_day_cap`.
8. `amount_cents > ledger.venture_balance(venture)` → `venture_balance`.
9. Otherwise allowed, reason `within_caps`. Any exception → denied `bad_input`.

`record_spend(ledger, limits, *, spend_id, venture, amount_cents, payee, purpose, now, currency="USD") -> dict`
re-runs `check_spend`; if it is not allowed it raises `ValueError` and writes nothing; otherwise applies the
`spend` action as `forge-core` with payload `spend_id, venture, amount_cents, payee, purpose[:200],
decision "allowed", caps {per_action_cents, per_day_cents}, day` and returns the apply result.

`deny_spend(ledger, *, job, venture, service, amount_cents, payee, purpose, reason, now, ask=None) -> {"recorded":
bool, "question": dict | None}` writes a guard event `kind "spend_denied"`, `step_class "spend"`, the given
`reason`, `payee` (if it does not match `ID_RE`, `"unknown"`) and `day`. A question is built only when no
earlier `spend_denied` guard event has the same venture, payee and day: `{"kind": "spend", "subject":
"Spend denied: <payee> $<dollars>", "body": ..., "default": core.channel.DEFAULTS["spend"]}` where the body
names the payee, the amount as dollars and cents (`$9.00`, 900 cents), the purpose (first 200 characters), the
rule (the reason) and the default `Nothing is bought.`. When a question is built and `ask` is given,
`ask("spend", subject, body)` is called exactly once. A later denial for the same venture, payee and day is
recorded but returns `question: None` and does not call `ask`.

Acceptance criteria the tests must check: with empty limits (defaults) any amount > 0 is denied and 0 is
allowed; `None`, `5.0`, `"5"`, `-1`, `True` and currency `EUR` are denied with the reasons above; with caps
1000/1500, a balance from patched `income` events and a seeded earlier spend to the payee, 1001 is
`per_action_cap`, a total crossing 1500 the same day is `per_day_cap` while yesterday's spend does not count,
an amount above the balance is `venture_balance`, and a fitting amount is allowed; with caps 100000 and income
100000 a new payee with purpose `one-time API activation fee` of 500 is `new_paid_service`, and `Pro plan, $9
per month` and `free trial, renews` are `subscription` even for known payees and at 0; each such denial passed
to `deny_spend` writes one guard `spend_denied` event and calls a fake `ask` once with kind `spend` and a body
naming payee, `$5.00`, purpose and reason; a second denial the same day for the same venture and payee calls
`ask` zero more times while another payee or the next day asks again; `record_spend` refuses a denied spend and
writes nothing; `charter/limits.json` is unchanged.

---

## T3Ad: `core/secrets.py`: refs, resolve, store, redact

Files: `core/secrets.py` (new). Test: `tests/core/test_p3a_secrets.py`. No dependencies. Covers 4.5.

Standard library plus `keyring` imported lazily inside the default backend only. Nothing in this module prints
or logs a secret value; exception messages may name a ref but never a value.

- `SERVICE = "forge-secrets"`. A backend is any object with `get_password(service, username)` and
  `set_password(service, username, password)`; the default is the `keyring` module (Windows Credential
  Manager). Tests always pass a fake dict-backed backend.
- `parse_ref(ref) -> dict | None`. Segments match `[a-z0-9][a-z0-9_.-]{0,59}`. Accepted forms:
  `identity:<venture>:<field>` → `{"kind": "identity", "venture", "field"}`; `mailalias:<venture>` →
  `{"kind": "mailalias", "venture"}`; `account:<service>:<field>` → `{"kind": "account", "service", "field"}`;
  `code:<venture>:<service>` → `{"kind": "code", "venture", "service"}` (recognised so it can be refused).
  Anything else, a non-string or extra segments returns `None`.
- `ref_allowed(ref, venture, service) -> bool` (design 4.5): true only for `identity:<venture>:*` and
  `mailalias:<venture>` of the job's own venture and `account:<service>:*` of the job's own service. Every
  other ref is false: another venture's identity or alias, another service's account, every `code:`,
  `payment:` or `card:` ref, and malformed refs.
- `resolve(ref, *, backend=None) -> str`: `ValueError` for a ref `parse_ref` rejects or of kind `code`;
  returns the stored value; `MissingSecret` (subclass of `KeyError`) when nothing is stored.
- `store(ref, value, *, backend=None) -> None`: only `identity`, `mailalias` and `account` refs; a field name
  containing `card`, `cc`, `cvv`, `cvc`, `iban` or `bank` is refused; `value` must be a non-empty string of at
  most 4096 characters; a value holding a 12-19 digit run (spaces and dashes ignored) that passes the Luhn check
  is refused as a payment instrument. Every refusal raises `ValueError` and calls no backend method.
- `redact(text, values) -> str`: a non-string `text` returns `""`; every non-empty string in `values` (longest
  first) is replaced by `[secret]`; then runs of 13-19 digits optionally separated by single spaces or dashes
  become `[card]`; then standalone 6-digit numbers (`\b\d{6}\b`) become `[code]`. `None` or empty values are
  ignored.

Acceptance criteria the tests must check: `parse_ref` accepts the four forms and rejects `identity:Tariff:x`,
`identity:tariff`, `foo:bar`, `''` and `None`; `ref_allowed` is true for `identity:tariff:full_name`,
`mailalias:tariff` and `account:github:token` with venture `tariff` and service `github`, and false for
`identity:other:full_name`, `mailalias:other`, `account:gitlab:token`, `code:tariff:github`,
`payment:tariff:card` and a malformed ref; `resolve` returns a stored value through a fake backend, raises
`MissingSecret` for a missing one and `ValueError` for a code ref, and the error text never holds the value;
`store` writes through the fake backend and refuses `4111 1111 1111 1111`, a `card` field, an empty value and a
`code:` ref without touching the backend; `redact` removes a planted secret everywhere in a text, longest
value first, turns `4111-1111-1111-1111` into `[card]` and `123456` into `[code]` but keeps `12345` and dates
like `2026-10-02`; `core/secrets.py` does not import `keyring` at module level.

---

## T3Ae: The activity gate and the `Sender` input path

Files: `core/guard.py`, `core/hands.py` (new). Test: `tests/core/test_p3a_input_gate.py`. Depends on T3Aa
(guard action), T3Ab (`DEFAULTS`, `ACTIONS`, `record_guard`) and T3Ac (same file, `core/guard.py`). Covers 3.1.

In `core/guard.py`: `input_allowed(idle_s, limits, target) -> {"allowed": bool, "reason": str}`. Target `box`
→ allowed, reason `box`. Target `host`: `limits.get("hands_host_enabled", DEFAULTS[...])` must be exactly
`True`, else `host_disabled`; `idle_s` that is `None`, a bool, NaN or not a number → `idle_unknown`; the
threshold is `hands_host_idle_s` if it is a non-bool number >= 0, else 900; `idle_s` below it → `host_active`;
otherwise allowed, reason `host_idle`. Any other target → `bad_step`.

New `core/hands.py` (Phase 3B extends it with `HandsBox`): `class HostInputDisabled(Exception)`,
`class InputBlocked(Exception)` with attribute `reason`, `ledger_recorder(ledger) -> Callable[[dict], None]`
(calls `guard.record_guard(ledger, **payload)`), and

`class Sender: __init__(self, limits: dict, *, idle: Callable[[], float | None], box_driver: Callable[[dict],
Any], record: Callable[[dict], None], host_driver: Callable[[dict], Any] | None = None)`;
`send(event: dict) -> Any`, where `event = {"target": "box" | "host", "action": str, "args": dict, "job": str,
"venture": str, "service": str}`. Every call, before any driver is touched:
1. `action` not in `guard.ACTIONS` or target not `box`/`host` → record a guard payload `{kind
   "input_blocked", job, venture, service, step_class "input", reason "bad_step"}` and raise `InputBlocked`.
2. A `desktop_*` action with target `host` → record (reason `host_disabled`) and raise `HostInputDisabled`.
3. Call `idle()` once for this event (an exception from it counts as `None`) and run
   `guard.input_allowed(idle_s, limits, target)`.
4. Not allowed → record the guard payload with the gate's reason; raise `HostInputDisabled` when the reason is
   `host_disabled`, else `InputBlocked(reason)`. The event is not stored, queued or retried: no driver call
   happens for it, and a later `send` is judged fresh.
5. Allowed `box` → return `box_driver(event)`. Allowed `host` with `host_driver` None → record reason
   `no_host_driver` and raise `HostInputDisabled`; otherwise return `host_driver(event)`.

No module in `core/` other than `core/hands.py` may contain the strings `xdotool`, `pyautogui`, `pynput`,
`SendInput`, `keybd_event` or `mouse_event` (the only input path is `Sender.send`).

Acceptance criteria the tests must check: `input_allowed` gives `box` allowed for any idle including `None`;
host with defaults is `host_disabled`; with `hands_host_enabled: true`, idle `None`, `"900"`, `True` and NaN
are `idle_unknown`, 899 is `host_active`, 900 is allowed, and a custom threshold of 60 allows 60; a counting
fake host driver is called 0 times for host sends with defaults, with idle 5, with idle `None` and for
`desktop_click` to host even when enabled and idle 10000, and once for an enabled host send at idle 900; each
blocked send raises the stated exception and appends exactly one input_blocked payload to the fake recorder;
`idle` is called once per send (three sends, three calls), so the gate runs per event; box sends call the box
driver with the event and return its result; an unknown action is blocked; `ledger_recorder` with a real
temporary ledger writes a guard event the ledger accepts; the source scan of `core/*.py` passes.

## Reviewer notes

- T3Aa: Extend ledger tests to verify spend is core-only and both actions preserve contracts across all statuses. Keep patched income registration active during replay tests.
- T3Ac: A zero-amount spend must not establish permission for a later first paid service fee. Test free-tier registration followed by a nonzero activation fee with sufficient caps and income.
- T3Ac: Clarify that importing core.channel for DEFAULTS is permitted despite T3Ab’s narrower import instruction. Handle malformed denied amounts without dollar-formatting exceptions, and normalize payee identically for deduplication and recording.
- T3Ad: Test backend failures containing a planted secret and suppress secret-bearing exception messages and chaining. Verify the default backend uses Windows Credential Manager without permitting plaintext fallback.
- T3Ae: Test an allowed host event followed by an active or unknown-idle event on the same Sender; only the first may reach the driver. Also test idle-source exceptions and the enabled-host path with no host driver.
