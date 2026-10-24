"""Tests that the orchestrator builds each blind role's brief correctly:
real denied roots reach the session, and the task's `steps` field — which
names files for the IMPLEMENTING agent to read, not the blind writer — never
reaches the spawned agent's context.

Exercises the real BlindTddOrchestrator (not the gate_integration stub used
in test_gate_integration.py) with a fake spawner that records exactly what
it was handed, so these assertions are against the actual code path a live
spawn takes, not a mock of it.

Run with:
    python -m pytest blind_tdd/test_blind_brief_composition.py -q
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from blind_tdd.orchestrator import BlindTddOrchestrator
from blind_tdd.session import current_session


def _write_prompts(home: Path) -> Path:
    d = home / "templates" / "blind_tdd" / "prompts"
    d.mkdir(parents=True, exist_ok=True)
    for name in ("test_writer.md", "test_runner.md", "arbiter.md"):
        (d / name).write_text(f"# Stub {name}\n", encoding="utf-8")
    return d


TASK_WITH_STEPS = {
    "id": "task-steps-1",
    "acceptance_criteria": [
        {"id": "AC-1", "given": "a", "when": "b", "then": "c"},
    ],
    "public_surface": {"module": "server.unit_store", "adds": ["do_thing"]},
    # The implementing-agent-only guidance the bug hands to the blind writer
    # verbatim today.
    "steps": [
        "read server/unit_store.py",
        "read core/compliance.py",
        "python -m pytest tests/ -q",
    ],
}


class _RecordingSpawner:
    """Fake spawner that records its inputs and the session active at spawn
    time, then produces the minimum output files each role needs."""

    def __init__(self):
        self.captured_inputs: dict | None = None
        self.captured_session: dict | None = None

    def spawn(self, *, role, prompt_template, inputs):
        self.captured_inputs = inputs
        self.captured_session = current_session()
        task_id = inputs["task_id"]
        if role == "test_writer":
            Path("tests/contracts").mkdir(parents=True, exist_ok=True)
            Path("tests/contracts/test_ac1.py").write_text(
                'def test_ac1():\n    """Covers: AC-1"""\n    assert 1 == 1\n',
                encoding="utf-8",
            )
            Path(".themis/blind_tdd/triage").mkdir(parents=True, exist_ok=True)
            Path(f".themis/blind_tdd/triage/{task_id}.json").write_text(
                json.dumps({"task": task_id, "triage": [],
                            "criteria_tested": ["AC-1"]}),
                encoding="utf-8",
            )
        return {"success": True, "agent_id": f"{role}-1"}


def _orchestrator(spawner, blocked_paths=None, sealed_roots_meta=None, home=None) -> BlindTddOrchestrator:
    base = home or Path.cwd()
    d = _write_prompts(base)
    return BlindTddOrchestrator(
        spawner=spawner,
        writer_prompt_path=str(d / "test_writer.md"),
        runner_prompt_path=str(d / "test_runner.md"),
        arbiter_prompt_path=str(d / "arbiter.md"),
        blocked_paths=blocked_paths,
        sealed_roots_meta=sealed_roots_meta,
    )


def test_steps_field_stripped_from_writer_brief(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    spawner = _RecordingSpawner()
    orch = _orchestrator(spawner)

    result = orch.run_red_phase(TASK_WITH_STEPS)

    assert result.passed, result.reason
    assert spawner.captured_inputs is not None
    briefed_task = spawner.captured_inputs["task"]
    assert "steps" not in briefed_task
    # Everything else the writer legitimately needs must survive.
    assert briefed_task["acceptance_criteria"] == TASK_WITH_STEPS["acceptance_criteria"]
    assert briefed_task["public_surface"] == TASK_WITH_STEPS["public_surface"]


def test_steps_field_stripped_but_original_task_dict_untouched(tmp_path, monkeypatch):
    """Sanitizing the brief must not mutate the caller's task object — the
    orchestrator's own coverage/hash checks downstream still need it whole."""
    monkeypatch.chdir(tmp_path)
    original = json.loads(json.dumps(TASK_WITH_STEPS))  # defensive copy
    spawner = _RecordingSpawner()
    orch = _orchestrator(spawner)

    orch.run_red_phase(original)

    assert original["steps"] == TASK_WITH_STEPS["steps"]  # untouched


def test_task_without_steps_passes_through_unchanged(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    task = {k: v for k, v in TASK_WITH_STEPS.items() if k != "steps"}
    spawner = _RecordingSpawner()
    orch = _orchestrator(spawner)

    orch.run_red_phase(task)

    assert spawner.captured_inputs["task"] == task


def test_derived_blocked_paths_reach_the_live_session(tmp_path, monkeypatch):
    """The orchestrator's blocked_paths (as would be derived by
    blind_tdd.roots.derive_blocked_paths for a real project) must be exactly
    what the active session — and therefore the path-guard hook — sees."""
    monkeypatch.chdir(tmp_path)
    derived = ["server/**", "core/**", "mastery_core/**", "bridge/**", ".git/**"]
    spawner = _RecordingSpawner()
    orch = _orchestrator(spawner, blocked_paths=derived)

    task = {k: v for k, v in TASK_WITH_STEPS.items() if k != "steps"}
    orch.run_red_phase(task)

    assert spawner.captured_session is not None
    assert spawner.captured_session["blocked_paths"] == derived


def test_sealed_roots_recorded_on_red_phase_result(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    meta = {
        "blocked_paths": ["server/**", ".git/**"],
        "roots": ["server"],
        "languages": ["python"],
        "method": "language-scan",
    }
    spawner = _RecordingSpawner()
    orch = _orchestrator(spawner, blocked_paths=meta["blocked_paths"], sealed_roots_meta=meta)

    task = {k: v for k, v in TASK_WITH_STEPS.items() if k != "steps"}
    result = orch.run_red_phase(task)

    assert result.passed, result.reason
    assert result.sealed_roots == meta


def test_no_blocked_paths_falls_back_to_session_defaults(tmp_path, monkeypatch):
    """Constructing the orchestrator without blocked_paths (direct-API /
    legacy callers) must keep working exactly as before."""
    monkeypatch.chdir(tmp_path)
    spawner = _RecordingSpawner()
    orch = _orchestrator(spawner)  # blocked_paths=None

    task = {k: v for k, v in TASK_WITH_STEPS.items() if k != "steps"}
    result = orch.run_red_phase(task)

    assert result.passed, result.reason
    assert "src/**" in spawner.captured_session["blocked_paths"]
    assert result.sealed_roots == {}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
