"""Install-behavior integration test for the blind-tdd plugin (BT4 AC-1, core).

AC-1 in full requires an interactive `/plugin install` and a human eye on a live
session; that part is verified by hand (see README "60-second quickstart"). What
*is* machine-checkable — and the load-bearing claim — is that once a blind
session is active, the plugin's shipped `PreToolUse` path-guard hook **blocks an
implementation read by the writer**, and that with no session it is pure
passthrough. This test drives the assembled hook exactly as Claude Code would:
the hook script as a subprocess, the PreToolUse JSON on stdin, cwd = project root.

Run: python -m pytest plugin/blind-tdd/test_plugin_install.py -q
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_PLUGIN_DIR = _HERE.parent
_PATH_GUARD = _PLUGIN_DIR / "hooks" / "blind_tdd_path_guard.py"


def _run_hook(project: Path, tool_name: str, tool_input: dict) -> subprocess.CompletedProcess:
    """Invoke the shipped path-guard hook as Claude Code does (stdin JSON, cwd=project)."""
    payload = json.dumps({"tool_name": tool_name, "tool_input": tool_input})
    return subprocess.run(
        [sys.executable, str(_PATH_GUARD)],
        input=payload,
        cwd=str(project),
        capture_output=True,
        text=True,
    )


def _open_blind_session(project: Path, role: str = "test_writer") -> None:
    session_dir = project / ".themis" / "blind_tdd"
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "active_session.json").write_text(
        json.dumps({
            "session_id": "blind-writer-test",
            "agent_role": role,
            "task_id": "AC1-smoke",
            "allowed_paths": ["tests/**", "public_api.md", "plan.md"],
            "blocked_paths": ["src/**", "examples/**", ".git/**"],
        }),
        encoding="utf-8",
    )


def test_path_guard_ships_in_assembled_plugin():
    assert _PATH_GUARD.exists(), "run assemble.py — path-guard hook is not assembled"


def test_blind_session_blocks_implementation_read(tmp_path):
    """With a blind writer session active, a Read of src/ is blocked (exit 2)."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "calculator.py").write_text("def add(a, b): return a + b\n")
    _open_blind_session(tmp_path, role="test_writer")

    result = _run_hook(tmp_path, "Read", {"file_path": "src/calculator.py"})

    assert result.returncode == 2, (
        f"expected the writer's implementation read to be BLOCKED (exit 2), "
        f"got {result.returncode}. stderr: {result.stderr!r}"
    )
    assert "BLOCKED" in result.stderr


def test_blind_session_allows_spec_and_test_reads(tmp_path):
    """The writer may still read its allowed surface (tests, public_api.md)."""
    _open_blind_session(tmp_path, role="test_writer")
    (tmp_path / "public_api.md").write_text("# API\n")

    allowed = _run_hook(tmp_path, "Read", {"file_path": "public_api.md"})
    assert allowed.returncode == 0, f"public_api.md read should be allowed; stderr: {allowed.stderr!r}"


def test_passthrough_when_no_session(tmp_path):
    """No active session => the always-on hook allows everything (safe default)."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "calculator.py").write_text("x = 1\n")

    result = _run_hook(tmp_path, "Read", {"file_path": "src/calculator.py"})
    assert result.returncode == 0, (
        f"with no .themis/blind_tdd/active_session.json the hook must passthrough; "
        f"got exit {result.returncode}, stderr: {result.stderr!r}"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
