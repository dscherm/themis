"""Spec health — measuring the unmeasurability rate of a project's specs.

A criterion the blind writer cannot honestly test is escalated `needs_human`
with a reason code (subjective, visual, nondeterministic, spec_unclear, ...).
Each escalation is already recorded in `.themis/observations.jsonl`; this
module aggregates them into a rate: of all acceptance criteria that reached
the writer, what fraction came back untestable? A rising rate is upstream
spec-writing drifting away from the executably-specifiable — visible here
before it shows up as wasted gate runs.

Scope honesty (see docs/limitations.md): unmeasurability is ONE of three ways
a spec is bad, and the only mechanically detectable one. A spec can be
perfectly measurable and wrong, or measurable and incomplete — this metric
sees neither. It is a floor gauge, not a quality score, and it must not
become a target: optimizing specs *for* measurability produces brittle,
over-specified criteria (Goodhart, one level up).

Usage:
    python -m blind_tdd.spec_health              # current project
    python -m blind_tdd.spec_health --project DIR --json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path


OBSERVATIONS_RELPATH = Path(".themis") / "observations.jsonl"

# Red-phase event types that carry per-task criteria outcomes.
_RED_EVENT_TYPES = ("blind_red_phase", "blind_red_phase_resumed")

# Below this many criteria, a rate is noise (1 escalation of 2 criteria
# reads as 50%). Callers should say "insufficient data" instead.
MIN_CRITERIA_FOR_SIGNAL = 5

RECENT_TASK_WINDOW = 10


def load_events(observations_path: Path) -> list[dict]:
    """All parseable observation events, file order. Missing file → []."""
    try:
        text = observations_path.read_text(encoding="utf-8")
    except OSError:
        return []
    events: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events


def spec_health(project: Path | str = ".") -> dict:
    """Aggregate the unmeasurability rate for a project.

    Per task, only the LATEST red-phase event counts (a resume after an
    upheld challenge replaces the original red's numbers). Reason codes come
    from `triage_escalation` events, deduplicated by (task, criterion).

    Returns a dict with:
        tasks               — tasks with at least one red-phase event
        criteria_total      — tested + escalated criteria across those tasks
        criteria_escalated  — needs_human criteria
        rate                — escalated / total (None when total is 0)
        recent_rate         — same, over the last RECENT_TASK_WINDOW tasks
        rising              — recent_rate exceeds overall rate by >5 points
        sufficient          — criteria_total >= MIN_CRITERIA_FOR_SIGNAL
        by_reason           — reason code → escalation count
    """
    events = load_events(Path(project) / OBSERVATIONS_RELPATH)

    # Latest red event per task, preserving first-seen task order.
    per_task: dict[str, dict] = {}
    task_order: list[str] = []
    for e in events:
        if e.get("type") not in _RED_EVENT_TYPES:
            continue
        task = str(e.get("task", "")).strip()
        if not task:
            continue
        if task not in per_task:
            task_order.append(task)
        per_task[task] = {
            "tested": len(e.get("tested_criteria") or []),
            "escalated": len(e.get("needs_human_criteria") or []),
        }

    # Reason breakdown, deduped by (task, criterion) keeping the latest.
    reasons_by_key: dict[tuple[str, str], str] = {}
    for e in events:
        if e.get("type") != "triage_escalation":
            continue
        key = (str(e.get("task", "")), str(e.get("criterion", "")))
        reason = str(e.get("reason") or e.get("category") or "unknown").strip() or "unknown"
        reasons_by_key[key] = reason
    by_reason = Counter(reasons_by_key.values())

    def _rate(tasks: list[str]) -> tuple[int, int, float | None]:
        total = sum(per_task[t]["tested"] + per_task[t]["escalated"] for t in tasks)
        escalated = sum(per_task[t]["escalated"] for t in tasks)
        return total, escalated, (escalated / total if total else None)

    total, escalated, rate = _rate(task_order)
    recent_tasks = task_order[-RECENT_TASK_WINDOW:]
    _, _, recent_rate = _rate(recent_tasks)

    rising = (
        rate is not None and recent_rate is not None
        and recent_rate > rate + 0.05
    )

    return {
        "tasks": len(task_order),
        "criteria_total": total,
        "criteria_escalated": escalated,
        "rate": rate,
        "recent_rate": recent_rate,
        "rising": rising,
        "sufficient": total >= MIN_CRITERIA_FOR_SIGNAL,
        "by_reason": dict(by_reason.most_common()),
    }


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.0f}%"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m blind_tdd.spec_health",
        description="Unmeasurability rate of a project's specs, from the "
                    "blind writer's needs_human escalations.",
    )
    ap.add_argument("--project", default=".", metavar="DIR")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ns = ap.parse_args(argv)

    health = spec_health(ns.project)
    if ns.json:
        print(json.dumps(health, indent=2))
        return 0

    if health["tasks"] == 0:
        print("No red-phase observations yet — the gate hasn't run a writer here.")
        return 0

    print(f"Spec health — {Path(ns.project).resolve().name}")
    print(f"  tasks observed:      {health['tasks']}")
    print(f"  criteria:            {health['criteria_total']} "
          f"({health['criteria_escalated']} escalated needs_human)")
    print(f"  unmeasurability:     {_pct(health['rate'])} overall, "
          f"{_pct(health['recent_rate'])} recent"
          + ("  ⚠ RISING" if health["rising"] else ""))
    if not health["sufficient"]:
        print(f"  (fewer than {MIN_CRITERIA_FOR_SIGNAL} criteria — treat the rate as noise)")
    if health["by_reason"]:
        print("  escalation reasons:")
        for reason, count in health["by_reason"].items():
            print(f"    {reason:<18} {count}")
    print(
        "\nThis measures the detectable third of spec badness (untestable\n"
        "criteria). Measurable-but-wrong and measurable-but-incomplete specs\n"
        "are invisible here — spec review stays human. Don't optimize FOR\n"
        "this number; use it to find where spec-writing needs help."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
