"""Tests for blind_tdd.roots — deriving the blindness seal's denied roots
from the project's own config instead of a hardcoded JS-shaped default.

The regression this closes: a Python project laid out as server/, core/,
mastery_core/ (no src/ at all) used to get session._DEFAULT_BLOCKED
verbatim — a deny-list that matched nothing real. These tests pin:

  - JS-family languages keep the conventional src/**+examples/** deny-list
    (mod-my-week-platform must not regress).
  - Non-JS languages (Python) get roots derived by scanning the project for
    directories that actually contain source files, excluding the
    configured test directory.
  - Ambiguous config (no language, unknown language, or a scan that finds
    nothing) raises RootInferenceError rather than returning an empty or
    partial deny-list.
  - The end-to-end claim: a session sealed with the derived roots actually
    causes the real path-guard hook to refuse a read of a Python
    implementation file.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from blind_tdd.roots import RootInferenceError, derive_blocked_paths

_REPO = Path(__file__).resolve().parents[1]
_GUARD_PATH = _REPO / "templates" / "hooks" / "blind_tdd_path_guard.py"


def _load_guard():
    spec = importlib.util.spec_from_file_location("_bt_path_guard_roots_test", _GUARD_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_GUARD = _load_guard()


# ---------------------------------------------------------------------------
# JS convention
# ---------------------------------------------------------------------------

def test_js_language_keeps_conventional_src_and_examples():
    config = {"stack": {"languages": ["node"]}}
    sealed = derive_blocked_paths(config)
    assert "src/**" in sealed.blocked_paths
    assert "examples/**" in sealed.blocked_paths
    assert ".git/**" in sealed.blocked_paths
    assert "data/generated/**" in sealed.blocked_paths
    assert sealed.method == "js-convention"
    assert sorted(sealed.roots) == ["examples", "src"]


def test_js_convention_applies_even_when_test_dir_points_at_src(tmp_path):
    """mod-my-week-platform: stack.test_runner.node.test_dir is "src" itself
    (co-located tests) — the JS convention must still seal all of src/,
    not exclude it because it's also "the test dir"."""
    config = {
        "stack": {
            "languages": ["node"],
            "test_runner": {"node": {"command": "npx vitest run", "test_dir": "src"}},
        },
    }
    sealed = derive_blocked_paths(config, tmp_path)
    assert "src/**" in sealed.blocked_paths


def test_languages_inferred_from_test_runner_keys_when_absent():
    """Some configs (probe sandboxes, older projects) declare
    stack.test_runner.<lang> without an explicit stack.languages array."""
    config = {"stack": {"test_runner": {"node": {"test_dir": "src"}}}}
    sealed = derive_blocked_paths(config)
    assert sealed.languages == ["node"]
    assert "src/**" in sealed.blocked_paths


# ---------------------------------------------------------------------------
# Python (scanned) — the regression this bead closes
# ---------------------------------------------------------------------------

def _make_python_project(root: Path) -> None:
    """Lay out a project shaped like in-the-loop-learning: no src/, real
    implementation under server/, core/, mastery_core/, bridge/."""
    for pkg in ("server", "core", "mastery_core", "bridge"):
        d = root / pkg
        d.mkdir()
        (d / "__init__.py").write_text("", encoding="utf-8")
        (d / f"{pkg}_module.py").write_text("x = 1\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_x.py").write_text("def test_x(): pass\n", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "notes.md").write_text("# notes\n", encoding="utf-8")
    (root / ".git").mkdir()


def _python_config() -> dict:
    return {
        "stack": {
            "languages": ["python"],
            "test_runner": {
                "python": {"command": "python -m pytest", "test_dir": "tests/"},
            },
        },
    }


def test_python_project_seals_its_real_source_dirs_not_src(tmp_path):
    _make_python_project(tmp_path)
    sealed = derive_blocked_paths(_python_config(), tmp_path)

    for pkg in ("server", "core", "mastery_core", "bridge"):
        assert f"{pkg}/**" in sealed.blocked_paths, sealed.blocked_paths

    # The JS-shaped default this replaces must not silently persist.
    assert "src/**" not in sealed.blocked_paths
    assert "examples/**" not in sealed.blocked_paths
    assert sealed.method == "language-scan"


def test_python_project_does_not_seal_tests_or_docs(tmp_path):
    _make_python_project(tmp_path)
    sealed = derive_blocked_paths(_python_config(), tmp_path)
    assert "tests/**" not in sealed.blocked_paths
    assert "docs/**" not in sealed.blocked_paths


def test_python_project_scan_excludes_blind_tdd_test_dirs(tmp_path):
    """gate.blind_tdd.test_dirs (the writer's OWN test output location) must
    also be excluded from the scan, even if it differs from
    stack.test_runner.python.test_dir."""
    _make_python_project(tmp_path)
    config = _python_config()
    config["gate"] = {"blind_tdd": {"test_dirs": ["tests/contracts/", "tests/integration/"]}}
    sealed = derive_blocked_paths(config, tmp_path)
    assert "tests/**" not in sealed.blocked_paths


def test_mixed_languages_union_js_convention_and_scan(tmp_path):
    _make_python_project(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "widget.js").write_text("export const x = 1;\n", encoding="utf-8")
    config = {
        "stack": {
            "languages": ["python", "node"],
            "test_runner": {"python": {"test_dir": "tests/"}},
        },
    }
    sealed = derive_blocked_paths(config, tmp_path)
    assert "src/**" in sealed.blocked_paths
    assert "server/**" in sealed.blocked_paths
    assert "tests/**" not in sealed.blocked_paths
    assert sealed.method == "js-convention+language-scan"


# ---------------------------------------------------------------------------
# Fail loud, not empty
# ---------------------------------------------------------------------------

def test_no_language_declared_fails_loud():
    with pytest.raises(RootInferenceError):
        derive_blocked_paths({})


def test_unknown_language_fails_loud(tmp_path):
    _make_python_project(tmp_path)
    config = {"stack": {"languages": ["cobol"]}}
    with pytest.raises(RootInferenceError):
        derive_blocked_paths(config, tmp_path)


def test_scan_finding_nothing_fails_loud(tmp_path):
    """An empty project (or one where everything is under the test dir) must
    not silently produce a deny-list that matches nothing."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def test_x(): pass\n", encoding="utf-8")
    with pytest.raises(RootInferenceError):
        derive_blocked_paths(_python_config(), tmp_path)


def test_fail_loud_message_names_the_actual_problem(tmp_path):
    """The error should point at what's missing, not just say 'failed'."""
    with pytest.raises(RootInferenceError, match="stack.languages"):
        derive_blocked_paths({})


# ---------------------------------------------------------------------------
# End-to-end: the derived seal actually blocks the real hook
# (AC1 — "this is the bead; everything else is secondary")
# ---------------------------------------------------------------------------

def test_derived_seal_refuses_blind_writer_reading_python_implementation(tmp_path):
    _make_python_project(tmp_path)
    sealed = derive_blocked_paths(_python_config(), tmp_path)

    session = {
        "session_id": "s1",
        "agent_role": "test_writer",
        "allowed_paths": [],  # blacklist-only mode: blocked_paths is the sole
                               # defense here, which is exactly the scenario
                               # where the old JS-shaped default was live-dangerous.
        "blocked_paths": sealed.blocked_paths,
    }
    allowed, reason = _GUARD._check_path("server/unit_store.py", session)
    assert allowed is False, reason
    assert "blocked pattern" in reason

    allowed, reason = _GUARD._check_path("core/compliance.py", session)
    assert allowed is False, reason


def test_old_js_shaped_default_would_have_missed_it(tmp_path):
    """Documents the regression: the previous hardcoded default really did
    let this through in blacklist-only mode."""
    from blind_tdd.session import default_blocked_paths

    session = {
        "session_id": "s1",
        "agent_role": "test_writer",
        "allowed_paths": [],
        "blocked_paths": default_blocked_paths(),
    }
    allowed, _ = _GUARD._check_path("server/unit_store.py", session)
    assert allowed is True  # the bug: nothing in the JS-shaped list matches


# ---------------------------------------------------------------------------
# to_record() — the auditable artifact
# ---------------------------------------------------------------------------

def test_to_record_is_json_serializable_and_complete(tmp_path):
    _make_python_project(tmp_path)
    sealed = derive_blocked_paths(_python_config(), tmp_path)
    record = sealed.to_record()
    json.dumps(record)  # must not raise
    assert set(record) == {"blocked_paths", "roots", "languages", "method"}
    assert record["languages"] == ["python"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
