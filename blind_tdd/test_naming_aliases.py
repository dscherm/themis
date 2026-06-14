"""Locks the THEMIS_* rename and the RALPH_* back-compat aliases.

Preferred names: THEMIS_TASK / THEMIS_HOME env, themis_home config key + ctor
param. Legacy names (RALPH_BLIND_TDD_TASK / RALPH_HOME / ralph_home) must keep
working as silent fallbacks.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[1]))

from blind_tdd import gate_integration as gi  # noqa: E402
from blind_tdd.spawners.claude_code_spawner import ClaudeCodeSpawner  # noqa: E402

VALID_TASK = {
    "id": "task-test-1",
    "passes": False,
    "public_surface": {"module": "src.x", "adds": ["x.f(a)"]},
    "acceptance_criteria": [
        {"id": "AC-1", "given": "g", "when": "f(a) called", "then": "returns 3"}
    ],
}


@contextmanager
def _chdir(d):
    prev = os.getcwd()
    os.chdir(d)
    try:
        yield
    finally:
        os.chdir(prev)


def _write_plan(d: Path, tasks):
    block = "\n".join("```json\n" + json.dumps(t) + "\n```" for t in tasks)
    (d / "plan.md").write_text(block, encoding="utf-8")


def _clear_task_env():
    for k in ("THEMIS_TASK", "RALPH_BLIND_TDD_TASK"):
        os.environ.pop(k, None)


# --- config key ---

def test_config_themis_home_key():
    cfg = gi.get_blind_tdd_config({"gate": {"blind_tdd": {"themis_home": "/x"}}})
    assert cfg["themis_home"] == "/x"


def test_config_ralph_home_legacy_alias():
    cfg = gi.get_blind_tdd_config({"gate": {"blind_tdd": {"ralph_home": "/legacy"}}})
    assert cfg["themis_home"] == "/legacy"


# --- env var task resolution ---

def test_env_themis_task():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td)
        _write_plan(p, [VALID_TASK])
        with _chdir(p):
            _clear_task_env()
            os.environ["THEMIS_TASK"] = "task-test-1"
            try:
                task, source = gi.load_current_task({})
                assert task is not None and task["id"] == "task-test-1"
                assert "THEMIS_TASK" in source
            finally:
                _clear_task_env()


def test_env_ralph_task_legacy():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td)
        _write_plan(p, [VALID_TASK])
        with _chdir(p):
            _clear_task_env()
            os.environ["RALPH_BLIND_TDD_TASK"] = "task-test-1"
            try:
                task, source = gi.load_current_task({})
                assert task is not None and task["id"] == "task-test-1"
                assert "RALPH_BLIND_TDD_TASK" in source
            finally:
                _clear_task_env()


def test_themis_task_takes_precedence_over_legacy():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td)
        _write_plan(p, [VALID_TASK])
        with _chdir(p):
            _clear_task_env()
            os.environ["THEMIS_TASK"] = "task-test-1"
            os.environ["RALPH_BLIND_TDD_TASK"] = "nonexistent"
            try:
                task, source = gi.load_current_task({})
                assert task is not None and task["id"] == "task-test-1"
                assert "THEMIS_TASK" in source
            finally:
                _clear_task_env()


# --- spawner constructor ---

def test_spawner_themis_home_param():
    s = ClaudeCodeSpawner(themis_home="/opt/themis")
    assert str(s.themis_home) == str(Path("/opt/themis"))


def test_spawner_ralph_home_legacy_param():
    s = ClaudeCodeSpawner(ralph_home="/opt/legacy")
    assert str(s.themis_home) == str(Path("/opt/legacy"))


def test_spawner_themis_home_wins_over_legacy():
    s = ClaudeCodeSpawner(themis_home="/opt/new", ralph_home="/opt/old")
    assert str(s.themis_home) == str(Path("/opt/new"))
