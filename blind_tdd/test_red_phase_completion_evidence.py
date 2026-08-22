"""The red phase is closed by the RECORD it leaves, not by the writer's file.

`clear_red_state` deletes the seal on every green pass, so a finished task and
a task that never started look identical on disk: no `red_state/<task>.json`.
The gate read that absence as "start the red phase", re-entered it, found no
`triage/<task>.json`, and raised a yellow demanding a writer handshake for work
it had already verified.

Measured on 2026-08-21 against the real records in the in-the-loop-learning
repo, with the code as it stood at 26fa7a3:

    AC-6 TD189 (real green_report, no red_state)  -> manual_mode=True, "pending"
    AC-6 TD193 (real green_report, no red_state)  -> manual_mode=True, "pending"

Both had passed: 792 and 807 tests green against a sealed baseline. Both were
told to go run a test writer. That is the seventh recorded instance of this
false positive, and the reason it matters is in the ITL config's own note —
the previous six got blind TDD switched off wholesale on 2026-08-17.

What this file pins, and what it deliberately does NOT change:

  * A sealed red_state closes the red phase whether or not a triage file
    exists (AC-1) — already true before this change, never tested; pinned
    here so it cannot regress.
  * With neither a record nor a file, the check still fires (AC-2). The
    control is the point of the exercise.
  * The message names the ARTIFACT it looked for and never claims the writer
    did not run (AC-3) — satisfied by 3d6f465, pinned here against the real
    completed-task shape it was never measured on.
  * A triage file does not stand in for a sealed red phase (AC-4). It still
    closes the writer's own spawn, which is its job; what it cannot do is
    prove the phase was verified, because verification runs after it.
  * A completed task asks for nothing (AC-6), and that exemption is narrow
    (AC-7): wrong task, non-passing report, or malformed JSON all fall
    through to the red phase.

Every fixture below is real data. The green-report and red-state values are
transcribed verbatim from the files named in each provenance comment; nothing
here is an invented shape.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from blind_tdd.gate_integration import (
    completed_green_report,
    load_red_state,
)
from blind_tdd.orchestrator import ManualSpawner

TEST_DIRS = ["tests/contracts/", "tests/integration/"]

# Verbatim from D:/Projects/in-the-loop-learning/.themis/blind_tdd/green_report/
# TD189.json and TD193.json, read 2026-08-21. Those files also carry `measured`
# and `per_test` (~111KB of pytest detail); the fields below are the ones the
# completion check reads, copied unchanged.
_REAL_GREEN_REPORTS = {
    "TD189": {
        "task": "TD189", "overall": "pass", "phase": "green",
        "tests_passed": 792, "tests_failed": 0, "tests_skipped": 1,
    },
    "TD193": {
        "task": "TD193", "overall": "pass", "phase": "green",
        "tests_passed": 807, "tests_failed": 0, "tests_skipped": 1,
    },
}

# Verbatim from .../red_state/TD20.json, read 2026-08-21: a genuine sealed
# baseline (8 hashed test files, unsigned — THEMIS_SEAL_KEY was unset when it
# was written). Two of the eight hashes are reproduced; the seal check does not
# read this fixture, the branch decision does.
_REAL_RED_STATE_TD20 = {
    "task_id": "TD20",
    "timestamp": "2026-08-16T01:26:19.589266+00:00",
    "test_file_hashes": {
        "tests\\contracts\\test_google_auth.py":
            "e06fbf1eae8f241cd2e2ce0087468b2c67767fd7e817abf245feaa81bb5baa75",
    },
    "triage_report": {"bead": "TD20", "writer": "blind-test-writer", "triage": []},
    "spawn_agent_id": None,
}


def _blind_dir(root: Path) -> Path:
    d = root / ".themis" / "blind_tdd"
    for sub in ("red_state", "triage", "green_report", "pending"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    return d


def _write(path: Path, payload) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        payload if isinstance(payload, str) else json.dumps(payload, indent=2),
        encoding="utf-8",
    )
    return path


def _seal_red_state(root: Path, task_id: str, state: dict) -> Path:
    return _write(_blind_dir(root) / "red_state" / f"{task_id}.json", state)


def _green_report(root: Path, task_id: str, report: dict) -> Path:
    return _write(_blind_dir(root) / "green_report" / f"{task_id}.json", report)


def _triage(root: Path, task_id: str) -> Path:
    return _write(
        _blind_dir(root) / "triage" / f"{task_id}.json",
        {"task": task_id, "triage": [{"criterion": "AC-1", "status": "tested"}]},
    )


def _brief(root: Path, task_id: str) -> Path:
    return _write(
        _blind_dir(root) / "pending" / f"test_writer-{task_id}.md",
        "# blind-TDD brief\n",
    )


def _spawn(task_id: str) -> dict:
    """The spawner call the red phase makes — what the gate turns into a yellow."""
    return ManualSpawner().spawn(
        role="test_writer",
        prompt_template="",
        inputs={"task_id": task_id, "test_dirs": TEST_DIRS},
    )


# ---------------------------------------------------------------------------
# AC-1 — a sealed record closes the red phase; the writer's file is not asked for
# ---------------------------------------------------------------------------

def test_ac1_sealed_red_state_with_no_triage_file_raises_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Under `spawner: manual` the orchestrator seals red directly and no
    triage file is ever written. The seal is the evidence."""
    monkeypatch.chdir(tmp_path)
    _blind_dir(tmp_path)
    _seal_red_state(tmp_path, "TD20", _REAL_RED_STATE_TD20)

    assert not (tmp_path / ".themis/blind_tdd/triage/TD20.json").exists()
    # A loadable record means the gate takes the green branch and never
    # constructs the spawner that would raise.
    assert load_red_state("TD20") is not None


def test_ac1_a_sealed_record_carrying_no_triage_report_at_all_still_closes_red(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The seal need not embed a triage report either — TD189/TD193 were
    sealed with none, which is exactly the manual-spawner shape."""
    monkeypatch.chdir(tmp_path)
    _blind_dir(tmp_path)
    state = dict(_REAL_RED_STATE_TD20)
    state.pop("triage_report")
    _seal_red_state(tmp_path, "TD20", state)

    assert load_red_state("TD20") is not None


# ---------------------------------------------------------------------------
# AC-2 — the control: no record and no file must still fire
# ---------------------------------------------------------------------------

def test_ac2_no_red_state_and_no_triage_still_fails_and_still_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The true positive this whole check exists for. If this ever passes
    silently, the control is gone and nothing will complain."""
    monkeypatch.chdir(tmp_path)
    _blind_dir(tmp_path)
    _brief(tmp_path, "TD189")

    assert load_red_state("TD189") is None
    assert completed_green_report("TD189") is None

    result = _spawn("TD189")
    assert result["manual_mode"] is True
    assert result["manual_state"] == "pending"
    assert result["success"] is False


# ---------------------------------------------------------------------------
# AC-3 — the message names the artifact, and claims nothing about the writer
# ---------------------------------------------------------------------------

def test_ac3_message_reports_the_absent_artifact_not_an_absent_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The check can establish that a file is not there. It cannot establish
    that nobody ran, and must not say so."""
    monkeypatch.chdir(tmp_path)
    _blind_dir(tmp_path)

    message = _spawn("TD189")["message"]

    assert "TD189.json" in message
    assert "NOT FOUND" in message
    assert "looked for:" in message
    assert "searched:" in message
    for claim in ("has not run", "did not run", "never ran", "no writer"):
        assert claim not in message.lower(), f"message asserts {claim!r}"


# ---------------------------------------------------------------------------
# AC-4 — a triage file is not a sealed red phase
# ---------------------------------------------------------------------------

def test_ac4_triage_file_alone_does_not_close_the_red_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The file closes the writer's spawn — that is its job, and coverage
    verification runs after it. What it must never do is stand in for the
    sealed record, because the record is written only once the phase has
    actually been verified."""
    monkeypatch.chdir(tmp_path)
    _blind_dir(tmp_path)
    _triage(tmp_path, "TD20")

    assert load_red_state("TD20") is None, (
        "a triage file must not be readable as a sealed red phase"
    )
    assert completed_green_report("TD20") is None


def test_ac4_triage_file_does_not_exempt_a_task_from_the_red_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Presence of the record, not the file, is what the gate branches on."""
    monkeypatch.chdir(tmp_path)
    _blind_dir(tmp_path)
    _triage(tmp_path, "TD20")
    _green_report(tmp_path, "TD20", {"task": "TD20", "overall": "fail"})

    assert completed_green_report("TD20") is None


# ---------------------------------------------------------------------------
# AC-6 — a completed task asks for nothing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("task_id", sorted(_REAL_GREEN_REPORTS))
def test_ac6_completed_task_needs_no_red_handshake(
    task_id, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """TD189 and TD193 as they actually sit on disk: a passing green report,
    the red_state consumed by `clear_red_state`, and the writer brief still
    staged in pending/. Before this change both raised a tier-2 yellow."""
    monkeypatch.chdir(tmp_path)
    _blind_dir(tmp_path)
    _green_report(tmp_path, task_id, _REAL_GREEN_REPORTS[task_id])
    _brief(tmp_path, task_id)

    assert load_red_state(task_id) is None, "the seal is cleared on green pass"

    done = completed_green_report(task_id)
    assert done is not None, (
        f"{task_id} passed with {_REAL_GREEN_REPORTS[task_id]['tests_passed']} "
        f"tests and must not be asked to re-run its red phase"
    )
    assert done["tests_passed"] == _REAL_GREEN_REPORTS[task_id]["tests_passed"]


# ---------------------------------------------------------------------------
# AC-7 — and that exemption is not a blanket one
# ---------------------------------------------------------------------------

def test_ac7_green_report_for_a_different_task_exempts_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """TD189's report is filed under TD193's name. The `task` field disagrees,
    so it proves nothing about TD193 and the red phase proceeds."""
    monkeypatch.chdir(tmp_path)
    _blind_dir(tmp_path)
    _green_report(tmp_path, "TD193", _REAL_GREEN_REPORTS["TD189"])

    assert completed_green_report("TD193") is None
    assert _spawn("TD193")["manual_mode"] is True


def test_ac7_a_failing_green_report_is_not_a_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A report that recorded a failure is evidence the task did NOT finish."""
    monkeypatch.chdir(tmp_path)
    _blind_dir(tmp_path)
    _green_report(tmp_path, "TD189", {
        **_REAL_GREEN_REPORTS["TD189"],
        "overall": "fail", "tests_failed": 3,
    })

    assert completed_green_report("TD189") is None
    assert _spawn("TD189")["manual_mode"] is True


def test_ac7_an_unverified_task_is_still_verified_when_a_sibling_completed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The distinction the criterion is really about: TD189 finishing must not
    buy TD196 a pass. A green report is evidence about the red phase it came
    from, never about one that never happened."""
    monkeypatch.chdir(tmp_path)
    _blind_dir(tmp_path)
    _green_report(tmp_path, "TD189", _REAL_GREEN_REPORTS["TD189"])
    _brief(tmp_path, "TD196")

    assert completed_green_report("TD189") is not None
    assert completed_green_report("TD196") is None
    assert _spawn("TD196")["manual_mode"] is True


@pytest.mark.parametrize("body", ["{ not json", "[]", '"a string"', "null"])
def test_ac7_a_malformed_green_report_falls_through_to_the_red_phase(
    body, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Unreadable is not the same as passing. The safe direction is to verify."""
    monkeypatch.chdir(tmp_path)
    _blind_dir(tmp_path)
    _write(tmp_path / ".themis/blind_tdd/green_report/TD189.json", body)

    assert completed_green_report("TD189") is None
