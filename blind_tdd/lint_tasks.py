"""Commit-time task spec linter.

Runs at the same point a new task is authored — during `smart_gate.py`
when `plan.md` (or `fix_plan.md`) is in the changed files list. **Advisory
by default**: logs warnings, does not block commits unless
`gate.blind_tdd.lint_plan_enforcement = "strict"` is explicitly set.

## What it checks

Structural checks (same as preflight, reused via `preflight_task`):
  - Subjective language in `then` clauses
  - Public-surface coverage
  - Criterion observability
  - Tasks that will fail preflight at spawn time are flagged here first
    so the author sees the issue before the blind gate kicks off

Commit-time-specific checks (new here, not in preflight):
  1. Criterion ID gaps (AC-1, AC-2, AC-4 skipping AC-3)
  2. Duplicate task IDs across plan.md
  3. public_surface.adds / modifies conflicts (same signature in both)
  4. Stale `passes: true` tasks with `acceptance_criteria` that never
     had a `blind_green_phase` observation recorded (gated behind
     `include_historical` since fresh clones have no history)

## CLI

    python -m blind_tdd.lint_tasks plan.md
    python -m blind_tdd.lint_tasks plan.md --strict --include-historical

Exit codes:
    0 — no findings, or only warnings (advisory mode)
    1 — errors found and strict mode enabled
    2 — input file not found / unparseable

## Integration with smart_gate

`smart_gate.py` imports `lint_tasks.lint_plan_file()` and runs it when
`plan.md` is in the changed files list. Behind
`gate.blind_tdd.lint_plan = true` (default true). Results recorded on
the ObservationCollector as a new `plan_lint` check.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .preflight import preflight_task, MODE_OFF, MODE_STRICT, MODE_WARN


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class LintFinding:
    task_id: str
    kind: str  # "gap" | "duplicate_task" | "surface_conflict" | "stale" | "preflight_<name>"
    severity: str  # "error" | "warning"
    message: str


@dataclass
class LintResult:
    ok: bool
    mode: str = MODE_WARN  # default advisory
    findings: list[LintFinding] = field(default_factory=list)
    tasks_scanned: int = 0

    @property
    def errors(self) -> list[LintFinding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def warnings(self) -> list[LintFinding]:
        return [f for f in self.findings if f.severity == "warning"]

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "mode": self.mode,
            "tasks_scanned": self.tasks_scanned,
            "findings": [
                {
                    "task_id": f.task_id,
                    "kind": f.kind,
                    "severity": f.severity,
                    "message": f.message,
                }
                for f in self.findings
            ],
        }


# ---------------------------------------------------------------------------
# plan.md parsing (reuses the same extractor as gate_integration)
# ---------------------------------------------------------------------------

def _extract_json_blocks(text: str) -> list[str]:
    """Yield the inside of every ```json ... ``` fence in the text."""
    blocks: list[str] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if lines[i].strip().startswith("```json"):
            i += 1
            buf: list[str] = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            if buf:
                blocks.append("\n".join(buf))
        i += 1
    return blocks


def _iter_tasks(obj) -> list[dict]:
    """Flatten a plan JSON object into a list of task dicts."""
    out: list[dict] = []
    if isinstance(obj, dict):
        if "id" in obj and (
            "title" in obj or "description" in obj or "acceptance_criteria" in obj
        ):
            out.append(obj)
        for key in ("tasks", "items", "backlog"):
            v = obj.get(key)
            if isinstance(v, list):
                for item in v:
                    if isinstance(item, dict):
                        out.append(item)
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict):
                out.append(item)
    return out


def parse_plan(path: Path) -> list[dict]:
    """Extract all task dicts from a plan.md file."""
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    tasks: list[dict] = []
    for block in _extract_json_blocks(text):
        try:
            obj = json.loads(block)
        except json.JSONDecodeError:
            continue
        tasks.extend(_iter_tasks(obj))
    return tasks


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

_AC_ID_RE = re.compile(r"^AC-(\d+)$", re.IGNORECASE)


def _check_criterion_gaps(task: dict) -> list[LintFinding]:
    """Warn if criterion IDs are non-contiguous starting at AC-1."""
    task_id = str(task.get("id", "?"))
    crits = task.get("acceptance_criteria") or []
    if not isinstance(crits, list):
        return []

    nums: list[int] = []
    for c in crits:
        if not isinstance(c, dict):
            continue
        m = _AC_ID_RE.match(str(c.get("id", "")))
        if m:
            nums.append(int(m.group(1)))

    if not nums:
        return []
    nums.sort()
    expected = list(range(1, max(nums) + 1))
    missing = [n for n in expected if n not in nums]
    if missing:
        return [LintFinding(
            task_id=task_id,
            kind="gap",
            severity="warning",
            message=(
                f"criterion ID gap in task {task_id!r}: expected "
                f"AC-1..AC-{max(nums)}, missing {['AC-' + str(n) for n in missing]}. "
                f"This is usually a copy-paste error."
            ),
        )]
    return []


def _check_duplicate_task_ids(tasks: Iterable[dict]) -> list[LintFinding]:
    """Warn if two tasks share the same id."""
    seen: dict[str, int] = {}
    for t in tasks:
        tid = str(t.get("id", ""))
        if not tid:
            continue
        seen[tid] = seen.get(tid, 0) + 1
    out: list[LintFinding] = []
    for tid, count in seen.items():
        if count > 1:
            out.append(LintFinding(
                task_id=tid,
                kind="duplicate_task",
                severity="warning",
                message=(
                    f"task id {tid!r} appears {count} times in plan — "
                    f"task IDs must be unique for current_task.json "
                    f"resolution and red-state persistence."
                ),
            ))
    return out


def _check_surface_conflicts(task: dict) -> list[LintFinding]:
    """Warn if the same signature appears in both adds and modifies."""
    task_id = str(task.get("id", "?"))
    surface = task.get("public_surface") or {}
    if not isinstance(surface, dict):
        return []
    adds = surface.get("adds") or []
    modifies = surface.get("modifies") or []
    if not isinstance(adds, list) or not isinstance(modifies, list):
        return []
    adds_set = {s for s in adds if isinstance(s, str)}
    mods_set = {s for s in modifies if isinstance(s, str)}
    overlap = adds_set & mods_set
    if overlap:
        return [LintFinding(
            task_id=task_id,
            kind="surface_conflict",
            severity="warning",
            message=(
                f"public_surface adds+modifies overlap in {task_id!r}: "
                f"{sorted(overlap)}. A signature cannot be simultaneously "
                f"added and modified."
            ),
        )]
    return []


def _check_stale_completed(
    task: dict,
    completed_task_ids_with_green: set[str],
    include_historical: bool,
) -> list[LintFinding]:
    """Warn if a task has passes:true + acceptance_criteria but no
    blind_green_phase observation was ever recorded for it.

    Only fires when `include_historical=True` to avoid flooding on
    fresh clones without observation history.
    """
    if not include_historical:
        return []
    if not task.get("passes"):
        return []
    crits = task.get("acceptance_criteria")
    if not isinstance(crits, list) or not crits:
        return []
    task_id = str(task.get("id", ""))
    if not task_id:
        return []
    if task_id in completed_task_ids_with_green:
        return []
    return [LintFinding(
        task_id=task_id,
        kind="stale",
        severity="warning",
        message=(
            f"task {task_id!r} has passes:true with acceptance_criteria "
            f"but never produced a blind_green_phase observation. Either "
            f"it was completed before the blind-TDD gate was enabled, "
            f"or the green phase was skipped."
        ),
    )]


def _load_green_passed_task_ids(observations_path: Path) -> set[str]:
    """Scan observations.jsonl for tasks that had a green_pass event."""
    ids: set[str] = set()
    if not observations_path.exists():
        return ids
    try:
        with observations_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (isinstance(r, dict)
                        and r.get("type") == "blind_green_phase"
                        and r.get("green_pass") is True):
                    tid = r.get("task")
                    if tid:
                        ids.add(str(tid))
    except OSError:
        return ids
    return ids


# ---------------------------------------------------------------------------
# Preflight bridge — reuse preflight_task's structural findings
# ---------------------------------------------------------------------------

def _preflight_findings(task: dict, config: dict) -> list[LintFinding]:
    """Run preflight_task against a task and convert findings to LintFindings."""
    # Force warn mode so preflight returns findings as warnings regardless
    # of the project's strict config. The lint_plan mode decides severity.
    warn_config = {
        "gate": {
            "blind_tdd": {
                **(config.get("gate", {}).get("blind_tdd", {})),
                "preflight": MODE_WARN,
            }
        }
    }
    # Only run preflight on tasks that actually have the fields it checks
    if not task.get("acceptance_criteria") or not task.get("public_surface"):
        return []

    result = preflight_task(task, warn_config)
    task_id = str(task.get("id", "?"))
    out: list[LintFinding] = []
    for w in result.warnings:
        out.append(LintFinding(
            task_id=task_id,
            kind="preflight",
            severity="warning",
            message=w,
        ))
    return out


# ---------------------------------------------------------------------------
# Top-level
# ---------------------------------------------------------------------------

def lint_plan_file(
    plan_path: str | Path,
    *,
    config: dict | None = None,
    mode: str = MODE_WARN,
    include_historical: bool = False,
    observations_path: str | Path | None = None,
) -> LintResult:
    """Lint a plan.md file and return a LintResult.

    Args:
        plan_path: path to plan.md or fix_plan.md
        config: project config (for preflight integration)
        mode: lint enforcement — "strict", "warn" (default), or "off"
        include_historical: enable stale-completed checks (opt-in)
        observations_path: .themis/observations.jsonl path (for stale check)
    """
    mode = (mode or MODE_WARN).lower().strip()
    if mode not in {MODE_STRICT, MODE_WARN, MODE_OFF}:
        mode = MODE_WARN

    if mode == MODE_OFF:
        return LintResult(ok=True, mode=MODE_OFF)

    path = Path(plan_path)
    tasks = parse_plan(path)
    if not tasks:
        return LintResult(ok=True, mode=mode, tasks_scanned=0)

    config = config or {}
    findings: list[LintFinding] = []

    # Plan-level checks
    findings.extend(_check_duplicate_task_ids(tasks))

    # Green-passed task IDs (only scanned if include_historical)
    green_ids: set[str] = set()
    if include_historical and observations_path:
        green_ids = _load_green_passed_task_ids(Path(observations_path))

    for task in tasks:
        # Per-task checks
        findings.extend(_check_criterion_gaps(task))
        findings.extend(_check_surface_conflicts(task))
        findings.extend(_check_stale_completed(task, green_ids, include_historical))
        # Preflight bridge — only for blind-TDD-eligible tasks
        findings.extend(_preflight_findings(task, config))

    # Promote to errors under strict mode
    if mode == MODE_STRICT:
        for f in findings:
            f.severity = "error"

    ok = all(f.severity != "error" for f in findings)
    return LintResult(
        ok=ok,
        mode=mode,
        findings=findings,
        tasks_scanned=len(tasks),
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_result(result: LintResult) -> None:
    if result.tasks_scanned == 0:
        print("[lint] no tasks found")
        return
    print(f"[lint] scanned {result.tasks_scanned} task(s), mode={result.mode}")
    errs = result.errors
    warns = result.warnings
    if not errs and not warns:
        print("[lint] OK — no findings")
        return
    for f in errs:
        print(f"[lint] ERROR {f.task_id} ({f.kind}): {f.message}")
    for f in warns:
        print(f"[lint] WARN  {f.task_id} ({f.kind}): {f.message}")
    print(f"[lint] {len(errs)} error(s), {len(warns)} warning(s)")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", help="Path to plan.md or fix_plan.md")
    parser.add_argument("--strict", action="store_true",
                        help="Promote all findings to errors; exit 1 if any")
    parser.add_argument("--off", action="store_true", help="Skip linting entirely")
    parser.add_argument("--include-historical", action="store_true",
                        help="Scan .themis/observations.jsonl for stale-completed tasks")
    parser.add_argument("--observations",
                        default=".themis/observations.jsonl",
                        help="Path to observations.jsonl")
    parser.add_argument("--config", default="themis.config.json",
                        help="Path to project config (for preflight integration)")
    args = parser.parse_args()

    mode = MODE_OFF if args.off else (MODE_STRICT if args.strict else MODE_WARN)

    cfg: dict = {}
    cfg_path = Path(args.config)
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass

    result = lint_plan_file(
        args.plan,
        config=cfg,
        mode=mode,
        include_historical=args.include_historical,
        observations_path=args.observations,
    )
    _print_result(result)

    if mode == MODE_STRICT and result.errors:
        sys.exit(1)
    sys.exit(0)
