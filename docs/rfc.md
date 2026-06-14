# RFC: Blind TDD/BDD Gate

**Status:** Complete and shipped. The full red/green/challenge pipeline, the
path/bash/audit hooks, the hash-lock, the AC coverage check, both spawners, and
the impossible-AC probe harness are implemented and tested (272 in-package
tests).
**Author:** Dan Schermele + Claude

> **Provenance & vocabulary.** This RFC was written while the gate lived inside a
> larger cross-project agent-learning harness — named `ralph-universal` where the
> text below refers to it directly — and Themis is its standalone extraction. The
> text therefore refers to that original integration by name. Read these as the
> *reference integration*, not as requirements:
>
> - `smart_gate` — the host's per-commit gate that the blind gate plugged into
>   (as "step 2c"). Standalone, the equivalent entry point is your own CI step.
> - `plan.md` / `current_task.json` — the host's task source (acceptance
>   criteria + public surface per task).
> - `.ralph/` — the host state directory; in Themis this is `.themis/`.
>
> The gate's core — blind writer → hash-lock → blind runner → arbiter — is
> independent of all of these. The commit-by-commit build history lives in git.

## Key design decisions

- **Enforcement modes.** `strict` blocks on any red/green/schema failure; `warn`
  records the failure but returns `passed=True`.
- **Gate ordering.** In the reference integration the blind gate runs after
  secrets/syntax checks but before the test suite, so a schema failure never
  wastes test time.
- **Task resolution.** `RALPH_BLIND_TDD_TASK` env → `current_task.json` → first
  `passes:false` task in `plan.md`. If none resolve, the gate returns
  `phase="skipped"` so doc-only commits aren't blocked.
- **Red-state persistence.** Test-file hashes + the triage report are held
  between the red and green phases under
  `.themis/blind_tdd/red_state/<task_id>.json`.
- **Optional import.** The host imports `blind_tdd.gate_integration` lazily, so a
  project without the subtree gates exactly as before.

---

## Problem statement

Today's `smart_gate.py` in ralph-universal has a structural integrity problem that became visible on the unity-py-sim dashboard:

- **Last 500 observations: 464 gate=pass, 2 gate=fail (100% gate pass rate).**
- **Of those 500, only 2 had tests actually run.** Both failed.
- **3 observations modified source files without adding any test** ("SRC w/o test").

The existing gate is **lying about quality**: it rubber-stamps commits that never had tests executed, because `determine_test_strategy()` silently returns `"none"` (and passes) whenever it can't find a test mapping for changed files. Track 1 of this work fixes that specific bug.

Track 3 (this RFC) goes further: it adds a **blind TDD/BDD enforcement layer** that guarantees tests are written **before** implementation code, by an agent that cannot read the implementation. This solves a second, deeper problem the existing "post-task validation agent" workaround only partially addresses: **the agent writing tests is biased by having seen (or being the same entity as) the agent that wrote the code.**

## Goals

1. **Enforce TDD ordering**: tests come first, implementation second. No exceptions.
2. **Enforce blindness**: the test-writing agent cannot see the implementation or be influenced by it.
3. **Enforce spec discipline**: every task must carry enough acceptance criteria and public-API shape that an outsider with no code access can write meaningful tests.
4. **Work across languages**: Python, JavaScript, TypeScript, C#, and anything else ralph-universal supports. No framework lock-in.
5. **Support human-in-the-loop for untestable cases**: when a criterion is genuinely subjective or requires visual verification, route to a human via a structured channel instead of writing a bad test.
6. **Preserve git cleanliness**: `git bisect` must work. One commit per task.
7. **Produce cross-project learning signals**: every blind-TDD event becomes an observation that feeds the existing dashboard and lessons system.
8. **Be opt-in**: projects that haven't adopted blind-TDD see zero behavior change.

## Non-goals

- Replacing `smart_gate.py` entirely. Blind-TDD layers on top.
- Replacing the existing post-task validation agent. It's kept and narrowed.
- Full Gherkin/pytest-bdd/behave framework adoption. We use loose Gherkin (structured text), no framework dependency.
- Sandboxing the blind agent at the filesystem level. We use log-audit with preflight rejection.

---

## Requirements

### R1. Smart gate integrity (prerequisite — Track 1)

| ID | Requirement |
|----|-------------|
| R1.1 | The gate MUST fail when source files changed and zero tests ran, unless explicitly overridden by a human via the human input channel or by `gate.allow_unmapped_sources = true` in config. |
| R1.2 | "Source files" MUST include `.py`, `.js`, `.ts`, `.tsx`, `.jsx`, `.cs`, `.json`, `.yaml`, `.yml`, `.go`, `.rs`, `.java`, `.kt`, `.rb`, plus project-specific extensions via `gate.source_extensions` config. |
| R1.3 | When the test map does not cover a changed runnable file, the gate MUST fall back to running the full test suite (not silently skip). |
| R1.4 | When a non-runnable source file (e.g. `.cs`, `.json`) changes, the gate MUST flag it as advisory by default; `gate.strict_nonrunnable = true` escalates to human check-in. |
| R1.5 | Ignorable paths MUST include `tools/`, `.ralph/`, `docs/`, `.github/`, `data/reference/`, `data/assets/`. |

**Status:** Implemented in Track 1 (commit on `ralph-universal/master`).

### R2. Human input channel (prerequisite — Track 2)

| ID | Requirement |
|----|-------------|
| R2.1 | A file-based drop box MUST exist at `.ralph/human_requests/` for any tool or agent to write structured JSON requests. |
| R2.2 | Request files MUST follow a standard schema (see Appendix A) including: id, task, agent, phase, criterion, category, question, options, free_text_allowed, blocking, timeout_seconds, created_at. |
| R2.3 | Response files MUST follow a standard schema (see Appendix A) including: request_id, chosen_option, free_text, responded_at, responder. |
| R2.4 | Claude (in the main conversation) MUST act as a **verbatim router**: when a new request file appears, Claude displays the `question` and `options` fields verbatim to the user, captures the user's next message verbatim, and writes it to the response file. Claude MUST NOT rephrase, summarize, interpret, or add commentary. |
| R2.5 | Blocking requests MUST time out after `timeout_seconds` (default 24 hours, configurable). On timeout, the requesting tool auto-fails its gate with reason "human input timed out". |
| R2.6 | The channel MUST be crash-safe: requests and responses persist across process restarts. |
| R2.7 | The dashboard MUST surface the count of pending human input requests as a top-level metric. |

**Status:** Partially implemented in Track 1 (`tools/human_input.py`). Dashboard surfacing (R2.7) is deferred to Track 4.

### R3. Blindness enforcement

| ID | Requirement |
|----|-------------|
| R3.1 | The blind test-writer agent (**Agent #1**) MUST NOT read any file in `src/`, `examples/`, existing tests, git history, or git diffs. |
| R3.2 | Agent #1 MAY read: the task spec from `plan.md`, `public_api.md`, external documentation via WebFetch, and its own previously-written tests from the current phase. |
| R3.3 | The blind test-runner agent (**Agent #2**) MUST NOT modify any test files. Tests are read-only. |
| R3.4 | Agent #2 MAY read: the task spec, acceptance criteria, test files written by Agent #1, `public_api.md`, and test runner output. Agent #2 MUST NOT read any file in `src/` or `examples/`. |
| R3.5 | The blind arbiter agent (**Agent #3**) MUST NOT read: implementation, other tests, git history, or comments outside the disputed test. |
| R3.6 | Agent #3 MAY read: the task spec, `public_api.md`, the specific disputed test, and the challenge text. |
| R3.7 | Enforcement MUST use the **three-layer defense** resolved in OQ1: (1) a PreToolUse hook rejects forbidden-path Read/Grep/Glob/Edit/Write calls before execution, (2) the Agent SDK's `tools=[...]` whitelist removes Bash from Agent #1 and restricts Agent #2's Bash to test-runner commands only, (3) a PostToolUse hook logs every tool call to `.ralph/blind_audit/` for forensic verification. |
| R3.8 | The audit log MUST be recorded in the observation for the task. Any preflight rejection counts as a violation attempt and is logged with the agent ID, attempted path, and timestamp. |
| R3.9 | Accidental violations (e.g. agent attempts to read `src/` but is blocked) MUST NOT fail the gate on their own. Successful reads of forbidden paths (if the wrapper fails) MUST fail the gate. |
| R3.10 | Agent #2's test execution MAY import implementation modules at runtime (via pytest/jest/etc.). Tracebacks that reveal source lines are visible by default (not redacted). |

### R4. Task spec format

| ID | Requirement |
|----|-------------|
| R4.1 | Every task in `plan.md` (or equivalent task queue) MUST carry an `acceptance_criteria` field. |
| R4.2 | `acceptance_criteria` MUST be a list of objects, each with: `id` (e.g. `AC-1`), `given`, `when`, `then`, optional `examples`, optional `notes`. |
| R4.3 | Every task MUST carry a `public_surface` field declaring: `module`, `class` (if applicable), `adds` (list of signatures for new functions/methods), `modifies` (list of changed signatures), `external_refs` (list of authoritative docs URLs). |
| R4.4 | A schema validator MUST reject tasks missing required fields **before** the blind gate runs. The validator is a precondition check in `smart_gate.py`. |
| R4.5 | `acceptance_criteria` uses **loose Gherkin** format — structured Given/When/Then text markers, but not parsed by a BDD framework. Tests reference criterion IDs via `Covers: AC-N` in test docstrings. |
| R4.6 | The `public_surface.module` field MUST be a valid import path in the target language (e.g. `src.engine.core` for Python, `./components/Player` for TypeScript). |

### R5. Agent #1 — Test Writer

| ID | Requirement |
|----|-------------|
| R5.1 | Agent #1 receives: task spec, acceptance criteria, public_surface, `public_api.md`, external doc URLs. Agent #1 does NOT receive: any source code. |
| R5.2 | Agent #1 MUST write test files that import from the declared `public_surface.module` paths. |
| R5.3 | Agent #1 MUST tag every generated test with the criterion ID it covers, via a `Covers: AC-N` docstring or equivalent annotation. |
| R5.4 | Agent #1 MUST produce a **triage report** alongside the tests, with one entry per acceptance criterion. Each entry has status: `tested`, `tested_partial`, or `needs_human`. |
| R5.5 | `needs_human` entries MUST include a reason code from: `subjective`, `visual`, `flaky_timing`, `external_dep`, `nondeterministic`, `integration_only`, `spec_unclear`, plus a free-text explanation. |
| R5.6 | If a criterion cannot be effectively tested (per the categories in R5.5), Agent #1 MUST NOT write a placeholder/trivial test. It MUST escalate via the human input channel. |
| R5.7 | Agent #1's output MUST be deterministic given the same spec: running it twice on the same task should produce equivalent tests (same criteria covered, same assertions, possibly different naming). |

### R6. Red phase

| ID | Requirement |
|----|-------------|
| R6.1 | After Agent #1 writes tests, the gate MUST execute them against the current (pre-implementation) codebase. |
| R6.2 | The red phase passes if: (a) all tests execute without syntax errors, (b) all test failures are `ImportError`, `ModuleNotFoundError`, `AttributeError`, or explicit `NotImplementedError`, (c) every acceptance criterion has at least one test or a `needs_human` triage entry. |
| R6.3 | The red phase fails if: (a) any test has a syntax error in the test file itself, (b) any test passes unexpectedly (indicates the test is not targeting new code), (c) any criterion has neither a test nor a triage entry. |
| R6.4 | The red phase result MUST be recorded as an observation with type `blind_red_phase` including: task id, test files created, triage report, test output, and pass/fail. |

### R7. Implementation phase

| ID | Requirement |
|----|-------------|
| R7.1 | The implementing agent MAY read everything in the repo including tests written by Agent #1. |
| R7.2 | The implementing agent MUST NOT modify test files written in the red phase. If it believes a test is wrong, it MUST file a challenge (see R9). |
| R7.3 | The implementing agent's work is bounded by the same existing rules (CLAUDE.md, smart_gate checks, etc.). |

### R8. Green phase (Agent #2)

| ID | Requirement |
|----|-------------|
| R8.1 | After the implementing agent commits, a fresh **Agent #2** is spawned with no memory of Agent #1's reasoning. |
| R8.2 | Agent #2 receives: the test files (read-only), the task spec, acceptance criteria, `public_api.md`. |
| R8.3 | Agent #2 MUST run the test suite (via the project's configured runner) and report pass/fail per test. |
| R8.4 | Agent #2 MUST verify that every criterion ID declared in the task has at least one passing test OR a `needs_human` triage entry from the red phase. Missing coverage = fail. |
| R8.5 | Agent #2 MUST verify that no tests were added, removed, or modified between red and green phases (by hashing the test files from the red phase observation and comparing). Modification = fail. |
| R8.6 | The green phase result MUST be recorded as an observation with type `blind_green_phase` including: pass/fail per test, coverage verification, test file hashes, and overall gate verdict. |

### R9. Challenge protocol (Agent #3 — Arbiter)

| ID | Requirement |
|----|-------------|
| R9.1 | If a test fails in the green phase, the implementing agent MAY file a **challenge** instead of fixing the code. |
| R9.2 | A challenge MUST specify: the disputed test file and name, the criterion it claims to cover, why the test does not correctly encode the criterion, and what a correct test would assert. |
| R9.3 | Each challenge spawns a fresh **Arbiter agent (Agent #3)** with no memory of prior phases. |
| R9.4 | The arbiter receives: task spec, acceptance criteria, `public_api.md`, the disputed test file, the challenge text. The arbiter does NOT receive: implementation, other tests, comments outside the disputed test, git history. |
| R9.5 | The arbiter MUST rule: `upheld` (test is wrong), `rejected` (test is correct), or `ambiguous` (cannot decide). |
| R9.6 | Upheld: the criterion is routed to a fresh Agent #1 for a test rewrite, with the ruling as additional context. Rejected: the implementing agent must make the test pass. Ambiguous: auto-escalate to human via the input channel. |
| R9.7 | Challenge caps MUST be configurable per-project: `gate.blind_tdd.max_challenges_per_task` (default 2), `gate.blind_tdd.max_challenges_per_criterion` (default 1). |
| R9.8 | Every challenge + ruling MUST be recorded as an observation with type `challenge_filed` and `arbiter_ruling`. |
| R9.9 | Observations logged per R9.8 MUST feed cross-project learning: patterns like "agent X has 40% false-challenge rate" or "criterion phrasing 'handles gracefully' produces 60% disputed tests" become derivable. |

### R10. Commit ordering and git hygiene

| ID | Requirement |
|----|-------------|
| R10.1 | Each task MUST result in **exactly one git commit** containing both the tests and the implementation. |
| R10.2 | The red phase, green phase, challenge events, and arbiter rulings MUST be recorded as observations in `.ralph/observations.jsonl`, not as git commits. |
| R10.3 | `git bisect` MUST work cleanly: every commit on the branch passes all tests. No broken-main states from the red/green trajectory. |
| R10.4 | The observation log provides the full TDD trajectory audit trail for any task. Querying observations by task id MUST yield the red → implementation → green sequence with timestamps. |

### R11. Post-task validation agent (existing mandate, narrowed)

| ID | Requirement |
|----|-------------|
| R11.1 | The existing CLAUDE.md mandate for a post-task validation agent is **retained but narrowed**. |
| R11.2 | The post-task validation agent's new scope: **mutation testing and implementation auditing**. It reads the implementation (unlike blind-TDD agents) and writes tests in `tests/mutation/` and `tests/audit/`. |
| R11.3 | Blind-TDD owns `tests/contracts/` and `tests/integration/`. Post-task validation owns `tests/mutation/` and `tests/audit/`. |
| R11.4 | The post-task validation agent's findings are advisory by default (warnings logged as observations) but configurable per-project to be blocking. |
| R11.5 | CLAUDE.md MUST be updated to reflect the new scope and relationship. |

### R12. Config shape

| ID | Requirement |
|----|-------------|
| R12.1 | All blind-TDD config lives under `gate.blind_tdd.*` in the project's ralph config. |
| R12.2 | `gate.blind_tdd.enabled` (bool, default false) controls opt-in. |
| R12.3 | `gate.blind_tdd.enforcement` (enum: `honor` / `audit` / `preflight`, default `preflight`) controls blindness enforcement strictness. |
| R12.4 | `gate.blind_tdd.max_challenges_per_task` (int, default 2). |
| R12.5 | `gate.blind_tdd.max_challenges_per_criterion` (int, default 1). |
| R12.6 | `gate.blind_tdd.human_input_timeout` (int seconds, default 86400). |
| R12.7 | `gate.blind_tdd.public_api_file` (path, default `public_api.md`). |
| R12.8 | `gate.blind_tdd.test_dirs` (dict mapping test category to directory, e.g. `{"contracts": "tests/contracts/", "integration": "tests/integration/"}`). |
| R12.9 | `gate.blind_tdd.triage_categories` (list of allowed `needs_human` reason codes, defaults to the R5.5 list; projects can extend). |

### R13. Cross-project observation schema

| ID | Requirement |
|----|-------------|
| R13.1 | New observation types: `blind_red_phase`, `blind_green_phase`, `challenge_filed`, `arbiter_ruling`, `triage_escalation`, `blindness_violation_attempt`, `human_input_requested`, `human_input_resolved`. |
| R13.2 | All new observation types MUST include: task id, phase, agent id, timestamp, and the existing ralph-universal observation fields (project, iteration, gate result, tags). |
| R13.3 | Observations MUST be queryable from the dashboard to produce metrics: blind-TDD pass rate, challenge frequency, arbiter upheld/rejected ratios, triage category histogram, human-intervention rate. |

### R14. Migration and rollout

| ID | Requirement |
|----|-------------|
| R14.1 | When `gate.blind_tdd.enabled = false`, behavior MUST be identical to current ralph-universal (no new phases, no new spawns). |
| R14.2 | When enabled on a project for the first time, existing tasks without `acceptance_criteria` MUST be grandfathered: the schema validator warns but does not block. New tasks MUST have the fields. |
| R14.3 | unity-py-sim is the first intended adopter. Migration guide must document the opt-in sequence. |
| R14.4 | Opt-in MUST be reversible: disabling the flag returns the project to standard smart_gate behavior without data loss. |

---

## Architecture

### Layered gate model

```
┌─────────────────────────────────────────────────────────┐
│                    Task Queue (plan.md)                 │
│           [ requires acceptance_criteria,               │
│             public_surface fields ]                     │
└──────────────────────────┬──────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────┐
│             Schema Validator (smart_gate)               │
│  Rejects tasks missing acceptance_criteria or          │
│  public_surface before any agent is spawned.            │
└──────────────────────────┬──────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────┐
│            Agent #1 — Blind Test Writer                 │
│  Input:   spec, criteria, public_api.md, external docs │
│  Output:  test files + triage report                    │
│  Audit:   tool calls logged, src/ reads preflight-      │
│           rejected                                      │
└──────────────────────────┬──────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────┐
│                     Red Phase                           │
│  Runs tests against pre-implementation codebase.        │
│  Expects ImportError/AttributeError failures.           │
│  Verifies every AC has a test or a needs_human entry.   │
│  Observation: blind_red_phase                           │
└──────────────────────────┬──────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────┐
│         Implementing Agent (normal claude)              │
│  Reads everything including tests. MUST NOT modify      │
│  tests. MAY file challenges. Commits code.              │
└──────────────────────────┬──────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────┐
│             Agent #2 — Blind Test Runner                │
│  Input:   test files (read-only), spec                  │
│  Output:  test results, coverage verification           │
│  Audit:   src/ reads preflight-rejected                 │
└──────────────────────────┬──────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────┐
│                    Green Phase                          │
│  All tests pass? Every AC covered? Test files           │
│  unchanged since red phase? → blind gate pass.          │
│  Observation: blind_green_phase                         │
└──────────────────────────┬──────────────────────────────┘
                           │                              ┌──────────────────────┐
                           │                              │                      │
                           │  ◄──── challenge ──────────► │ Agent #3 — Arbiter   │
                           │                              │ (fresh, blind)       │
                           │                              └──────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────┐
│                    smart_gate                           │
│  (Track 1 fixes applied: honest test running, source    │
│  coverage check, human check-in on gaps)                │
└──────────────────────────┬──────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────┐
│                      Commit                             │
│  Single commit, tests + implementation together.        │
│  TDD trajectory in observations, not git log.           │
└─────────────────────────────────────────────────────────┘
```

### Agent isolation

Each of the four agent types (Agent #1, Agent #2, Agent #3, and the implementing agent) runs in a **fresh context with no memory of the others**. They communicate only via files: test files, triage reports, challenge documents, arbiter rulings, and observations.

### Preflight tool wrapper

The blindness enforcement mechanism is a wrapper around the claude-code tool layer that intercepts Read/Grep/Glob/Bash calls from any agent marked "blind" in its spawn metadata. The wrapper:

1. Loads the current task's whitelist (the paths the agent is allowed to read)
2. Checks each incoming tool call's path argument against the whitelist
3. Rejects calls outside the whitelist with a tool error `"Access denied: path outside blind whitelist"`
4. Logs every call (accepted and rejected) to the observation record

**Open question (see below):** whether the current claude-code agent spawn mechanism can enforce path whitelists. If not, we fall back to pure log-audit (post-hoc detection). This is the biggest unknown in the design.

---

## Decision record — answers to the 15 questions from 2026-04-10

| # | Question | Decision |
|---|----------|----------|
| 1 | No-test-ran behavior | Fail + human check-in via drop box; provide reason; special case for aesthetic review |
| 2 | Source detection scope | Expand to `.cs`, `.json`, `.yaml`, `.yml`, etc., configurable |
| 3 | Unmapped file fallback | Full-suite first, then human check-in if coverage is still absent |
| 4 | Blindness strictness | Strict (c): no reading any code. Escape hatch: `public_api.md` |
| 5 | Task spec fields | `acceptance_criteria` (loose Gherkin) + `public_surface` required |
| 6 | Test-writer model | Option A (real imports, red/green trajectory) + discernment layer |
| 7 | Agent architecture | Two separate agents (Agent #1 writer, Agent #2 runner) |
| 8 | Enforcement | Log-audit with whitelist + preflight rejection; tracebacks visible |
| 9 | Challenge resolution | Formal challenge → fresh arbiter agent → upheld/rejected/ambiguous; cap configurable per-project |
| 10 | Human input channel | File-based drop box at `.ralph/human_requests/`; Claude is verbatim router |
| 11 | Tasks needing human interaction | Handled by discernment + triage categories in R5 |
| 12 | Existing post-task validation agent | Keep, narrow scope to mutation/audit testing |
| 13 | BDD framework | Loose Gherkin, no framework dependency |
| 14 | Commit ordering | Single commit per task; trajectory in observations |
| 15 | Blind gate placement | Layered in front of smart_gate; opt-in |

---

## Open questions and risks

### OQ1. Can claude-code enforce path whitelists on spawned agents? — **RESOLVED (2026-04-10)**

**Status: RESOLVED — preflight rejection IS supported via a three-layer defense.**

Claude Code does not support per-path restrictions directly in a subagent's `allowed_tools`, but the same effect is achievable by combining three mechanisms. This is the definitive enforcement model for R3.

**Layer 1 — PreToolUse hook (primary preflight rejection)**

A hook script matches `Read|Grep|Glob|Edit|Write` tool calls, inspects the `tool_input.file_path` (or equivalent) argument, and exits with code 2 to block the call before execution. Path patterns use gitignore syntax.

Example hook configuration for a blind agent:
```json
{
  "hooks": {
    "PreToolUse": [{
      "matcher": "Read|Grep|Glob|Edit|Write",
      "hooks": [{
        "type": "command",
        "command": ".claude/hooks/blind-tdd-path-guard.sh"
      }]
    }]
  }
}
```

The hook script reads JSON from stdin, extracts the path argument, checks it against the whitelist/blacklist, and exits 0 (allow) or 2 (block with error).

**Layer 2 — Subagent tool scoping (close the Bash bypass)**

The PreToolUse hook does NOT guard `Bash` subprocesses, so `Bash(cat src/file.py)` would bypass Layer 1. For Agent #1 (test writer), we remove `Bash` from the toolset entirely via the Agent SDK:

```python
AgentDefinition(
    tools=["Read", "Glob", "Grep", "Write", "Edit"],  # NO Bash
    prompt="..."
)
```

For Agent #2 (test runner), we cannot remove Bash because running `pytest`/`jest`/etc. requires it. Instead, we **whitelist specific Bash command patterns** via settings.json:

```json
{
  "permissions": {
    "allow": [
      "Bash(pytest*)",
      "Bash(python -m pytest*)",
      "Bash(npm test*)",
      "Bash(jest*)",
      "Bash(dotnet test*)"
    ],
    "deny": [
      "Bash(cat*)", "Bash(less*)", "Bash(more*)",
      "Bash(head*)", "Bash(tail*)",
      "Bash(grep*)", "Bash(rg*)",
      "Bash(find*)", "Bash(ls*)"
    ]
  }
}
```

Agent #2's Read/Grep/Glob are still guarded by the Layer 1 hook; the Bash whitelist just prevents shell-based reading.

**Layer 3 — PostToolUse audit log (forensic proof)**

Every tool call by any blind agent is logged to `.ralph/blind_audit/<task>-<agent>.jsonl` via a PostToolUse hook. This produces:
- Evidence for the observation record (R3.8)
- Post-hoc verification that Layer 1 actually blocked attempts
- Cross-project data for detecting patterns (e.g. "Agent #1 for task type X attempts src/ reads 60% of the time")

If Layer 1 ever fails (hook script bug, settings misconfiguration), the audit log will show src/ reads, and the gate fails the task retroactively.

**Constraints this imposes on the implementation**

- Each blind agent needs its own `.claude/settings.local.json` override (or the hook script needs to know which agent spawned it via an env var)
- The hook script must be committed to the project and treated as security-critical code
- The path patterns must be kept in sync between the hook, the settings.json Bash whitelist, and the RFC
- Projects adopting blind-TDD must include the hook script and settings in their bootstrap

**Not supported, with workarounds**

- Path scoping in subagent `allowed_tools` → use Layer 1 hook
- Agent frontmatter `restrict_paths` field → use Layer 1 hook
- Managed cross-project path restrictions → use project-level hooks that ralph-universal ships as a template

**Sources**

- Claude Code `PreToolUse` hook documentation: exit code 2 blocks tool execution
- Claude Code settings schema: `permissions.allow`/`permissions.deny` with gitignore-syntax patterns
- Claude Agent SDK: `AgentDefinition(tools=[...])` for explicit tool scoping
- "Block edits to protected files" example in the docs shows the same hook pattern

**Action items derived from this resolution**

1. Ship a reference `blind-tdd-path-guard.sh` hook script in `ralph-universal/templates/hooks/`
2. Ship a reference `settings.blind-tdd.json` config in `ralph-universal/templates/`
3. Update R3.7 wording from "log-audit with preflight rejection" to "three-layer defense (PreToolUse hook + subagent tool scoping + PostToolUse audit)"
4. Add a bootstrap step for blind-TDD adoption that copies the hook and settings into the project

### OQ2. How is `public_api.md` maintained?

**Risk level: MEDIUM.** The design requires a curated public API file the blind agents can read. Who updates it, and when?

**Proposed answer:** the implementing agent updates `public_api.md` as part of the task's commit. Any task that adds or modifies a public API must include a `public_api.md` diff. The schema validator checks that every `public_surface.adds` entry is present in `public_api.md` after the commit.

**Failure mode:** `public_api.md` drifts from reality. Mitigation: a periodic audit job compares `public_api.md` against actual exports (importable members of declared modules) and flags discrepancies.

### OQ3. What language-specific runners does Agent #2 need?

**Risk level: MEDIUM.** Agent #2 has to run tests via the project's configured runner (pytest for Python, jest/vitest for JS, dotnet test for C#, cargo test for Rust, etc.). The existing ralph config has a `stack.test_runner` map. Agent #2 should reuse that, not invent its own.

**Action:** Agent #2's spawn template reads `stack.test_runner` and constructs the appropriate command. Failures in unknown runners escalate to human.

### OQ4. What about projects with no tests at all?

**Risk level: LOW.** If a new project adopts blind-TDD but has no existing test infrastructure, Agent #1 will try to write tests against nonexistent directories. Proposed: the schema validator checks that every `gate.blind_tdd.test_dirs` directory exists before running Agent #1. Missing dirs auto-escalate to human with a setup-prompt.

### OQ5. Cost amplification

**Risk level: MEDIUM.** Every task now spawns at minimum 2 agents (writer + runner) and possibly 3+ (arbiter on challenge). For a 10-task plan, that's 20-40+ agent spawns vs. today's 0. Cross-context, this is a real token budget concern.

**Mitigation ideas:**
- Allow projects to disable blind-TDD per task via `task.blind_tdd.enabled = false` in plan.md for low-value tasks (typo fixes, docs).
- Cache the red-phase result by test file hash; skip Agent #2 if tests are byte-identical to a previously-passed green phase.
- Batch multiple criteria into a single Agent #1 spawn rather than one per criterion.

### OQ6. Flaky test detection

**Risk level: LOW.** A test that passes green phase might fail on the next run due to timing, network, randomness. The current design does not detect this. Proposed: run green phase 3 times, require all 3 passes.

**Deferred to Track 4.** Initial release accepts the flakiness risk.

---

## Build order (unchanged from 2026-04-10 discussion)

### Track 1 — smart_gate fixes
**Status: COMPLETE (committed to `ralph-universal/master`).**

### Track 2 — human input channel infrastructure
**Status: COMPLETE (part of Track 1, `tools/human_input.py`). Dashboard surfacing deferred to Track 4.**

### Track 3 — blind-TDD gate (THIS RFC)

Estimated 1,500-2,000 LOC in `ralph-universal/tools/` plus schema changes and migration docs.

Components:
1. **Schema validator for plan.md** — rejects tasks missing `acceptance_criteria` or `public_surface`
2. **`tools/blind_gate.py`** — orchestration script, spawns agents, runs red/green phases, manages challenges <!-- doc-lint:ignore (RFC future-state; implemented as tools/blind_tdd/ package) -->
3. **Agent prompt templates** — `prompts/blind_test_writer.md`, `prompts/blind_test_runner.md`, `prompts/blind_arbiter.md`
4. **Tool wrapper** — the preflight rejection mechanism (depends on OQ1 resolution)
5. **Observation schema extensions** — new record types in `observe.py`
6. **`public_api.md` format spec and first-project example**
7. **Ralph config shape** — new `gate.blind_tdd.*` keys
8. **Coverage verifier** — cross-references criterion IDs against test annotations
9. **Challenge protocol orchestration** — tracks cap, spawns arbiter, routes rulings
10. **Migration guide** — step-by-step opt-in for a project

### Track 4 — integrations and polish

- CLAUDE.md mandate rewrite (R11)
- Dashboard tiles for blind-TDD metrics (R13, R2.7)
- New lesson patterns: `untestable-pattern`, `flaky-criterion`, `spec-unclear`, `challenge-rate-anomaly`
- Flaky test detection (OQ6)
- Per-task `blind_tdd.enabled` override (OQ5 mitigation)

---

## Appendix A — schema definitions

### Human input request file (shared between Tracks 1-4)

```json
{
  "id": "string (unique)",
  "task": "string (task identifier) | null",
  "agent": "string (name of the requesting tool/agent)",
  "phase": "string (e.g. 'coverage-gap', 'spec-unclear', 'arbitration')",
  "criterion": "string (AC-N) | null",
  "category": "string (machine-readable category)",
  "question": "string (verbatim shown to human)",
  "options": ["string", ...] | null,
  "free_text_allowed": "bool",
  "blocking": "bool",
  "timeout_seconds": "int",
  "created_at": "ISO-8601 UTC string"
}
```

### Human input response file

```json
{
  "request_id": "string (matches request id)",
  "chosen_option": "string | null",
  "free_text": "string | null",
  "responded_at": "ISO-8601 UTC string",
  "responder": "human"
}
```

### Task spec with blind-TDD fields

```json
{
  "id": "task-42",
  "category": "feature",
  "priority": 1,
  "description": "Add invoke_repeating to MonoBehaviour",
  "acceptance_criteria": [
    {
      "id": "AC-1",
      "given": "a MonoBehaviour X with a method foo that increments a counter",
      "when": "X.invoke_repeating('foo', 0.5, 1.0) is called",
      "then": "foo runs after 0.5s, then every 1.0s thereafter"
    },
    {
      "id": "AC-2",
      "given": "AC-1 setup with foo scheduled",
      "when": "X.cancel_invoke('foo') is called",
      "then": "foo does not run again, even if a tick was pending"
    }
  ],
  "public_surface": {
    "module": "src.engine.core",
    "class": "MonoBehaviour",
    "adds": [
      "def invoke_repeating(self, method_name: str, delay: float, repeat_rate: float) -> None",
      "def cancel_invoke(self, method_name: str | None = None) -> None"
    ],
    "modifies": [],
    "external_refs": [
      "https://docs.unity3d.com/ScriptReference/MonoBehaviour.InvokeRepeating.html",
      "https://docs.unity3d.com/ScriptReference/MonoBehaviour.CancelInvoke.html"
    ]
  },
  "passes": false
}
```

### Triage report (Agent #1 output)

```json
{
  "task": "task-42",
  "agent_id": "blind-writer-abc123",
  "triage": [
    {
      "criterion": "AC-1",
      "status": "tested",
      "test_file": "tests/engine/test_invoke.py",
      "test_names": ["test_invoke_repeating_fires_after_delay"]
    },
    {
      "criterion": "AC-5",
      "status": "needs_human",
      "reason": "subjective",
      "note": "Criterion says 'parallax scrolls smoothly'. 'Smoothly' is subjective — any deterministic test would just assert that transform.x changes each frame, which doesn't capture what 'smoothly' means. Need human to define an objective jitter metric, mark as manual verification, or reword."
    }
  ]
}
```

### Challenge document

```json
{
  "task": "task-42",
  "challenger": "implementing-agent-xyz",
  "test_file": "tests/engine/test_invoke.py",
  "test_name": "test_invoke_repeating_fires_after_delay",
  "criterion": "AC-1",
  "argument": "The test asserts foo is called exactly twice in 1.5 seconds starting at t=0, but AC-1 says 'after 0.5s, then every 1.0s', which means calls at t=0.5 and t=1.5, so exactly 2 calls between t=0 and t=1.5 is correct. However the test uses time.sleep(1.5) which drifts on Windows by up to 50ms, so the test is flaky. Proposed correct test: use a mocked clock.",
  "proposed_fix": "Use freezegun or a mock clock fixture instead of time.sleep"
}
```

### Arbiter ruling

```json
{
  "task": "task-42",
  "challenge_id": "chal-001",
  "arbiter_id": "arbiter-def456",
  "ruling": "upheld",
  "reasoning": "The test design is flaky on Windows due to time.sleep drift. The argument is valid. Route to Agent #1 for rewrite with a mocked clock.",
  "resolution": "rewrite_with_mocked_clock"
}
```

### New observation record types

```json
{
  "type": "blind_red_phase",
  "task": "task-42",
  "timestamp": "...",
  "agent_id": "blind-writer-abc123",
  "test_files": ["tests/engine/test_invoke.py"],
  "test_file_hashes": {"tests/engine/test_invoke.py": "sha256:..."},
  "triage_report": { ... },
  "red_pass": true,
  "expected_failures": ["ImportError", "AttributeError"],
  "actual_failures": ["ImportError: cannot import name 'invoke_repeating'"]
}
```

```json
{
  "type": "blind_green_phase",
  "task": "task-42",
  "timestamp": "...",
  "agent_id": "blind-runner-ghi789",
  "test_file_hashes_match": true,
  "pass_count": 12,
  "fail_count": 0,
  "coverage_verified": true,
  "missing_ac_ids": [],
  "green_pass": true
}
```

```json
{
  "type": "blindness_violation_attempt",
  "task": "task-42",
  "timestamp": "...",
  "agent_id": "blind-writer-abc123",
  "attempted_path": "src/engine/core.py",
  "tool": "Read",
  "rejected": true,
  "reason": "path outside blind whitelist"
}
```

---

## Appendix B — glossary

- **Blind agent**: an agent whose tool access is restricted to a whitelist that excludes `src/`, `examples/`, and other implementation paths.
- **Red phase**: the period between Agent #1 writing tests and the implementing agent starting work. Tests are expected to fail with import/attribute errors.
- **Green phase**: the period after the implementing agent commits, when Agent #2 runs the tests and they must pass.
- **Challenge**: a formal dispute of a test, filed by the implementing agent when it believes the test is incorrect.
- **Arbiter**: Agent #3, a fresh blind agent that rules on challenges.
- **Triage**: Agent #1's per-criterion classification of whether a criterion can be tested automatically or needs human input.
- **Acceptance criterion (AC-N)**: a single Given/When/Then statement from the task spec.
- **Public surface**: the declared set of modules/classes/functions the task will add or modify, including import paths and signatures.
- **Coverage verification**: the gate's check that every declared AC has at least one passing test tagged with its ID.
- **Preflight rejection**: the tool wrapper's mechanism for blocking a Read/Grep/Bash call before it executes if the path is outside the whitelist.

---

## Remaining work

Tracks 1, 2, and the Track 3 foundation + agent layer have shipped. The remaining pieces are orchestration glue — well-defined, bounded, and unblocked by any open design questions.

### Phase A — Integrate Track 3 into the real pipeline (next session priority)

The orchestrator currently only supports `ManualSpawner`, which writes briefs to disk for a human to run agents against. To make blind-TDD work inside a running Claude Code session, we need a real spawner that invokes the Agent tool.

#### A1. `tools/blind_tdd/spawners/claude_code_spawner.py`

**Goal:** Implement `ClaudeCodeSpawner(AgentSpawner)` that spawns Agents #1/#2/#3 via the Agent tool within a Claude Code session.

**Inputs:** role name, prompt template text, inputs dict
**Outputs:** the standard spawner result dict (`success`, `output_files`, `agent_id`, `error`)

**Key implementation details:**
- Before spawning, write the role-appropriate `settings.blind-{writer,runner,arbiter}.json` template to `.claude/settings.local.json` so the PreToolUse/PostToolUse hooks activate for the child agent.
- Copy the hook scripts from `templates/hooks/` to `.claude/hooks/` if not already present.
- Construct the Agent tool's prompt by concatenating the prompt template with the task-specific inputs dict formatted as a context block.
- Wait for the Agent call to return. On return, verify that the expected output files exist (triage report for writer, green report for runner, ruling for arbiter).
- Read the audit log via `session.audit_violations()` and include it in the spawn result.
- Restore the previous `.claude/settings.local.json` on exit (save/restore pattern).

**Risk:** the Agent tool's API for setting per-spawn settings may require research. Claude Code's `Agent` tool accepts `subagent_type` but not (as far as I know) per-spawn settings.json overrides. The fallback is to write `.claude/settings.local.json` before the spawn and restore after. That approach works if Claude Code re-reads settings per agent spawn.

**Estimated effort:** ~200-300 lines, 1-2 hours including testing with a real spawn.

#### A2. `tools/blind_tdd/spawners/agent_sdk_spawner.py`

**Goal:** Implement `AgentSdkSpawner(AgentSpawner)` for standalone (non-Claude-Code) automation using the Claude Agent SDK.

**Inputs/outputs:** same interface as ClaudeCodeSpawner.

**Key implementation details:**
- Use `claude_agent_sdk.query()` or `ClaudeAgentOptions` with `AgentDefinition(tools=[...])` for explicit tool scoping (Layer 2).
- Hooks are configured via the SDK's hooks option instead of `.claude/settings.local.json`.
- Returns when the SDK's result stream completes.

**Estimated effort:** ~200 lines, 1-2 hours. Lower priority than A1 because ralph runs interactively today.

#### A3. Wire `BlindTddOrchestrator` into `smart_gate.py`

**Goal:** Make the blind gate a real phase in front of smart_gate when `gate.blind_tdd.enabled = true` in the project's config.

**Key implementation details:**
- Add config parsing: `gate.blind_tdd.enabled`, `gate.blind_tdd.enforcement`, `gate.blind_tdd.test_dirs`, `gate.blind_tdd.max_challenges_per_task`, `gate.blind_tdd.max_challenges_per_criterion`, `gate.blind_tdd.human_input_timeout`, `gate.blind_tdd.public_api_file`.
- In `smart_gate.main()`, after the denylist/secrets/syntax checks but before tests, call `BlindTddOrchestrator.run_red_phase(task)` if enabled.
- The blind gate runs per-TASK, not per-COMMIT. Smart gate is per-commit. This means we need a way to identify "the current task" — read from `.ralph/current_task.json` or parse from `plan.md` based on which task is marked in-progress.
- If red phase fails, gate fails and caller goes back to the implementing agent with the red-phase reason.
- Between red and green phases, the smart_gate flow hands control back to ralph for the implementing agent to code.
- On the next gate run (after implementation), detect that a red phase record exists for the current task and proceed to green phase instead of re-running red.
- Green phase result folds into the existing `collector.record_check()` flow as a new "blind_tdd" check.

**Estimated effort:** ~300-400 lines, 2-4 hours. This is the biggest integration touch-point.

**Open question:** how does `smart_gate.py` know which task is currently being worked on? Options:
- (a) Parse `plan.md` for the first `"passes": false` task (fragile — multiple in-progress tasks break this)
- (b) Track current task in `.ralph/current_task.json` updated by ralph's task-selection logic
- (c) Require the caller to pass `--task <id>` explicitly
- I'd recommend (b) with (c) as override — cleanest and most flexible.

### Phase B — Challenge protocol (follow-on)

Lowest frequency, can wait until Phase A is validated end-to-end.

#### B1. `tools/blind_tdd/challenge.py`

**Goal:** Orchestrate the challenge → arbiter → ruling → resolution flow.

**Key implementation details:**
- `file_challenge(task_id, test_file, test_name, criterion, argument, proposed_fix)` writes `.ralph/blind_tdd/challenges/<challenge_id>.json`
- `spawn_arbiter(challenge_id)` reads the challenge, spawns Agent #3 via the arbiter settings, waits for `.ralph/blind_tdd/rulings/<challenge_id>.json`
- `apply_ruling(challenge_id, ruling)` routes:
  - `upheld` → delete the disputed test file, route criterion back to a fresh Agent #1 with the arbiter's reasoning as additional context
  - `rejected` → log and tell the implementing agent to make the test pass
  - `ambiguous` → write a human input request and block
- `check_challenge_caps(task_id)` enforces `max_challenges_per_task` and `max_challenges_per_criterion` — if exceeded, further challenges are rejected and the implementing agent must escalate to human
- Observations: `challenge_filed`, `arbiter_ruling`, `challenge_cap_exceeded`

**Estimated effort:** ~300-400 lines, 2-3 hours.

#### B2. Challenge integration with the orchestrator

- `BlindTddOrchestrator.run_green_phase()` already returns the green report. If it fails, the caller (the implementing agent) can now file a challenge via `challenge.file_challenge()`.
- The orchestrator needs a `resume_green_phase(task)` method that re-runs green after a challenge resolution (upheld → test rewritten → re-run).

### Phase C — Observations and dashboard (polish)

#### C1. Extend `observe.py` with new record types

Add validators/helpers for the new observation types from Appendix A:
- `blind_red_phase`
- `blind_green_phase`
- `challenge_filed`
- `arbiter_ruling`
- `triage_escalation`
- `blindness_violation_attempt`
- `human_input_requested`
- `human_input_resolved`

The orchestrator already writes these as raw dicts via `_record_observation()`. `observe.py` needs matching classifier entries so the analytics/dashboard code can aggregate them.

**Estimated effort:** ~100-150 lines, 1 hour.

#### C2. Dashboard tiles for blind-TDD metrics

Add to `tools/dashboard_server.py`:
- Count of tasks with active red phase (pending implementation)
- Count of tasks in green phase
- Challenge rate (challenges filed / tasks with blind gate enabled)
- Arbiter ruling distribution (upheld/rejected/ambiguous counts)
- Triage category histogram (subjective/visual/flaky_timing/etc.)
- Blindness violation attempt count (should be 0; any >0 is a red flag)
- Pending human input request count (already in R2.7, hasn't been built yet)

**Estimated effort:** ~200 lines, 1-2 hours.

#### C3. Cross-project lesson patterns

Add to `tools/observe.py` (or a new `tools/lesson_extractor.py`):
- Auto-extract lesson when `needs_human` triage category repeats ≥ 3 times across projects → lesson `untestable-pattern-<category>.md`
- Auto-extract lesson when same criterion phrasing produces `ambiguous` rulings ≥ 2 times → lesson `spec-unclear-phrasing.md`
- Auto-extract lesson when an agent has > 10% blindness violation attempt rate → lesson about prompt clarity

**Estimated effort:** ~150 lines, 1 hour.

### Phase D — CLAUDE.md mandate update

#### D1. Update ralph-universal CLAUDE.md

- Document the blind-TDD workflow at the top level so it applies to all ralph projects
- Reference this RFC for the full spec
- Explicitly scope the existing post-task validation agent to `tests/mutation/` and `tests/audit/` (per R11)
- Add a checklist of what a project needs to do to adopt blind-TDD (copy hooks, settings, add `public_api.md`, add `acceptance_criteria` + `public_surface` to tasks, set `gate.blind_tdd.enabled = true`)

**Estimated effort:** ~100 lines (pure docs), 30 minutes.

### Phase E — Migration guide and first adopter

#### E1. `docs/blind-tdd-adoption-guide.md`

Step-by-step guide for opting a project in:

1. Copy `templates/hooks/blind_tdd_path_guard.py` and `blind_tdd_audit.py` to the project's `.claude/hooks/`
2. Copy the appropriate settings template to `.claude/settings.local.json` (or set up per-agent settings via the SDK)
3. Create `public_api.md` at the project root with the current public surface
4. Add `gate.blind_tdd.enabled = true` to the project's ralph config
5. Update any in-progress task in `plan.md` to include `acceptance_criteria` and `public_surface` fields
6. Run `smart_gate.py` — the blind gate should engage on the next task

**Estimated effort:** ~200 lines, 1 hour.

#### E2. unity-py-sim as first adopter

- Write `public_api.md` for unity-py-sim listing the current public surface of `src/engine/`
- Pick a concrete near-term task (e.g. a translator fix or a new engine API) and add `acceptance_criteria` + `public_surface` fields to it
- Enable `gate.blind_tdd.enabled = true` in `unity-py-sim/ralph.config.json`
- Run the pipeline end-to-end
- Log any friction as observations and fix in ralph-universal

**Estimated effort:** 2-4 hours depending on how much friction appears.

---

## Suggested next session order

1. **A1** — ClaudeCodeSpawner (highest leverage, unblocks everything else)
2. **A3** — wire orchestrator into smart_gate.py (closes the end-to-end loop)
3. **E1 + E2** — migration guide + unity-py-sim adoption (validates the whole system on a real project)
4. **B1 + B2** — challenge protocol (after real-world validation exposes which challenge cases actually occur)
5. **C1 → C3** — polish, observations, dashboard tiles
6. **D1** — CLAUDE.md updates (last because it documents what was built, not what to build)

## Open questions still outstanding (unchanged from original RFC)

- **OQ2** (public_api.md maintenance): how it stays in sync with actual exports. Addressed in Phase E with "implementing agent updates public_api.md as part of the commit."
- **OQ3** (language-specific test runners): covered by Agent #2's use of `stack.test_runner` config.
- **OQ4** (projects with no tests): schema validator checks test dirs exist; missing dirs auto-escalate to human.
- **OQ5** (cost amplification): mitigations are (a) per-task blind_tdd.enabled opt-out, (b) red-phase result caching by hash, (c) batching criteria per spawn. These are polish items, not blockers.
- **OQ6** (flaky test detection): deferred to Track 4 / Phase C polish.

**Tracks 1 and 2 have already shipped**, so the dashboard "100% gate pass / 0 tests ran" problem is already fixed. Track 3 foundation + agent layer has shipped, but end-to-end validation via a real spawner is the next big milestone.
