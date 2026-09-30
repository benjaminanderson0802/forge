# Layer 1B part 1: judges and weak-test checks

> Plan task P1B1. Serves `docs/specs/layer-1-design.md` §3 steps 1, 4 and 5 (weak-test check, mutation judge, reviewer verdict in the ledger). Parts P1B2 (readiness) and P1B3 (role files, worktrees, merge) are planned separately. Builds on `core/bootstrap.py` (the conductor), `core/ledger.py` and the bootstrap-conductor spec rules R1–R40.

## What the conductor lacks today

| Design requirement | Today | This plan |
|---|---|---|
| New tests must fail on an **empty implementation** too | only the current-code run (R4) | T1B1c (plain code), T1B1d (wired in, with sabotage tests) |
| Mutation testing on changed lines, kill rate ≥ `mutation_min` | none | T1B1a (sites and mutants), T1B1b (deadline-aware runner), T1B1e (judge) |
| Every surviving mutant reported to Builder **and** Reviewer (D-038) | none | T1B1b returns structured survivors; T1B1e always shows them to the Reviewer, then feeds them to the next Builder |
| Reviewer verdict and reasons in the ledger | reasons kept only in `queue.json` | T1B1e puts `verdict`, `reasons` and mutation evidence in the `pass` / `fail` event payload |

## Fixes for the earlier review findings

- **Multiline expressions:** mutation sites are chosen by the line of the operator *token* itself, found with `tokenize`, never by the `lineno` of the enclosing node (T1B1a).
- **Chained comparisons:** every mutant id includes the token's line and column plus `original->replacement`, and ids are checked unique (T1B1a).
- **Total budget:** every mutant's timeout is clamped to the time left before the deadline. A mutant cut short by the deadline, or not started, is `not_run`, and any `not_run` makes the result `complete=False`, which can never pass (T1B1b).
- **Structured evidence:** the runner returns a `MutationResult` with survivor ids, not a string (T1B1b).
- **Empty-implementation check:** T1B1c/T1B1d, with R4's real-failing-run criteria (no timeout, `Ran N` with N ≥ 1, non-zero exit) applied to the empty-implementation run as well as the current-code run.
- **Reviewer sees survivors even when the mutation gate fails:** the Reviewer runs whenever the task tests and `judge_cmds` pass, with the mutation evidence in its prompt; plain code then fails the attempt if the mutation gate failed, whatever the verdict (T1B1e).
- **Verdicts in the ledger:** T1B1e.

## Tasks (in order; each depends on the ones before it)

1. **T1B1a** `core/mutation.py`: changed-line parsing, mutation sites, mutants. Test: `tests/core/test_mutation_sites.py`.
2. **T1B1b** `core/mutation.py`: `run_mutation` with a deadline-aware budget and `MutationResult`. Test: `tests/core/test_mutation_run.py`.
3. **T1B1c** `core/weaktest.py`: `empty_implementation`, `real_failing_run`. Test: `tests/core/test_weaktest.py`.
4. **T1B1d** `core/bootstrap.py`: Stage A runs the empty-implementation check. Test: `tests/core/test_weak_empty.py`.
5. **T1B1e** `core/bootstrap.py`: mutation judge, Reviewer evidence, ledger verdicts. Test: `tests/core/test_judge_pipeline.py`.

The full task sections (the only text the test writer and builder see) are in the queue entries returned with this plan; they are self-contained and repeat the interfaces above.

## Checks against the rules

- R1: every `test_cmd` is `python -m unittest tests/core/<file>.py`, its only path is the task's own test file.
- R4: T1B1d applies the no-timeout / `Ran N ≥ 1` / non-zero-exit rule to both runs.
- R8/R13: a worktree that isn't clean after the stub or mutation restore raises `RuntimeError` (stage error), never a silent pass.
- R37: the reviewer-capped path is unchanged.
- D-025: judges are plain code; the builder still can't touch tests; the reviewer stays read-only.
- D-027: everything lands on the layer branch; `main` only through Ben's gate.
- Existing `tests/core/test_bootstrap.py` must keep passing unchanged (its toy `VALUE = 42` build kills its only mutant, and its tests fail on an empty `feat.py`).

## Reviewer notes

- T1B1b: Start the deadline before mutant generation, recompute remaining time immediately before launching each subprocess, and check the clock after it returns. A final run that exhausts the total budget must remain incomplete, including when it returns without timed_out or its full per-mutant timeout equals the remaining budget.
- T1B1d: Prevent stale bytecode from affecting the empty-implementation run or subsequent restored-code runs. Use an isolated bytecode cache, and cover a same-size stub replacement.
- T1B1d: Wrap stub creation and modification as well as test execution in the restoration try/finally, so partial setup failures also restore every touched file.
- T1B1e: Preserve survivor evidence in Builder feedback when mutation passes its threshold but the Reviewer rejects the attempt; survivors can exist with a passing mutation score.
- T1B1e: Check restoration to the builder's text immediately after mutation or on the successful path. After a failed attempt, the existing fail helper intentionally resets the worktree to tests_commit.
