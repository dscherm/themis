# Blind-TDD Fidelity Improvements — Plan

**Status:** Drafted 2026-04-10. Decisions locked, not yet implemented.
**Follow-on to:** `blind-tdd-gate-rfc.md` (functionally complete as of `c3c95f0`)

This plan covers three additive improvements to the blind-TDD gate's *input fidelity* — the quality of the task specs that feed the pipeline. None of these are on the original RFC's remaining-work list; they emerged from an in-session conversation about how to improve protocol adherence by tightening the front door rather than the back door.

## Locked decisions (user)

| # | Question | Decision |
|---|---|---|
| 1 | Preflight enforcement default | **`strict`** — task preflight failures block the blind gate by default |
| 2 | Linter behavior in smart_gate | **Advisory** — runs when `plan.md` is in changed files, logs warnings, never blocks commits unless a user explicitly flips a flag |
| 3 | `acceptance_criteria[].examples` schema | **Loose** — any dict; the blind writer maps fields to `pytest.parametrize` args based on criterion context |
| 4 | This plan document | Committed to `docs/blind-tdd-fidelity-plan.md` (this file) |

---

## Addition 1 — Task preflight checklist

**Goal:** Before the orchestrator spends API budget on a blind-writer spawn, catch tasks that are technically schema-valid but structurally weak (no numeric thresholds, no concrete observables, subjective words, criteria that don't exercise the public surface).

### Scope

**In:**
- New `preflight_task(task, config)` → `PreflightResult(ready, errors, warnings)` in `tools/blind_tdd/preflight.py`
- Called from `gate_integration.run_blind_tdd_gate()` between `validate_task` and `run_red_phase`
- Wired to `gate.blind_tdd.preflight = "strict" | "warn" | "off"`, **default `"strict"`** (locked decision #1)

**Out:**
- Not a replacement for `schema_validator` — runs AFTER it
- Not a commit-time linter (that's Addition 2)
- Never runs when `gate.blind_tdd.enabled = false`

### Checks (priority order)

1. **Subjective language in `then` clauses** — `schema_validator` already emits these as warnings; preflight PROMOTES them to errors under `strict` mode. `warn` mode keeps them as warnings. `off` mode skips entirely.
2. **Public-surface coverage** — for every `public_surface.adds[i]` signature, extract the identifier (`Mathf.delta_angle(...)` → `delta_angle`; `foo()` → `foo`) and verify at least one criterion's `when` or `then` clause mentions that identifier. If none do, the task adds something no criterion tests.
3. **Criterion observability** — each criterion's `then` clause must contain either:
   - A numeric literal (regex `\b\d+(\.\d+)?\b`), OR
   - A comparator/verb word (`equals`, `matches`, `is`, `returns`, `raises`, `contains`, `throws`, `greater`, `less`, `true`, `false`, `exactly`, `within`, `not`), OR
   - A PascalCase exception class name (`.*Error` or `.*Exception`)
   - If none match, the criterion is likely vague. **Exception:** criteria that the writer will escalate as `needs_human` are allowed to fail this check if the task declares them upfront (see "Open question" below).
4. **`public_api.md` exists and mentions the module** — if `gate.blind_tdd.public_api_file` is configured, verify the file exists and contains the `public_surface.module` name as a substring. If not, the writer has no context for the surface it's supposed to test.
5. **Test dirs exist** — every path in `gate.blind_tdd.test_dirs` must exist in the project. Missing dirs → writer can't create tests there.

### Files touched

- **New**: `tools/blind_tdd/preflight.py` (~150 LOC)
- **New**: `tools/blind_tdd/test_preflight.py` (~200 LOC, ~20 tests)
- **Edit**: `tools/blind_tdd/gate_integration.py` (+20 LOC — wire the check in + new config field)
- **Edit**: `docs/blind-tdd-adoption-guide.md` (+15 LOC — document the preflight flag and its checks)

### Tests

Each check in isolation (pass/fail cases), strict vs warn vs off enforcement, public-surface coverage detection via identifier extraction regex, false-positive avoidance for legitimately-qualitative criteria with upfront `needs_human` declaration, missing `public_api.md` handling, missing test dir handling.

### Mutation targets

- Flip the numeric regex — at least 1 observability test should fail
- Bypass the public-surface match — at least 1 coverage test should fail
- Skip the test-dir existence check — at least 1 existence test should fail
- Return `strict` → `off` from config reader — at least 1 enforcement test should fail

### Risks

- **False positives on observability check**: criteria that are legitimately English-prose might fail the comparator-word check even when clear. Mitigation: generous keyword list (15+ terms) plus the `needs_human`-upfront escape hatch.
- **Identifier extraction edge cases**: `public_surface.adds` signatures come in forms like `Class.method(args) -> type`, `function_name(a, b)`, `async foo()`. The regex needs to handle all of these. Tests pinned.

### Effort

~370 LOC total, self-contained (depends only on existing `schema_validator.py`).

### Open question

Should tasks declare `needs_human` criteria upfront in the task spec (new field `criteria_preclassified: {"AC-3": "needs_human"}`) so the observability check can skip them? Or let the writer discover them at triage time?

**Lean:** declare upfront, because the preflight check needs to know before it burns the API budget on a spawn. New field would be optional and additive — tasks without it work as today.

---

## Addition 2 — Task spec linter (commit-time, advisory)

**Goal:** Catch task spec quality issues *before* the task reaches the blind gate, at the same point the user writes new tasks. Runs during `smart_gate.py` when `plan.md` is in the changed files list.

### Scope

**In:**
- New tool `tools/blind_tdd/lint_tasks.py` that scans all JSON blocks in `plan.md` / `fix_plan.md` and reports issues
- CLI: `python -m tools.blind_tdd.lint_tasks plan.md [--strict]`
- Optional smart_gate integration: runs when `plan.md` is in changed files AND `gate.blind_tdd.lint_plan = true` (default `true`)
- **Advisory by default** (locked decision #2): logs warnings, never fails the gate unless `gate.blind_tdd.lint_plan_enforcement = "strict"` is explicitly set
- Reuses `preflight_task()` from Addition 1 to run the same structural checks at both spawn time and lint time

**Out:**
- No auto-fixing
- Ignores tasks without `acceptance_criteria` (those are opt-out)
- Does not run in CI (project-level decision)

### Additional checks beyond preflight

1. **Criterion ID gaps** — a task with `AC-1`, `AC-2`, `AC-4` (skipping `AC-3`) is probably a copy-paste error. Warn.
2. **Duplicate task IDs** — currently not validated by schema_validator, trivial to add at linter level.
3. **`public_surface.adds` conflicts with `modifies`** — the same signature in both → inconsistent, warn.
4. **Stale `passes: true` with `acceptance_criteria`** — a completed task with criteria that were never validated through the blind gate (no `blind_green_phase` observation with the task id). Warn with "this task completed without blind-TDD validation." Gated on `--include-historical` CLI flag OR a `gate.blind_tdd.lint_warn_on_historical = true` config flag, since a fresh clone with no `.themis/observations.jsonl` would otherwise flood with false positives.

### Files touched

- **New**: `tools/blind_tdd/lint_tasks.py` (~200 LOC)
- **New**: `tools/blind_tdd/test_lint_tasks.py` (~150 LOC, ~15 tests)
- **Edit**: `tools/smart_gate.py` (+20 LOC — run lint when plan.md is changed, behind `gate.blind_tdd.lint_plan` flag, default advisory)

### Tests

Each new check (criterion gaps, dup task IDs, add/modify conflict, stale completed), plan.md JSON block parsing (leverage `gate_integration._extract_json_blocks`), integration with `preflight_task` from Addition 1, strict vs advisory enforcement, historical grace period.

### Risks

- **"Stale completed task" false positives** on fresh clones: observations history not present → every completed task warns. Mitigation: `--include-historical` opt-in and a grace period flag in config.
- **plan.md parsing duplication**: reuse `_extract_json_blocks` and `_iter_tasks` from `gate_integration.py`; promote them to a shared module if needed.

### Effort

~370 LOC, depends on Addition 1 landing first (reuses `preflight_task`).

---

## Addition 3 — Parameterized examples usage

**Goal:** Actually use the `acceptance_criteria[].examples` field that `schema_validator` already supports. Zero tasks currently populate it; if they did, the blind writer could emit `pytest.parametrize` tests and get ~3x coverage per criterion for free.

### Scope

**In:**
- Update `templates/blind_tdd/prompts/test_writer.md` to explicitly instruct the writer: "If a criterion has `examples`, emit a parameterized test function that exercises each example as a case."
- Update `schema_validator.py` to **warn** when a criterion has no `examples` field AND its `then` clause contains multi-case phrasing (regex hits on `for every`, `for each`, `given any`, `wraps around`, `normalizes`, `range`, `boundary`, `inputs outside`). Surfaces the opportunity without forcing it.
- Update `docs/blind-tdd-adoption-guide.md` with a worked example showing criterion-with-examples producing a parameterized test (use the `Mathf.delta_angle` pattern).
- **Optional**: helper in `coverage.py` that counts parametrized test cases per criterion so the green report reflects actual coverage width, not just "AC-1 has 1 test."

**Loose examples schema** (locked decision #3): any dict is acceptable. The blind writer infers `pytest.parametrize` args from the keys present in the criterion context. No fixed schema, no `input:`/`expected:` requirement. Example tasks may use `{"current": 0, "target": 90, "expected": 90}` or `{"in": 1, "out": 2}` or anything else that makes sense for the criterion.

**Out:**
- NOT forcing every criterion to have examples
- NOT re-running existing completed tasks — forward-looking only
- NOT changing the test runner (pytest parametrize is already supported by every test_runner config we care about)

### The `Mathf.delta_angle` task would collapse to

```json
{
  "id": "AC-all-rotations",
  "given": "delta_angle receives two degree angles",
  "when": "the shortest signed difference is computed",
  "then": "the result is within 1e-4 of expected and handles wrap-around",
  "examples": [
    {"current": 0,    "target": 90,  "expected": 90},
    {"current": 0,    "target": 180, "expected": 180},
    {"current": 350,  "target": 10,  "expected": 20},
    {"current": 10,   "target": 350, "expected": -20},
    {"current": 5,    "target": 5,   "expected": 0},
    {"current": 1080, "target": 90,  "expected": 90}
  ]
}
```

Writer emits:

```python
@pytest.mark.parametrize("current,target,expected", [
    (0, 90, 90), (0, 180, 180), (350, 10, 20),
    (10, 350, -20), (5, 5, 0), (1080, 90, 90),
])
def test_delta_angle_all_rotations(current, target, expected):
    """Covers: AC-all-rotations"""
    assert Mathf.delta_angle(current, target) == pytest.approx(expected, abs=1e-4)
```

6 criteria → 1 criterion with 6 cases. Same coverage, less ceremony.

### Files touched

- **Edit**: `templates/blind_tdd/prompts/test_writer.md` (+25 LOC — parametrize instructions, worked example, loose-schema note)
- **Edit**: `tools/blind_tdd/schema_validator.py` (+15 LOC — multi-case phrasing warning)
- **Edit**: `tools/blind_tdd/test_schema_validator.py` (+6 LOC — one new test)
- **Edit**: `docs/blind-tdd-adoption-guide.md` (+40 LOC — pattern section)
- **Optional edit**: `tools/blind_tdd/coverage.py` (+30 LOC + 3 tests — parametrize case counting)

### Tests

- Multi-case phrasing warning fires for each trigger phrase
- Warning does NOT fire when `examples` is already present
- Warning does NOT fire for single-case criteria
- Loose schema: warning check is indifferent to the shape of existing `examples` entries
- Optional coverage counter handles parametrized tests correctly

### Risks

- **Writer interpretation** — without a strict schema, different writers might map the same criterion's `examples` to parametrize args differently. Mitigation: first real blind-writer run validates the pattern. If drift appears, tighten the prompt (not the schema).
- **Backward compat** — additive only; tasks without `examples` continue working unchanged.

### Effort

~85 LOC main, ~120 LOC with optional coverage piece. Smallest of the three additions.

---

## Suggested implementation order

1. **Addition 1 first** — foundation for Addition 2, valuable standalone, low risk, self-contained
2. **Addition 3 second** — 15-minute win, high leverage, independent of the others
3. **Addition 2 third** — biggest of the three, depends on Addition 1's `preflight_task`, mostly advisory so low blast radius

## What this does NOT solve

- **Agent judgment drift** — blind writer interpreting a clear criterion in an unexpected way. That's what the challenge protocol (already shipped) is for.
- **Project-level scope errors** — deciding what belongs in a task. No spec format helps; that's a design conversation.
- **Free-text task descriptions** — The harness still reads task descriptions as free text. These additions only tighten the structured fields (`acceptance_criteria`, `public_surface`). The description field remains advisory.
- **Cross-project coordination** — if two projects define `AC-1` with different semantics, these checks don't notice. The lesson_extractor's `spec-unclear-phrasing` pattern covers the observed-drift case.

## Estimated total effort

| Addition | Main LOC | Tests LOC | Docs LOC | Total |
|---|---|---|---|---|
| 1 — Preflight | ~150 | ~200 | ~15 | ~365 |
| 2 — Linter | ~200 | ~150 | ~0 | ~350 |
| 3 — Examples | ~50 | ~10 | ~40 | ~100 |
| **Sum** | **~400** | **~360** | **~55** | **~815** |

All three shippable in one session if greenlit together, or split across two sessions if you want to validate Addition 1 before committing to Addition 2.
