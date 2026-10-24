"""Tests for blind_tdd.lint_tasks.

Covers parse_plan, each commit-time check in isolation, the preflight
bridge, enforcement modes (strict/warn/off), and the stale-completed
historical gate.

Run with:
    python -m blind_tdd.test_lint_tasks
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve()
_REPO = _HERE.parents[1]
sys.path.insert(0, str(_REPO))

from blind_tdd.lint_tasks import (  # noqa: E402
    LintFinding,
    _check_criterion_gaps,
    _check_duplicate_task_ids,
    _check_stale_completed,
    _check_surface_conflicts,
    _load_green_passed_task_ids,
    lint_plan_file,
    parse_plan,
)
from blind_tdd.preflight import MODE_OFF, MODE_STRICT, MODE_WARN  # noqa: E402


def _chdir(path: Path):
    class _Ctx:
        def __enter__(self_):
            self_.prev = os.getcwd()
            os.chdir(path)
            return path

        def __exit__(self_, *a):
            os.chdir(self_.prev)
    return _Ctx()


def _write_plan(dir_: Path, tasks: list[dict], filename: str = "plan.md") -> Path:
    blob = {"tasks": tasks}
    p = dir_ / filename
    p.write_text(
        "# Plan\n\n```json\n" + json.dumps(blob, indent=2) + "\n```\n",
        encoding="utf-8",
    )
    return p


def _clean_task(id_: str = "t1") -> dict:
    return {
        "id": id_,
        "title": "clean task",
        "passes": False,
        "acceptance_criteria": [
            {"id": "AC-1", "given": "a", "when": "foo is called",
             "then": "foo returns True"},
        ],
        "public_surface": {"module": "src.foo", "adds": ["foo()"]},
    }


# ---------------------------------------------------------------------------
# parse_plan
# ---------------------------------------------------------------------------

def test_parse_plan_empty_file():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "empty.md"
        p.write_text("# no tasks here\n", encoding="utf-8")
        assert parse_plan(p) == []


def test_parse_plan_single_task():
    with tempfile.TemporaryDirectory() as td:
        p = _write_plan(Path(td), [_clean_task("t1")])
        tasks = parse_plan(p)
        assert len(tasks) == 1
        assert tasks[0]["id"] == "t1"


def test_parse_plan_multiple_tasks():
    with tempfile.TemporaryDirectory() as td:
        p = _write_plan(Path(td), [_clean_task("t1"), _clean_task("t2")])
        tasks = parse_plan(p)
        assert [t["id"] for t in tasks] == ["t1", "t2"]


def test_parse_plan_ignores_malformed_json():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "plan.md"
        p.write_text(
            "# Plan\n\n```json\n{not valid\n```\n\n"
            "```json\n" + json.dumps({"tasks": [_clean_task("t1")]}) + "\n```\n",
            encoding="utf-8",
        )
        tasks = parse_plan(p)
        assert len(tasks) == 1
        assert tasks[0]["id"] == "t1"


def test_parse_plan_missing_file():
    assert parse_plan(Path("/nonexistent/plan.md")) == []


# ---------------------------------------------------------------------------
# _check_criterion_gaps
# ---------------------------------------------------------------------------

def test_criterion_gaps_none():
    t = _clean_task()
    t["acceptance_criteria"] = [
        {"id": "AC-1", "given": "x", "when": "y", "then": "z"},
        {"id": "AC-2", "given": "x", "when": "y", "then": "z"},
    ]
    assert _check_criterion_gaps(t) == []


def test_criterion_gap_detected():
    t = _clean_task()
    t["acceptance_criteria"] = [
        {"id": "AC-1", "given": "x", "when": "y", "then": "z"},
        {"id": "AC-2", "given": "x", "when": "y", "then": "z"},
        {"id": "AC-4", "given": "x", "when": "y", "then": "z"},  # skip AC-3
    ]
    findings = _check_criterion_gaps(t)
    assert len(findings) == 1
    assert findings[0].kind == "gap"
    assert "AC-3" in findings[0].message


def test_criterion_gap_not_starting_at_1():
    """AC-2, AC-3 missing AC-1 → flagged."""
    t = _clean_task()
    t["acceptance_criteria"] = [
        {"id": "AC-2", "given": "x", "when": "y", "then": "z"},
        {"id": "AC-3", "given": "x", "when": "y", "then": "z"},
    ]
    findings = _check_criterion_gaps(t)
    assert len(findings) == 1
    assert "AC-1" in findings[0].message


def test_criterion_gap_non_ac_ids_ignored():
    """Criteria with non-AC-N IDs don't participate in gap detection."""
    t = _clean_task()
    t["acceptance_criteria"] = [
        {"id": "requirement-1", "given": "x", "when": "y", "then": "z"},
        {"id": "requirement-2", "given": "x", "when": "y", "then": "z"},
    ]
    assert _check_criterion_gaps(t) == []


# ---------------------------------------------------------------------------
# _check_duplicate_task_ids
# ---------------------------------------------------------------------------

def test_duplicate_task_ids_detected():
    tasks = [_clean_task("t1"), _clean_task("t1"), _clean_task("t2")]
    findings = _check_duplicate_task_ids(tasks)
    assert len(findings) == 1
    assert findings[0].task_id == "t1"
    assert "appears 2 times" in findings[0].message


def test_duplicate_task_ids_none():
    tasks = [_clean_task("t1"), _clean_task("t2"), _clean_task("t3")]
    assert _check_duplicate_task_ids(tasks) == []


def test_duplicate_task_ids_multiple_collisions():
    tasks = [_clean_task("t1"), _clean_task("t1"), _clean_task("t2"), _clean_task("t2")]
    findings = _check_duplicate_task_ids(tasks)
    assert len(findings) == 2


# ---------------------------------------------------------------------------
# _check_surface_conflicts
# ---------------------------------------------------------------------------

def test_surface_conflict_detected():
    t = _clean_task()
    t["public_surface"] = {
        "module": "src.foo",
        "adds": ["foo()", "bar()"],
        "modifies": ["bar()"],  # overlap
    }
    findings = _check_surface_conflicts(t)
    assert len(findings) == 1
    assert "bar()" in findings[0].message


def test_surface_conflict_none():
    t = _clean_task()
    t["public_surface"] = {
        "module": "src.foo",
        "adds": ["foo()"],
        "modifies": ["baz()"],
    }
    assert _check_surface_conflicts(t) == []


def test_surface_conflict_no_modifies():
    t = _clean_task()
    assert _check_surface_conflicts(t) == []


# ---------------------------------------------------------------------------
# _check_stale_completed + _load_green_passed_task_ids
# ---------------------------------------------------------------------------

def test_stale_completed_skipped_when_not_historical():
    t = _clean_task()
    t["passes"] = True
    findings = _check_stale_completed(
        t, completed_task_ids_with_green=set(), include_historical=False,
    )
    assert findings == []


def test_stale_completed_detected():
    t = _clean_task()
    t["passes"] = True
    findings = _check_stale_completed(
        t, completed_task_ids_with_green=set(), include_historical=True,
    )
    assert len(findings) == 1
    assert "never produced a blind_green_phase" in findings[0].message


def test_stale_completed_skipped_when_green_recorded():
    t = _clean_task("t7")
    t["passes"] = True
    findings = _check_stale_completed(
        t, completed_task_ids_with_green={"t7"}, include_historical=True,
    )
    assert findings == []


def test_stale_completed_skipped_for_open_tasks():
    t = _clean_task()
    t["passes"] = False  # still open
    findings = _check_stale_completed(
        t, completed_task_ids_with_green=set(), include_historical=True,
    )
    assert findings == []


def test_stale_completed_skipped_without_acceptance_criteria():
    t = _clean_task()
    t["passes"] = True
    del t["acceptance_criteria"]
    findings = _check_stale_completed(
        t, completed_task_ids_with_green=set(), include_historical=True,
    )
    assert findings == []


def test_load_green_passed_task_ids():
    with tempfile.TemporaryDirectory() as td:
        obs_path = Path(td) / "obs.jsonl"
        obs_path.write_text(
            json.dumps({"type": "blind_green_phase", "task": "t1", "green_pass": True}) + "\n"
            + json.dumps({"type": "blind_green_phase", "task": "t2", "green_pass": False}) + "\n"
            + json.dumps({"type": "blind_red_phase", "task": "t3", "red_pass": True}) + "\n"
            + json.dumps({"type": "blind_green_phase", "task": "t4", "green_pass": True}) + "\n",
            encoding="utf-8",
        )
        ids = _load_green_passed_task_ids(obs_path)
        assert ids == {"t1", "t4"}


# ---------------------------------------------------------------------------
# lint_plan_file — end-to-end
# ---------------------------------------------------------------------------

def test_lint_clean_plan_passes():
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            p = _write_plan(Path(td), [_clean_task("t1"), _clean_task("t2")])
            result = lint_plan_file(p)
            assert result.ok
            assert result.errors == []
            assert result.tasks_scanned == 2


def test_lint_detects_gap_and_duplicate():
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            t1 = _clean_task("same-id")
            t2 = _clean_task("same-id")  # duplicate
            t1["acceptance_criteria"] = [
                {"id": "AC-1", "given": "x", "when": "foo is called", "then": "foo returns True"},
                {"id": "AC-3", "given": "x", "when": "foo is called", "then": "foo returns False"},
            ]
            p = _write_plan(Path(td), [t1, t2])
            result = lint_plan_file(p)
            kinds = {f.kind for f in result.findings}
            assert "gap" in kinds
            assert "duplicate_task" in kinds


def test_lint_off_mode_returns_empty():
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            t = _clean_task()
            t["acceptance_criteria"][0]["then"] = "the thing feels good"  # subjective
            p = _write_plan(Path(td), [t])
            result = lint_plan_file(p, mode=MODE_OFF)
            assert result.ok
            assert result.findings == []


def test_lint_warn_mode_does_not_promote_to_errors():
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            t = _clean_task()
            t["acceptance_criteria"][0]["then"] = "the thing feels good"  # subjective
            p = _write_plan(Path(td), [t])
            result = lint_plan_file(p, mode=MODE_WARN)
            assert result.ok  # warnings don't fail
            assert len(result.warnings) >= 1
            assert result.errors == []


def test_lint_strict_mode_promotes_to_errors():
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            t = _clean_task()
            t["acceptance_criteria"][0]["then"] = "the thing feels good"
            p = _write_plan(Path(td), [t])
            result = lint_plan_file(p, mode=MODE_STRICT)
            assert not result.ok
            assert len(result.errors) >= 1
            assert result.warnings == []


def test_lint_no_tasks_is_ok():
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            p = Path(td) / "plan.md"
            p.write_text("# empty plan\n", encoding="utf-8")
            result = lint_plan_file(p)
            assert result.ok
            assert result.tasks_scanned == 0


def test_lint_preflight_findings_surface():
    """A task with a surface-coverage gap should generate a preflight finding,
    kinded by the check that raised it rather than a flat "preflight"."""
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            t = _clean_task()
            t["public_surface"]["adds"].append("Uncovered.method()")
            p = _write_plan(Path(td), [t])
            result = lint_plan_file(p)
            preflight_findings = [f for f in result.findings
                                  if f.kind.startswith("preflight_")]
            assert len(preflight_findings) >= 1
            assert any("Uncovered" in f.message or "method" in f.message
                       for f in preflight_findings)
            assert any(f.kind == "preflight_surface" for f in preflight_findings)
            assert not any(f.kind == "preflight" for f in result.findings)


def test_lint_preflight_kinds_vary_per_check():
    """TD195/AC-5 prerequisite. Every preflight finding used to be flattened
    to kind="preflight", contradicting LintFinding.kind's own docstring
    ("preflight_<name>") and making an undeclared-surface warning
    indistinguishable from a subjective-wording nit inside one 600-finding
    plan lint. A task tripping two DIFFERENT checks must produce two
    different kinds."""
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            t = _clean_task()
            # (1) surface check: an `adds` entry no criterion mentions.
            t["public_surface"]["adds"].append("Uncovered.method()")
            # (2) subjective check: a `then` clause full of feel words.
            t["acceptance_criteria"].append({
                "id": "AC-9",
                "given": "the thing exists",
                "when": "it is used",
                "then": "it feels smooth and nice",
            })
            p = _write_plan(Path(td), [t])
            result = lint_plan_file(p)
            kinds = {f.kind for f in result.findings if f.kind.startswith("preflight_")}
            assert "preflight_surface" in kinds
            assert "preflight_subjective" in kinds
            assert len(kinds) >= 2


def test_lint_historical_stale_check():
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            t = _clean_task("old-task")
            t["passes"] = True  # completed
            p = _write_plan(Path(td), [t])

            obs_path = Path(td) / "obs.jsonl"
            obs_path.write_text("", encoding="utf-8")  # empty history

            result = lint_plan_file(
                p, include_historical=True, observations_path=obs_path,
            )
            stales = [f for f in result.findings if f.kind == "stale"]
            assert len(stales) == 1
            assert "old-task" in stales[0].task_id


def test_lint_historical_stale_check_skipped_when_green_exists():
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            t = _clean_task("validated-task")
            t["passes"] = True
            p = _write_plan(Path(td), [t])

            obs_path = Path(td) / "obs.jsonl"
            obs_path.write_text(
                json.dumps({"type": "blind_green_phase", "task": "validated-task", "green_pass": True}) + "\n",
                encoding="utf-8",
            )

            result = lint_plan_file(
                p, include_historical=True, observations_path=obs_path,
            )
            stales = [f for f in result.findings if f.kind == "stale"]
            assert stales == []


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def _run_all() -> int:
    tests = [v for k, v in globals().items()
             if callable(v) and k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            import traceback
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
            traceback.print_exc()
        else:
            print(f"  PASS  {t.__name__}")
    print()
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(_run_all())
