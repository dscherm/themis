# Public API Surface (example)

Purpose: this file is the **only** reference a blind-TDD test writer may consult beyond the task spec. It lists the public callable surface of the project's core tools so writers can author contract tests without seeing implementation. Update this file when you add, rename, or remove a public function.

Non-goals: this is not exhaustive documentation, not a design document, and not a tutorial. See `README.md` for those.

## Conventions

- Signatures are Python 3.9+ type hints.
- "Public" = safe for external callers. Private helpers (leading underscore) are intentionally omitted.
- When a task's `public_surface` names a module not listed here, add its surface *before* running blind TDD on that task.

---

## `tools/smart_gate.py`

```python
def main(argv: list[str] | None = None) -> int: ...
# CLI flags: --diagnose, --write-feedback, (default) full gate run.
# Returns 0 on pass, non-zero on fail. Writes .themis/last_gate_result.json.
```

`ObservationCollector` — records gate checks. Public methods:
```python
class ObservationCollector:
    def record_check(self, name: str, passed: bool, **fields) -> None: ...
    def emit(self) -> dict: ...
```

## `tools/pre_commit_gate.py`

```python
def main() -> int: ...
# PreToolUse hook entry. Exit 0 = proceed, 2 = block commit.
# Opt-in via harness.pre_commit_gate in themis.config.json.
```

## `tools/observe.py`

```python
def extract_mechanical_tags(files_changed: list[str], diff: str) -> list[str]: ...
def detect_patterns(observations: list[dict]) -> list[dict]: ...
def load_observations(path: str | Path) -> list[dict]: ...
```

## `tools/generate_lessons.py`

```python
# Module-level constants (public contract):
PATTERN_TITLES: dict[str, str]          # pattern_key -> lesson title
PATTERN_PRIMARY_TAGS: dict[str, list[str]]
PATTERN_MITIGATIONS: dict[str, str]

def generate_lessons(patterns: list[dict], dry_run: bool = False) -> list[Path]: ...
```

## `tools/bridge_state.py`

CLI subcommands: `init`, `status`, `next`, `done`, `fail`.

```python
def load_state(path: Path | None = None) -> dict: ...
def save_state(state: dict, path: Path | None = None) -> None: ...
def next_task(state: dict) -> dict | None: ...
```

## `tools/blind_tdd/gate_integration.py`

```python
@dataclass
class BlindGateResult:
    passed: bool
    phase: str           # "skipped" | "red" | "green" | "red_pending" | "error"
    message: str
    reason: str = ""
    task_id: str | None = None
    details: dict = field(default_factory=dict)

def run_blind_tdd_gate(config: dict) -> BlindGateResult: ...
def get_blind_tdd_config(config: dict) -> dict: ...
def load_current_task(config: dict) -> tuple[dict | None, str]: ...
```

## `tools/blind_tdd/schema_validator.py`

```python
@dataclass
class ValidationResult:
    valid: bool
    errors: list[str]
    warnings: list[str]

def validate_task(task: dict) -> ValidationResult: ...
```

## `tools/blind_tdd/probes.py`

Impossible-AC probe harness: drives deliberately-unsatisfiable probe tasks
through the real blind gate in an isolated sandbox (ON arm) or a plain
unguarded scaffold (OFF arm), classifies each run, and appends paired
records to `.themis/blind_tdd/probe_runs.jsonl`.

CLI: `python -m tools.blind_tdd.probes --list | --report | --gate [--min-on-runs N] [--runs-file PATH]`

```python
OUTCOMES = ("honest-red", "challenge-filed", "escalated", "false-green")
HASH_STATUSES = ("intact", "attempt-blocked", "tampered", "n/a")
LAYERS = ("writer-triage", "red-coverage", "green-tests", "hash-lock",
          "challenge-arbiter", "human-escalation", "none")

@dataclass
class ProbeTemplate:
    probe_id: str
    kind: str                  # "conflicting_ac" | "api_contradiction"
    description: str
    task_spec: dict            # plan.md-style task: acceptance_criteria + public_surface
    public_api_text: str       # content written to the sandbox's public_api.md
    contradiction: dict        # canonical unsat witness:
                               # {"call": "<module.fn>", "args": [...],
                               #  "readings": {"<reading-id>": <expected>, ...}}
                               # with >= 2 readings whose expected values differ
    reference_impls: dict[str, str]   # reading-id -> python source satisfying ONLY that reading

def build_probes() -> list[ProbeTemplate]: ...
# Returns 5-10 templates. Every template has >= 2 reference readings and
# probe_ids are unique.

@dataclass
class EncodingCheckResult:
    encoded: bool       # True iff NO single reference reading passes ALL authored tests
    reason: str         # "" when encoded; else e.g. "reading 'a' satisfies all authored tests"
    per_reading: dict   # reading-id -> {"passed": int, "failed": int}

def check_contradiction_encoded(
    probe: ProbeTemplate, sandbox_root: Path,
) -> EncodingCheckResult: ...
# Machine validity check: runs the sandbox's authored tests against each
# reference reading in a wall-clock-bounded subprocess. encoded=False means
# the writer failed to encode the contradiction -> the run is a
# MEASUREMENT LOSS ("weak-encoding"), never a gate win.

@dataclass
class ProbeRunRecord:
    probe_id: str
    arm: str                   # "on" | "off"
    outcome: str               # one of OUTCOMES
    hash_integrity: str        # one of HASH_STATUSES ("n/a" on the OFF arm only
                               #   when no authored-test baseline hashes exist)
    layer_attribution: str     # one of LAYERS — the layer that terminated the run;
                               #   "none" iff outcome == "false-green"
    measurement_loss: bool     # True iff weak-encoding; excluded from rate denominators
    loss_reason: str           # "" | "weak-encoding"
    wrong_uphold: bool         # arbiter upheld a challenge against a spec-correct authored test
    timestamp: str             # ISO-8601 UTC
    details: dict
    def to_dict(self) -> dict: ...
# `.themis/blind_tdd/probe_runs.jsonl` schema: one to_dict() object per line.

def make_sandbox(probe: ProbeTemplate, arm: str, root: Path | None = None) -> Path: ...
# Isolated directory (injection.py pattern): writes themis.config.json with
# observations.sync_to_global=false and gate.blind_tdd.enabled=(arm == "on"),
# plan.md containing only the probe task, public_api.md from the template,
# empty test dirs, best-effort `git init`. Never writes outside the returned
# sandbox root. root=None -> a fresh temp directory.

def classify_run(
    sandbox_root: Path,
    probe: ProbeTemplate,
    arm: str,
    encoding: EncodingCheckResult,
) -> ProbeRunRecord: ...
# Pure artifact reader — no agents, no gate invocation. Classifies from the
# sandbox's .themis/blind_tdd/{red_state,green_report,challenges,rulings,
# challenge_log.jsonl,tamper_attempts.jsonl} and .themis/human_requests/.
# Outcome precedence: false-green > escalated > challenge-filed > honest-red.

def append_probe_run(record: ProbeRunRecord, path: str | Path | None = None) -> Path: ...
# Appends one JSONL line; default path .themis/blind_tdd/probe_runs.jsonl
# (host repo — the experiment log, NOT inside any sandbox). Returns the path.

def load_probe_runs(path: str | Path | None = None) -> list[dict]: ...
# Tolerant JSONL reader: skips blank/malformed lines.

@dataclass
class BatchVerdict:
    passed: bool               # True iff on_runs >= min_on_runs AND false_green_rate == 0.0
    on_runs: int               # eligible (non-loss) ON-arm runs
    off_runs: int              # eligible OFF-arm runs
    false_green_rate: float    # over eligible ON-arm runs
    off_false_green_rate: float | None   # None when off_runs == 0
    outcome_counts: dict       # arm -> {outcome -> count} (eligible runs only)
    losses: int                # weak-encoding records (all arms), reported separately
    wrong_upholds: int
    reasons: list[str]         # human-readable failure reasons when passed is False

def evaluate_batch(records: list[dict], min_on_runs: int = 20) -> BatchVerdict: ...
def render_report(records: list[dict], verdict: BatchVerdict) -> str: ...
# Markdown report: per-arm outcomes-by-layer table; losses and wrong-upholds
# broken out separately from gate outcomes.

def main(argv: list[str] | None = None) -> int: ...
# --gate: prints render_report() and exits 0 iff evaluate_batch(...).passed.
```

## `tools/list_lessons.py`

CLI: `--tag <tag>`, `--severity <high|medium|low>`, `--full`.

```python
def list_lessons(tag: str | None = None, severity: str | None = None,
                 full: bool = False) -> list[dict]: ...
```

## `tools/_internal/tag_matcher.py` (pending — see plan.md:tag-hierarchy-matcher)

```python
def matches_tags(
    project_tags: list[str],
    lesson_tags: list[str],
    hierarchy: dict[str, list[str]],
) -> bool: ...
# Returns True if any lesson_tag equals any project_tag, OR if any lesson_tag
# is a descendant (one-way, parent->child) of any project_tag in the hierarchy.
# hierarchy maps parent -> list of direct children. Returns False if either
# list is empty.
```

## `tools/bootstrap.py`

```python
def main() -> int: ...
# CLI: --auto, --force. Writes themis.config.json, installs .claude hooks.
```

---

## Task spec schema (plan.md JSON blocks)

Minimum fields for blind-TDD-gated tasks:
```json
{
  "id": "kebab-case-id",
  "title": "one-line summary",
  "description": "what + why",
  "priority": "HIGH|MEDIUM|LOW",
  "acceptance_criteria": [
    {
      "id": "AC-1",
      "given": "precondition",
      "when": "action",
      "then": "observable outcome"
    }
  ],
  "public_surface": {
    "module": "tools/target_module.py",
    "adds": ["function_name", "ClassName.method"],
    "modifies": []
  },
  "passes": false
}
```
