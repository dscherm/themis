# Blind TDD Adoption Guide

> Step-by-step guide for opting a ralph-universal project into the blind-TDD gate.
> For the full design rationale, see [`blind-tdd-gate-rfc.md`](./reference/blind-tdd-gate-rfc.md).
>
> **Requirement levels.** The keywords **MUST**, **MUST NOT**, **REQUIRED**,
> **SHALL**, **SHALL NOT**, **SHOULD**, **SHOULD NOT**, **RECOMMENDED**,
> **MAY**, and **OPTIONAL** in this document are to be interpreted as
> described in [RFC 2119](https://datatracker.ietf.org/doc/html/rfc2119).
> Lowercase "must" / "should" in prose are non-normative.

## What this gets you

When enabled, every task with `acceptance_criteria` runs through a two-phase gate:

1. **Red phase** — a blind agent writes failing tests from the spec alone (no access to `src/`, `examples/`, `.git/`, or `data/generated/`). Enforced by a PreToolUse hook that rejects blocked reads at the tool-call level.
2. **Green phase** — a separate blind runner agent executes those tests against the implementation, verifies every acceptance criterion is covered by a passing test, and checks that no test file was tampered with between red and green.

The gate blocks commits that skip either phase. Criteria that can't be tested objectively (visual polish, aesthetic decisions) escalate to a human via a structured drop box instead of producing flaky assertions.

## Prerequisites

- Project is already using ralph-universal's `smart_gate.py` and `observe.py`.
- `RALPH_HOME` is set to the ralph-universal checkout.
- The project has a `plan.md` (or `fix_plan.md`) with JSON task blocks.
- You have a test directory (e.g. `tests/contracts/`, `tests/integration/`).

## Step 1 — install hooks into the project

Copy the blind-TDD path guard and audit scripts into `.claude/hooks/`:

```bash
mkdir -p .claude/hooks
cp "$RALPH_HOME/templates/hooks/blind_tdd_path_guard.py" .claude/hooks/
cp "$RALPH_HOME/templates/hooks/blind_tdd_audit.py"      .claude/hooks/
```

The `ClaudeCodeSpawner` copies these automatically on every spawn, so this step is optional if you only run blind-TDD through the gate. It's recommended anyway so the hooks are visible in version control and the project's `.claude/settings.local.json` can reference them directly.

## Step 2 — create `public_api.md`

This is the **only** file the blind test-writer agent can read to learn the shape of the code it's testing. Keep it tight — module names, function signatures, parameter types, return types. No implementation details.

Example skeleton:

```markdown
# Public API

## src.engine.core.GameObject

- `add_component(component: Component) -> Component`
- `get_component(cls: Type[T]) -> T | None`
- `destroy(obj: GameObject) -> None` (staticmethod)

## src.engine.transform.Transform

- `position: Vector3`
- `rotation: Quaternion`
- `translate(delta: Vector3) -> None`
```

The implementing agent is expected to keep this file in sync as part of the same commit that adds or changes public surface. Outdated entries are a lint warning, not a hard failure.

## Step 3 — add `acceptance_criteria` and `public_surface` to a task

Open `plan.md` and pick a small, concrete task. Add two fields:

```json
{
  "id": "task-engine-invoke-repeating",
  "title": "GameObject.invoke_repeating matches Unity semantics",
  "passes": false,
  "public_surface": {
    "module": "src.engine.core",
    "adds": [
      "GameObject.invoke_repeating(method_name, time, repeat_rate)",
      "GameObject.cancel_invoke(method_name)"
    ]
  },
  "acceptance_criteria": [
    {
      "id": "AC-1",
      "given": "a GameObject with an enabled MonoBehaviour 'foo' that has a method 'tick'",
      "when": "invoke_repeating('tick', 0.5, 1.0) is called and the lifecycle is advanced 2.6 seconds",
      "then": "tick has been called 3 times (at t≈0.5, 1.5, 2.5)"
    },
    {
      "id": "AC-2",
      "given": "a scheduled repeating invocation",
      "when": "cancel_invoke('tick') is called",
      "then": "no further calls to tick occur"
    }
  ]
}
```

**Schema requirements** (enforced by `tools/blind_tdd/schema_validator.py`):

Keywords below follow [RFC 2119](https://datatracker.ietf.org/doc/html/rfc2119).

- `acceptance_criteria` **MUST** be a non-empty list of dicts with `id`, `given`, `when`, `then`
- `id` **MUST** match `AC-\d+` (case-insensitive)
- `public_surface.module` **MUST** be a dotted module path the agent can import
- `public_surface.adds` **MUST** be a list of signature strings
- Subjective words (`smoothly`, `nicely`, `cleanly`, `properly`) **SHOULD NOT** appear in criteria — they trigger a warning and **SHOULD** be escalated to human-in-the-loop instead

**Preflight requirements** (enforced by `tools/blind_tdd/preflight.py`, default `strict`):

- Every `then` clause **MUST** contain observable assertion language: a numeric value, a comparator/verb word (`equals`, `matches`, `returns`, `raises`, `contains`, `within`, `exactly`, etc.), a PascalCase exception class, or a boolean literal
- Every `public_surface.adds` identifier **MUST** be mentioned by at least one criterion's `when` or `then`
- `public_api.md` **MUST** exist and **MUST** contain the `public_surface.module` as a substring
- Every configured `test_dirs` entry **MUST** exist on disk
- Criteria that are genuinely qualitative **MAY** opt out with `"preclassified": "needs_human"`

Preflight enforcement is controlled by `gate.blind_tdd.preflight`:
- `"strict"` (default): failures block the red phase
- `"warn"`: failures recorded but gate passes
- `"off"`: preflight skipped entirely

### Parameterized criteria with `examples`

When a criterion describes a *pattern* rather than a single case (wrap-around, range handling, normalization), add an `examples` list of dicts. The blind writer will emit a single parameterized test that exercises every example:

```json
{
  "id": "AC-all-rotations",
  "given": "delta_angle receives two degree angles",
  "when": "the shortest signed difference is computed",
  "then": "the result is within 1e-4 of expected",
  "examples": [
    {"current": 0,   "target": 90,  "expected": 90},
    {"current": 0,   "target": 180, "expected": 180},
    {"current": 350, "target": 10,  "expected": 20},
    {"current": 10,  "target": 350, "expected": -20},
    {"current": 5,   "target": 5,   "expected": 0},
    {"current": 1080,"target": 90,  "expected": 90}
  ]
}
```

The `examples[]` schema is **loose** — any dict is acceptable. Use whatever keys make sense for the criterion; the writer infers the `pytest.parametrize` column order from the first example. This collapses 6 criteria into 1 with 6 cases at the same coverage depth.

Without `examples`, multi-case phrasing in `when`/`then` (e.g. `for every`, `wraps around`, `in the range`, `inputs outside`) triggers a schema validator warning suggesting you add them.

Run the validator locally to check:

```bash
python -c "
import json
from tools.blind_tdd.schema_validator import validate_task
task = json.loads(open('plan.md').read().split('\`\`\`json')[1].split('\`\`\`')[0])
print(validate_task(task))
"
```

## Step 4 — enable the gate in `ralph.config.json`

Add a `blind_tdd` block under `gate`:

```json
{
  "gate": {
    "blind_tdd": {
      "enabled": true,
      "enforcement": "warn",
      "spawner": "manual",
      "test_dirs": ["tests/contracts/", "tests/integration/"],
      "public_api_file": "public_api.md",
      "max_challenges_per_task": 3,
      "max_challenges_per_criterion": 1,
      "human_input_timeout": 3600
    }
  }
}
```

### Config field reference

| Field | Default | Purpose |
|---|---|---|
| `enabled` | `false` | Master switch |
| `enforcement` | `"strict"` | `"strict"` blocks commits on any red/green/schema failure; `"warn"` records the failure but returns pass |
| `preflight` | `"strict"` | `"strict"` blocks red phase on structural quality failures (see Step 3 Preflight requirements); `"warn"` logs; `"off"` skips |
| `spawner` | `"claude_code"` | `"claude_code"` fires `claude -p` subprocesses (uses API budget); `"manual"` writes briefs for you to run agents manually |
| `test_dirs` | `["tests/contracts/", "tests/integration/"]` | Where blind tests live |
| `public_api_file` | `"public_api.md"` | The single source the writer can read |
| `max_challenges_per_task` | `3` | Cap on implementer's ability to dispute tests |
| `max_challenges_per_criterion` | `1` | Per-criterion challenge cap |
| `human_input_timeout` | `3600` | Seconds to wait for a human check-in before auto-failing |
| `ralph_home` | `$RALPH_HOME` | Where templates/hooks live (autodetected if unset) |
| `claude_binary` | `"claude"` | Path or name of the claude CLI |
| `spawn_timeout_seconds` | `1800` | Per-agent spawn timeout |

**Recommended for first adoption:** `"enforcement": "warn"` + `"spawner": "manual"`. You'll see briefs land in `.ralph/blind_tdd/pending/` without any commit being blocked or any API budget spent.

## Step 5 — mark the current task

Tell the gate which task it's working on. Three sources, checked in order:

1. **Env var** (explicit override, useful for CI and ad-hoc runs):
   ```bash
   export RALPH_BLIND_TDD_TASK=task-engine-invoke-repeating
   ```
2. **State file** (recommended for the ralph loop to write):
   ```bash
   mkdir -p .ralph
   echo '{"id": "task-engine-invoke-repeating"}' > .ralph/current_task.json
   ```
3. **plan.md fallback** — the first task with `"passes": false` is auto-selected.

If none of these resolve, the blind gate passes with `phase="skipped"` and a clear log message, so doc-only commits are never blocked.

## Step 6 — run the gate

```bash
python "$RALPH_HOME/tools/smart_gate.py"
```

With `spawner: "manual"`:

1. Smart gate calls `run_blind_tdd_gate(config)` as step 2c
2. Red state isn't found → orchestrator calls `ManualSpawner.spawn(role="test_writer", ...)`
3. Spawner writes `.ralph/blind_tdd/pending/test_writer-<task_id>.md` containing the full prompt + task spec as a JSON context block
4. Orchestrator returns `manual_mode=True`, gate logs "manual spawn requested", commit passes under `warn` enforcement
5. You open that brief in a fresh Claude Code session (or any agent), copy the prompt, and run it with the blind-writer settings at `$RALPH_HOME/templates/blind_tdd/settings.blind-writer.json`
6. The agent writes test files under `tests/contracts/` and drops `.ralph/blind_tdd/triage/<task_id>.json`
7. You re-run `smart_gate.py` — this time it verifies coverage, saves red state, and tells you to implement
8. You (or the implementing agent) write the code
9. Next `smart_gate.py` run finds the red state → kicks off the green phase → you run Agent #2 manually the same way → final pass/fail lands

## Step 7 — switch to `claude_code` spawner when ready

Once the manual flow is validated for your project, flip:

```json
"spawner": "claude_code"
```

Now the orchestrator fires fresh `claude -p` subprocesses per agent automatically. Each spawn uses your claude subscription (not raw API credits), with role-specific hooks and settings installed and restored around the call.

## What the output looks like

### On a successful red phase

```
[gate] Running blind-TDD gate...
[gate] blind-tdd phase: red (PASS)
[gate] red phase passed for task 'task-engine-invoke-repeating'. 1 test file(s) hashed. Implementation phase may begin.
```

Red state written to `.ralph/blind_tdd/red_state/task-engine-invoke-repeating.json`. Test file SHAs frozen. Triage report preserved.

### On a successful green phase

```
[gate] Running blind-TDD gate...
[gate] blind-tdd phase: green (PASS)
[gate] green phase passed for task 'task-engine-invoke-repeating': 2 test(s) passing, coverage verified.
```

Red state is cleared. Next task starts fresh.

### On a failure

```
[gate] blind-tdd phase: green (FAIL)
[gate] green phase did not pass: coverage gap in green phase: missing=[], not_passing=['AC-2']
```

Gate fails under strict enforcement. The RFC's challenge protocol (Phase B, not yet shipped) will let the implementing agent dispute a specific test here.

## Troubleshooting

**"no current task resolvable"** — set `RALPH_BLIND_TDD_TASK` or `.ralph/current_task.json`, or make sure `plan.md` has a task with `passes: false`.

**"task failed blind-tdd schema validation"** — run the validator in step 3 and fix the errors. Common issues: missing `public_surface.module`, criterion id that isn't `AC-\d+`, empty `acceptance_criteria`.

**"manual spawn requested — complete the brief and re-run"** — the brief is at `.ralph/blind_tdd/pending/<role>-<task_id>.md`. Run the agent, verify the output files land where the brief says, then re-run `smart_gate.py`.

**Blindness violation in the audit log** — the writer agent attempted to read a blocked path. The brief's inputs weren't set up correctly, or the hook isn't wired. Check `.ralph/blind_audit/<session_id>.jsonl` for the exact blocked tool call.

**Hash mismatch between red and green** — a test file was modified between phases. Typically this means the implementing agent edited a test to make it pass. Revert the test file and re-run.

## What to do after you adopt

- Log friction as ralph-universal observations so the gate improves across projects
- If a criterion category repeatedly escalates to human (e.g. "visual polish"), file a lesson for cross-project learning
- When a challenge is filed (Phase B), review the arbiter ruling — upheld rulings are signal that spec phrasing or test design needs improvement

## Further reading

- [`blind-tdd-gate-rfc.md`](./reference/blind-tdd-gate-rfc.md) — full design, open questions, remaining work
- `templates/blind_tdd/prompts/test_writer.md` — Agent #1 briefing
- `templates/blind_tdd/prompts/test_runner.md` — Agent #2 briefing
- `templates/blind_tdd/prompts/arbiter.md` — Agent #3 briefing (challenge protocol)
- `tools/blind_tdd/schema_validator.py` — the task-spec schema enforced at validate time
