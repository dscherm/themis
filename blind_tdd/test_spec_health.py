"""Tests for the spec-health metric (spec_health.py).

The metric's load-bearing properties: only the latest red event per task
counts (a resume replaces the original), reasons dedupe by (task, criterion),
the recent window tracks task order, and thin data is flagged as
insufficient rather than reported as a confident rate.
"""

from __future__ import annotations

import json
from pathlib import Path

from blind_tdd.spec_health import (
    MIN_CRITERIA_FOR_SIGNAL,
    OBSERVATIONS_RELPATH,
    main,
    spec_health,
)


def _write_events(project: Path, events: list[dict]) -> None:
    obs = project / OBSERVATIONS_RELPATH
    obs.parent.mkdir(parents=True, exist_ok=True)
    obs.write_text(
        "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8"
    )


def _red(task: str, tested: int, escalated: int, kind: str = "blind_red_phase") -> dict:
    return {
        "type": kind,
        "task": task,
        "tested_criteria": [f"AC-{i + 1}" for i in range(tested)],
        "needs_human_criteria": [f"AC-{tested + i + 1}" for i in range(escalated)],
    }


def _escalation(task: str, criterion: str, reason: str) -> dict:
    return {"type": "triage_escalation", "task": task, "criterion": criterion,
            "reason": reason, "category": "ignored-when-reason-present"}


def test_no_observations_is_empty_not_error(tmp_path: Path):
    health = spec_health(tmp_path)
    assert health["tasks"] == 0
    assert health["rate"] is None
    assert health["sufficient"] is False


def test_basic_rate(tmp_path: Path):
    _write_events(tmp_path, [
        _red("t1", tested=4, escalated=1),
        _red("t2", tested=3, escalated=2),
    ])
    health = spec_health(tmp_path)
    assert health["tasks"] == 2
    assert health["criteria_total"] == 10
    assert health["criteria_escalated"] == 3
    assert health["rate"] == 0.3
    assert health["sufficient"] is True


def test_latest_red_event_per_task_wins(tmp_path: Path):
    """A resumed red phase (after an upheld challenge) replaces the original
    task's numbers — no double counting."""
    _write_events(tmp_path, [
        _red("t1", tested=2, escalated=3),
        _red("t1", tested=5, escalated=0, kind="blind_red_phase_resumed"),
    ])
    health = spec_health(tmp_path)
    assert health["tasks"] == 1
    assert health["criteria_total"] == 5
    assert health["criteria_escalated"] == 0
    assert health["rate"] == 0.0


def test_reasons_dedupe_by_task_and_criterion(tmp_path: Path):
    _write_events(tmp_path, [
        _red("t1", tested=3, escalated=2),
        _escalation("t1", "AC-4", "subjective"),
        _escalation("t1", "AC-4", "spec_unclear"),  # re-triaged: latest wins
        _escalation("t1", "AC-5", "visual"),
    ])
    health = spec_health(tmp_path)
    assert health["by_reason"] == {"spec_unclear": 1, "visual": 1}


def test_recent_window_and_rising_flag(tmp_path: Path):
    # 15 clean early tasks, then 10 recent tasks escalating heavily:
    # recent rate far above overall → rising.
    events = [_red(f"early-{i}", tested=4, escalated=0) for i in range(15)]
    events += [_red(f"late-{i}", tested=2, escalated=2) for i in range(10)]
    _write_events(tmp_path, events)
    health = spec_health(tmp_path)
    assert health["recent_rate"] == 0.5
    assert health["rate"] < health["recent_rate"]
    assert health["rising"] is True


def test_insufficient_data_flagged(tmp_path: Path):
    _write_events(tmp_path, [_red("t1", tested=1, escalated=1)])
    health = spec_health(tmp_path)
    assert health["criteria_total"] < MIN_CRITERIA_FOR_SIGNAL
    assert health["sufficient"] is False
    assert health["rate"] == 0.5  # still reported, caller decides


def test_corrupt_lines_skipped(tmp_path: Path):
    obs = tmp_path / OBSERVATIONS_RELPATH
    obs.parent.mkdir(parents=True)
    obs.write_text(
        json.dumps(_red("t1", tested=5, escalated=1)) + "\n{broken\n", encoding="utf-8"
    )
    health = spec_health(tmp_path)
    assert health["tasks"] == 1


def test_cli_human_and_json(tmp_path: Path, capsys):
    _write_events(tmp_path, [
        _red("t1", tested=4, escalated=1),
        _escalation("t1", "AC-5", "subjective"),
    ])
    assert main(["--project", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "20%" in out and "subjective" in out
    assert "spec review stays human" in out  # the honesty footer

    assert main(["--project", str(tmp_path), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["rate"] == 0.2


def test_cli_no_data(tmp_path: Path, capsys):
    assert main(["--project", str(tmp_path)]) == 0
    assert "hasn't run a writer" in capsys.readouterr().out


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
