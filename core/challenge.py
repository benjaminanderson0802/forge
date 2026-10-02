"""Pure Challenger proof checks and bounds (design §4).

An overturn stands only when plain code proves it: a patch whose tests pass or
show real progress, or a capability check that is OK now. A "stands" answer
counts only with evidence: at least two fresh routes and the real error.
Nothing here launches an agent, runs git or writes files; callers pass test
runs and checks in as callables.
"""
import fnmatch
import hashlib
import json
import re

from core.readiness import NAME_RE

RUN_OUTCOMES = ("verified_overturn", "unverified_overturn", "stands",
                "stands_no_evidence", "unusable", "interrupted")

ROUTE_CAP = 2000
NO_ROUTES = "stands without evidence: fewer than 2 distinct routes not copied from the claimant"
NO_ERROR = "stands without evidence: no error"

_WS = re.compile(r"\s+")
_RAN = re.compile(r"^Ran (\d+) tests? in", re.M)
_RAN_LINE = re.compile(r"^Ran (\d+) tests? in .*$", re.M)
_FAILED = re.compile(r"^FAILED \((.*)\)", re.M)
_FAILED_LINE = re.compile(r"FAILED \((.+)\)")
_COUNT = re.compile(r"(failures|errors|skipped|expected failures|unexpected successes)=(\d+)")
# The order unittest's TextTestRunner prints the counts in.
_KEY_ORDER = ("failures", "errors", "skipped", "expected failures", "unexpected successes")


def norm_route(text):
    """Casefold, collapse whitespace, strip quotes/backticks and trailing punctuation."""
    text = _WS.sub(" ", str(text).casefold()).strip()
    text = text.strip("\"'`").strip()
    return text.rstrip(".;:,!").strip()


def _routes(tried):
    if not isinstance(tried, list):
        return []
    return [route for route in tried if isinstance(route, str)]


def stands_evidence(answer, claimant_routes):
    """None when a stands answer carries evidence, else the reason it does not."""
    copied = {norm_route(route) for route in _routes(list(claimant_routes or []))}
    fresh = {norm_route(route) for route in _routes(answer.get("tried"))}
    fresh.discard("")
    if len(fresh - copied) < 2:
        return NO_ROUTES
    error = answer.get("error")
    if not isinstance(error, str) or not error.strip():
        return NO_ERROR
    return None


def parse_unittest(output):
    """Lenient read of a unittest summary: last Ran count and last FAILED totals."""
    output = output or ""
    ran = _RAN.findall(output)
    counts = {"failures": 0, "errors": 0}
    failed = _FAILED.findall(output)
    if failed:
        for part in failed[-1].split(","):
            key, _, value = part.strip().partition("=")
            if key in counts and value.strip().isdigit():
                counts[key] = int(value)
    return {"ran": int(ran[-1]) if ran else None, **counts}


def _final_summary(output):
    """Totals only when the output ends in a well-formed unittest FAILED summary.

    The first non-blank line after the last Ran line must be exactly what
    TextTestRunner prints for an unsuccessful run, and only blank lines may
    follow it. Anything missing, truncated or malformed gives None.
    """
    matches = list(_RAN_LINE.finditer(output or ""))
    if not matches:
        return None
    rest = [line.strip() for line in output[matches[-1].end():].splitlines()]
    rest = [line for line in rest if line]
    if len(rest) != 1:
        return None
    line = _FAILED_LINE.fullmatch(rest[0])
    if not line:
        return None
    counts, last = {}, -1
    for part in line.group(1).split(", "):
        item = _COUNT.fullmatch(part)
        if not item:
            return None
        position = _KEY_ORDER.index(item.group(1))
        if position <= last:
            return None
        last = position
        counts[item.group(1)] = int(item.group(2))
    failures = counts.get("failures", 0)
    errors = counts.get("errors", 0)
    if failures + errors + counts.get("unexpected successes", 0) < 1:
        return None
    return {"ran": int(matches[-1].group(1)), "failures": failures, "errors": errors}


def run_passed(code, output, timed_out):
    """A run passes only when it finished, exited 0 and ran at least one test."""
    if timed_out or code != 0:
        return False
    ran = parse_unittest(output)["ran"]
    return ran is not None and ran >= 1


def _norm_path(path):
    path = str(path).replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def scope_problem(changed, files_in_scope, test_files):
    """Why a changed-file set is not an acceptable patch, or None."""
    changed = [_norm_path(path) for path in changed or []]
    if not changed:
        return "no changes"
    tests = {_norm_path(path) for path in test_files or []}
    touched = [path for path in changed if path in tests]
    if touched:
        return "touched test files: " + ", ".join(touched)
    patterns = [_norm_path(pattern) for pattern in files_in_scope or []]
    outside = [path for path in changed
               if not any(fnmatch.fnmatch(path, pattern) for pattern in patterns)]
    if outside:
        return "out of scope: " + ", ".join(outside)
    return None


def _failed_totals(code, output, timed_out):
    """Strict totals for a finished, failing run that ran tests, else None."""
    if timed_out or code == 0:
        return None
    totals = _final_summary(output)
    if totals is None or totals["ran"] < 1:
        return None
    return totals


def verify_patch(changed, files_in_scope, test_files, run_patched, run_baseline):
    """Plain-code proof that a patch fixes or measurably improves the tests."""
    problem = scope_problem(changed, files_in_scope, test_files)
    if problem:
        return False, problem
    code, output, timed_out = run_patched()
    if run_passed(code, output, timed_out):
        return True, f"tests pass (Ran {parse_unittest(output)['ran']})"
    patched = _failed_totals(code, output, timed_out)
    if patched is None:
        if timed_out:
            return False, "patched run timed out"
        return False, "patched run unusable: no complete failure summary"
    b_code, b_output, b_timed_out = run_baseline()
    if run_passed(b_code, b_output, b_timed_out):
        baseline = {**parse_unittest(b_output), "failures": 0, "errors": 0}
    else:
        baseline = _failed_totals(b_code, b_output, b_timed_out)
    if baseline is None:
        return False, "baseline run unusable"
    before = baseline["failures"] + baseline["errors"]
    after = patched["failures"] + patched["errors"]
    if after < before and patched["ran"] >= baseline["ran"]:
        return True, f"progress: failures+errors {before} -> {after}"
    return False, f"no progress: failures+errors {before} -> {after}"


def verify_capability(capability, check):
    """Plain-code proof that a named capability is OK now."""
    if not isinstance(capability, str) or not NAME_RE.fullmatch(capability):
        return False, "no capability named"
    entry = check(capability)
    if entry is None:
        return False, f"no check exists for {capability}"
    if isinstance(entry, dict) and entry.get("ok") is True:
        return True, f"{capability} ok: {entry.get('detail', '')}".rstrip(": ")
    detail = entry.get("detail", "") if isinstance(entry, dict) else entry
    return False, f"{capability} still broken: {detail}"


def judge_run(ok, answer, claimant_routes, verify):
    """Classify one Challenger run into a RUN_OUTCOMES value."""
    usable = isinstance(answer, dict)
    fields = answer if usable else {}
    route = fields.get("route")
    route = route.strip()[:ROUTE_CAP] if isinstance(route, str) else ""
    error = fields.get("error")
    result = {"outcome": "unusable", "verdict": fields.get("verdict"), "route": route,
              "tried": _routes(fields.get("tried")),
              "error": error if isinstance(error, str) else "",
              "proof": fields.get("proof"), "detail": ""}
    verdict = result["verdict"]
    if not ok or not usable or verdict not in ("overturned", "stands"):
        result["detail"] = "run failed" if not ok else "unusable answer"
        return result
    if verdict == "overturned":
        proof = result["proof"]
        if proof in ("patch", "capability"):
            verified, detail = verify(proof)
            result["outcome"] = "verified_overturn" if verified is True else "unverified_overturn"
            result["detail"] = detail
        else:
            result["outcome"] = "unverified_overturn"
            result["detail"] = "unknown proof"
        return result
    reason = stands_evidence(fields, claimant_routes)
    result["outcome"] = "stands" if reason is None else "stands_no_evidence"
    result["detail"] = reason or "stands with evidence"
    return result


def decide(outcomes, limit):
    """Final verdict for a claim, or None when another run is allowed."""
    limit = max(1, int(limit))
    outcomes = list(outcomes)
    if "verified_overturn" in outcomes:
        return "overturned"
    if "stands" in outcomes:
        return "stands"
    if len(outcomes) >= limit:
        return "unconfirmed"
    return None


def claim_key(target, subject, claim):
    """Stable identity for a challenged claim."""
    blob = json.dumps([target, subject, claim], sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def ledger_payload(target, claimant, verdict, runs):
    """The challenge ledger event for a decided claim."""
    runs = list(runs)
    wanted = {"overturned": "verified_overturn", "stands": "stands"}.get(verdict)
    proof, route = None, ""
    for run in runs:
        if wanted and run.get("outcome") == wanted:
            proof = run.get("proof") if verdict == "overturned" else None
            route = run.get("route")
            route = route[:ROUTE_CAP] if isinstance(route, str) else ""
            break
    return {"target": target, "claimant": claimant, "verdict": verdict,
            "proof": proof, "route": route,
            "run_ids": [run.get("run_id") for run in runs],
            "outcomes": [run.get("outcome") for run in runs]}
