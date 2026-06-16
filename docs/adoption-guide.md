# Themis Adoption Guide

> Step-by-step guide for putting the Themis blind-TDD gate on your own project.
> For the full design rationale, see [`rfc.md`](./rfc.md).
>
> **Requirement levels.** The keywords **MUST**, **MUST NOT**, **REQUIRED**,
> **SHALL**, **SHALL NOT**, **SHOULD**, **SHOULD NOT**, **RECOMMENDED**,
> **MAY**, and **OPTIONAL** in this document are to be interpreted as
> described in [RFC 2119](https://datatracker.ietf.org/doc/html/rfc2119).
> Lowercase "must" / "should" in prose are non-normative.

## What this gets you

Every task with `acceptance_criteria` runs through a two-phase gate:

1. **Red phase** — a blind agent writes failing tests from the spec alone (no access to `src/`, `examples/`, `.git/`, or generated data). Enforced by a Claude Code `PreToolUse` hook that rejects blocked reads at the tool-call level.
2. **Green phase** — a separate blind runner agent executes those tests against the implementation, verifies every acceptance criterion is covered by a passing test, and checks that no test file was tampered with between red and green (hash-lock).

The gate blocks (or warns on) commits that skip either phase. Criteria that can't be tested objectively (visual polish, aesthetic decisions) escalate to a human via a structured drop box instead of producing flaky assertions. If the implementer believes a test is genuinely wrong, they may file a **challenge**, which a fresh arbiter agent rules on.

## How the gate runs

Themis is a **Python API plus a set of Claude Code hooks**, not a CLI daemon. The single entry point is:

```python
from blind_tdd.gate_integration import run_blind_tdd_gate

result = run_blind_tdd_gate(config)   # config is a plain dict (see Step 4)
if not result.passed:
    raise SystemExit(result.reason)
```

You call that from wherever you gate commits — a `pre-commit` hook, a CI step, or your own task runner. There is no required host harness: `config` is just a dict you construct (typically loaded from a JSON file you own). Themis was extracted from a larger harness, so the original `RALPH_*` / `ralph_home` identifiers are still honored as silent legacy aliases — see [Naming](#naming) at the end.

## Prerequisites

- **Python 3.9+** and the **Claude Code CLI** (`claude`) on `PATH`. Blindness is enforced by Claude Code hooks, so the gate is Claude Code-specific.
- Themis installed: from a clone, `pip install -e .` (add `.[sdk]` for the Agent-SDK spawner).
- Your project has a **test directory** (e.g. `tests/contracts/`, `tests/integration/`) and a **task source** — a `plan.md` with fenced ` ```json ` task blocks, or tasks you pass to the gate directly.

Throughout this guide, `<themis>` is the path to your Themis checkout (where `templates/` lives).

## Step 1 — install the hooks into your project

Copy the three blind-TDD hooks into your project's `.claude/hooks/`:

```bash
mkdir -p .claude/hooks
cp <themis>/templates/hooks/blind_tdd_path_guard.py .claude/hooks/   # blocks reads of src/, examples, generated data
cp <themis>/templates/hooks/blind_tdd_bash_guard.py .claude/hooks/   # blocks Bash escape hatches around the guard
cp <themis>/templates/hooks/blind_tdd_audit.py      .claude/hooks/   # records every tool call for the audit log
```

The `ClaudeCodeSpawner` installs and restores these automatically around each spawn, so this step is optional if you *only* run blind-TDD through the gate. It's recommended anyway so the hooks are visible in version control and your `.claude/settings.local.json` can reference them directly. Per-role settings templates live at `<themis>/templates/blind_tdd/settings.blind-{writer,runner,arbiter}.json`.

## Step 2 — create `public_api.md`

This is the **only** file the blind test-writer agent can read to learn the shape of the code it's testing. Keep it tight — module names, function signatures, parameter types, return types. No implementation details. A starting template is at `<themis>/templates/public_api.example.md`.

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

Pick a small, concrete task in your task source and add two fields:

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

**Schema requirements** (enforced by `blind_tdd/schema_validator.py`):

Keywords below follow [RFC 2119](https://datatracker.ietf.org/doc/html/rfc2119).

- `acceptance_criteria` **MUST** be a non-empty list of dicts with `id`, `given`, `when`, `then`
- `id` **MUST** match `AC-\d+` (case-insensitive)
- `public_surface.module` **MUST** be a dotted module path the agent can import
- `public_surface.adds` **MUST** be a list of signature strings
- Subjective words (`smoothly`, `nicely`, `cleanly`, `properly`) **SHOULD NOT** appear in criteria — they trigger a warning and **SHOULD** be escalated to human-in-the-loop instead

**Preflight requirements** (enforced by `blind_tdd/preflight.py`, default `strict`):

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

**Validate your task spec before running the gate** — the commit-time linter checks the same structural rules preflight will, so you see problems before any agent is spawned:

```bash
python -m blind_tdd.lint_tasks plan.md            # advisory: prints warnings
python -m blind_tdd.lint_tasks plan.md --strict   # exit non-zero on any finding
```

To validate a single task dict programmatically:

```python
from blind_tdd.schema_validator import validate_task
print(validate_task(task))   # task = the dict above
```

## Step 4 — write the config

The gate takes a plain dict shaped like `{"gate": {"blind_tdd": {...}}}`. Store it however you like (a `themis.config.json` you load, inline in your gate script, etc.):

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

Load and pass it:

```python
import json
from blind_tdd.gate_integration import run_blind_tdd_gate

config = json.load(open("themis.config.json"))
result = run_blind_tdd_gate(config)
```

### Config field reference

| Field | Default | Purpose |
|---|---|---|
| `enabled` | `false` | Master switch |
| `enforcement` | `"strict"` | `"strict"` blocks commits on any red/green/schema failure; `"warn"` records the failure but returns pass |
| `preflight` | `"strict"` | `"strict"` blocks red phase on structural quality failures (see Step 3); `"warn"` logs; `"off"` skips |
| `spawner` | `"claude_code"` | `"claude_code"` fires `claude -p` subprocesses; `"manual"` writes briefs for you to run agents yourself |
| `test_dirs` | `["tests/contracts/", "tests/integration/"]` | Where blind tests live |
| `public_api_file` | `"public_api.md"` | The single source the writer can read |
| `max_challenges_per_task` | `3` | Cap on the implementer's ability to dispute tests |
| `max_challenges_per_criterion` | `1` | Per-criterion challenge cap |
| `human_input_timeout` | `3600` | Seconds to wait for a human check-in before auto-failing |
| `themis_home` | _(unset)_ | **Optional.** Overrides where prompt/hook templates are found. Leave unset standalone — templates resolve relative to the installed package. (Legacy alias: `ralph_home`.) |
| `claude_binary` | `"claude"` | Path or name of the claude CLI |
| `spawn_timeout_seconds` | `1800` | Per-agent spawn timeout |

**Recommended for first adoption:** `"enforcement": "warn"` + `"spawner": "manual"`. You'll see briefs land in `.themis/blind_tdd/pending/` without any commit being blocked or any agent being spawned automatically.

## Step 5 — mark the current task

Tell the gate which task it's working on. Three sources, checked in order:

1. **Env var** (explicit override, useful for CI and ad-hoc runs):
   ```bash
   export THEMIS_TASK=task-engine-invoke-repeating   # legacy alias: RALPH_BLIND_TDD_TASK
   ```
2. **State file**:
   ```bash
   mkdir -p .themis
   echo '{"id": "task-engine-invoke-repeating"}' > .themis/current_task.json
   ```
3. **plan.md fallback** — the first task with `"passes": false` is auto-selected.

If none of these resolve, the blind gate passes with `phase="skipped"` and a clear log message, so doc-only commits are never blocked.

## Step 6 — run the gate

Call `run_blind_tdd_gate(config)` from your script (Step 4). With `spawner: "manual"`:

1. The gate finds no red state → the orchestrator calls `ManualSpawner.spawn(role="test_writer", ...)`.
2. The spawner writes `.themis/blind_tdd/pending/test_writer-<task_id>.md` containing the full prompt + task spec as a JSON context block.
3. The gate returns `manual_mode=True` and (under `warn`) passes.
4. You open that brief in a fresh Claude Code session, copy the prompt, and run it with the blind-writer settings at `<themis>/templates/blind_tdd/settings.blind-writer.json`.
5. The agent writes test files under `tests/contracts/` and drops `.themis/blind_tdd/triage/<task_id>.json`.
6. You re-run the gate — this time it verifies coverage, saves red state, and tells you to implement.
7. You (or the implementing agent) write the code.
8. The next gate run finds the red state → kicks off the green phase → you run the runner agent manually the same way → final pass/fail lands.

## Step 7 — switch to the `claude_code` spawner when ready

Once the manual flow is validated for your project, flip:

```json
"spawner": "claude_code"
```

Now the orchestrator fires fresh `claude -p` subprocesses per agent automatically, with role-specific hooks and settings installed and restored around each call. (Spawning fresh subscription CLI agents — rather than in-conversation subagents — also matters for honesty; see the spawn-mechanism section of [`impossible-ac-results.md`](./impossible-ac-results.md).)

## What the output looks like

### On a successful red phase

```
blind-tdd phase: red (PASS)
red phase passed for task 'task-engine-invoke-repeating'. 1 test file(s) hashed. Implementation phase may begin.
```

Red state written to `.themis/blind_tdd/red_state/task-engine-invoke-repeating.json`. Test file SHAs frozen. Triage report preserved.

### On a successful green phase

```
blind-tdd phase: green (PASS)
green phase passed for task 'task-engine-invoke-repeating': 2 test(s) passing, coverage verified.
```

Red state is cleared. Next task starts fresh.

### On a failure

```
blind-tdd phase: green (FAIL)
green phase did not pass: coverage gap in green phase: missing=[], not_passing=['AC-2']
```

The gate fails under strict enforcement. If the implementer believes a specific test is wrong rather than the code, they may file a challenge (`blind_tdd/challenge.py`); a fresh arbiter rules upheld / rejected / ambiguous.

## Troubleshooting

**"no current task resolvable"** — set `THEMIS_TASK` (or legacy `RALPH_BLIND_TDD_TASK`) or `.themis/current_task.json`, or make sure `plan.md` has a task with `passes: false`.

**"task failed blind-tdd schema validation"** — run `python -m blind_tdd.lint_tasks plan.md` and fix the errors. Common issues: missing `public_surface.module`, criterion id that isn't `AC-\d+`, empty `acceptance_criteria`.

**"manual spawn requested — complete the brief and re-run"** — the brief is at `.themis/blind_tdd/pending/<role>-<task_id>.md`. Run the agent, verify the output files land where the brief says, then re-run the gate.

**Blindness violation in the audit log** — the writer agent attempted to read a blocked path. The brief's inputs weren't set up correctly, or the hook isn't wired. Check `.themis/blind_audit/<session_id>.jsonl` for the exact blocked tool call.

**Hash mismatch between red and green** — a test file was modified between phases. Typically this means the implementing agent edited a test to make it pass. Revert the test file and re-run. (This is the load-bearing defense; see [`impossible-ac-results.md`](./impossible-ac-results.md) §5.)

**Templates not found** — if you moved the package or installed it oddly, set `themis_home` in config (or the `THEMIS_HOME` env var) to your Themis checkout so the prompt/hook templates resolve.

## Naming

The preferred identifiers are `THEMIS_TASK`, `THEMIS_HOME`, and the `themis_home` config key / constructor parameter. Themis was extracted from a larger agent-learning harness, so the original names it used are still honored as **silent legacy aliases** — you don't need to change anything if you were already using them:

| Preferred | Legacy alias (still works) |
|---|---|
| `THEMIS_TASK` env var | `RALPH_BLIND_TDD_TASK` |
| `THEMIS_HOME` env var | `RALPH_HOME` |
| `themis_home` config key / ctor param | `ralph_home` |

When both are set, the `THEMIS_*` value wins.

## Further reading

- [`rfc.md`](./rfc.md) — full design, resolved open questions, remaining work
- [`impossible-ac-results.md`](./impossible-ac-results.md) — adversarial evidence the gate holds (0/42 bypass)
- `<themis>/templates/blind_tdd/prompts/test_writer.md` — Agent #1 briefing
- `<themis>/templates/blind_tdd/prompts/test_runner.md` — Agent #2 briefing
- `<themis>/templates/blind_tdd/prompts/arbiter.md` — Agent #3 briefing (challenge protocol)
- `blind_tdd/schema_validator.py` — the task-spec schema enforced at validate time
