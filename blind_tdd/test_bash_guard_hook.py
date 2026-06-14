"""Tests for templates/hooks/blind_tdd_bash_guard.py.

The in-session blind runner runs real Bash; this hook is what stops it from
using the shell to read implementation code. Invoked as a real subprocess with
fake stdin, like the path-guard test.

Covers:
- Passthrough when no active session
- Non-Bash tool passes through
- Runner: allowed test commands pass; inspection commands blocked
- Runner: chained `pytest && cat src` blocked (every segment must be allowed)
- Runner: command substitution blocked
- Writer / arbiter: all Bash blocked
- Corrupt session fails closed
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_REPO = _HERE.parents[1]
_HOOK = _REPO / "templates" / "hooks" / "blind_tdd_bash_guard.py"

# Import the pure evaluator directly for fast unit checks.
sys.path.insert(0, str(_REPO / "templates" / "hooks"))
import blind_tdd_bash_guard as guard  # noqa: E402


def _run(cwd: Path, tool_name: str, command: str) -> int:
    res = subprocess.run(
        [sys.executable, str(_HOOK)],
        input=json.dumps({"tool_name": tool_name, "tool_input": {"command": command}}),
        capture_output=True, text=True, cwd=str(cwd), timeout=30,
        encoding="utf-8", errors="replace",
    )
    return res.returncode


def _session(cwd: Path, role: str, *, corrupt: bool = False) -> None:
    d = cwd / ".themis" / "blind_tdd"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "active_session.json"
    if corrupt:
        f.write_text("{not json", encoding="utf-8")
    else:
        f.write_text(json.dumps({"session_id": "s1", "agent_role": role}), encoding="utf-8")


# --------------------------- pure evaluator ---------------------------

def test_eval_runner_allows_pytest():
    assert guard.evaluate("pytest -q", "test_runner")[0] is True
    assert guard.evaluate("python -m pytest tests/", "test_runner")[0] is True
    assert guard.evaluate("dotnet test", "test_runner")[0] is True


def test_eval_runner_blocks_inspection():
    assert guard.evaluate("cat src/foo.py", "test_runner")[0] is False
    assert guard.evaluate("grep secret src/", "test_runner")[0] is False
    assert guard.evaluate("ls -la src", "test_runner")[0] is False


def test_eval_runner_blocks_chained_read():
    assert guard.evaluate("pytest && cat src/foo.py", "test_runner")[0] is False
    assert guard.evaluate("pytest; head src/x.py", "test_runner")[0] is False


def test_eval_runner_blocks_command_substitution():
    assert guard.evaluate("pytest $(cat src/foo.py)", "test_runner")[0] is False
    assert guard.evaluate("pytest `cat src/foo.py`", "test_runner")[0] is False


def test_eval_writer_and_arbiter_block_all_bash():
    assert guard.evaluate("pytest", "test_writer")[0] is False
    assert guard.evaluate("pytest", "arbiter")[0] is False


def test_eval_unknown_role_fails_closed():
    assert guard.evaluate("pytest", "unknown")[0] is False


# --------------------------- subprocess / exit codes ---------------------------

def test_no_session_passthrough(tmp_path):
    assert _run(tmp_path, "Bash", "cat /etc/passwd") == 0


def test_non_bash_passthrough(tmp_path):
    _session(tmp_path, "test_runner")
    assert _run(tmp_path, "Read", "irrelevant") == 0


def test_runner_pytest_allowed(tmp_path):
    _session(tmp_path, "test_runner")
    assert _run(tmp_path, "Bash", "python -m pytest tests/contracts") == 0


def test_runner_cat_blocked(tmp_path):
    _session(tmp_path, "test_runner")
    assert _run(tmp_path, "Bash", "cat src/secret.py") == 2


def test_writer_bash_blocked(tmp_path):
    _session(tmp_path, "test_writer")
    assert _run(tmp_path, "Bash", "pytest") == 2


def test_corrupt_session_fails_closed(tmp_path):
    _session(tmp_path, "test_runner", corrupt=True)
    assert _run(tmp_path, "Bash", "pytest") == 2


def test_audit_written_on_block(tmp_path):
    _session(tmp_path, "test_runner")
    _run(tmp_path, "Bash", "cat src/secret.py")
    audit = tmp_path / ".themis" / "blind_audit" / "s1.jsonl"
    assert audit.exists()
    rec = json.loads(audit.read_text(encoding="utf-8").splitlines()[-1])
    assert rec["allowed"] is False and rec["tool_name"] == "Bash"
