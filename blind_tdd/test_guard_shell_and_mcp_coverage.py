"""A blind session must govern every tool that can read the implementation.

The guards only see the tools their matchers name. Registered on the built-in
file tools and on `Bash` alone, they let the PowerShell tool and any MCP file
tool (desktop-commander's `read_file`, `start_process`) reach `src/` with a
session armed. Found 2026-10-24 dispatching a blind writer.

Covers both halves: the hooks (PowerShell gets the shell guard's
deny-by-default policy, the runner keeps its test-runner allowance; every
`mcp__*` call is denied for every role while armed; no session => passthrough)
and every matcher Themis authors (plugin hooks.json, the per-role settings
templates, the Agent SDK spawner).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_HOOKS = _REPO / "templates" / "hooks"
_PATH_GUARD = _HOOKS / "blind_tdd_path_guard.py"
_BASH_GUARD = _HOOKS / "blind_tdd_bash_guard.py"

ROLES = ["test_writer", "test_runner", "arbiter"]
MCP_CALLS = [
    ("mcp__plugin_desktop-commander_desktop-commander__read_file",
     {"path": "src/thing.py"}),
    ("mcp__plugin_desktop-commander_desktop-commander__start_process",
     {"command": "type src\\thing.py", "timeout_ms": 5000}),
]
MCP_NAME = MCP_CALLS[0][0]


@pytest.fixture
def project(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "thing.py").write_text("SECRET = 1\n", encoding="utf-8")
    return tmp_path


def _arm(project: Path, role: str) -> None:
    d = project / ".themis" / "blind_tdd"
    d.mkdir(parents=True, exist_ok=True)
    (d / "active_session.json").write_text(json.dumps({
        "session_id": f"s-{role}", "agent_role": role,
        "allowed_paths": ["tests/**"], "blocked_paths": ["src/**"],
        "impl_paths": ["src/thing.py"],
    }), encoding="utf-8")


def _run(hook: Path, project: Path, tool_name: str, tool_input: dict) -> int:
    res = subprocess.run(
        [sys.executable, str(hook)],
        input=json.dumps({"tool_name": tool_name, "tool_input": tool_input,
                          "cwd": str(project)}),
        capture_output=True, text=True, cwd=str(project), timeout=30,
        encoding="utf-8", errors="replace",
    )
    return res.returncode


@pytest.mark.parametrize("role", ROLES)
def test_powershell_read_is_denied_for_every_role(role, project):
    _arm(project, role)
    assert _run(_BASH_GUARD, project, "PowerShell",
                {"command": "Get-Content src/thing.py"}) == 2


def test_powershell_runner_keeps_test_allowance(project):
    _arm(project, "test_runner")
    assert _run(_BASH_GUARD, project, "PowerShell",
                {"command": "python -m pytest tests/ -q"}) == 0


@pytest.mark.parametrize("command", [
    "pytest (Get-Content src/thing.py)",
    "pytest @(gc src/thing.py)",
    "pytest ${C:/x/src/thing.py}",
    "pytest tests/; gc src/thing.py",
])
def test_powershell_cannot_smuggle_a_read(command, project):
    _arm(project, "test_runner")
    assert _run(_BASH_GUARD, project, "PowerShell", {"command": command}) == 2


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("tool_name,tool_input", MCP_CALLS)
def test_mcp_is_denied_for_every_role(role, tool_name, tool_input, project):
    _arm(project, role)
    assert _run(_PATH_GUARD, project, tool_name, tool_input) == 2


def test_powershell_passes_through_with_no_session(project):
    assert _run(_BASH_GUARD, project, "PowerShell",
                {"command": "Get-Content src/thing.py"}) == 0


@pytest.mark.parametrize("tool_name,tool_input", MCP_CALLS)
def test_mcp_passes_through_with_no_session(tool_name, tool_input, project):
    assert _run(_PATH_GUARD, project, tool_name, tool_input) == 0


# ------------------------------ wiring ------------------------------

def _routes(pre_tool_use: list, script: str, tool_name: str) -> bool:
    for entry in pre_tool_use:
        if script not in json.dumps(entry):
            continue
        m = entry.get("matcher", "")
        if m in ("*", "") or re.fullmatch(m, tool_name):
            return True
    return False


def test_plugin_hooks_json_routes_powershell_and_mcp():
    d = json.loads((_REPO / "plugin" / "blind-tdd" / "hooks" / "hooks.json")
                   .read_text(encoding="utf-8"))
    pre = d["hooks"]["PreToolUse"]
    assert _routes(pre, "blind_tdd_bash_guard.py", "PowerShell")
    assert _routes(pre, "blind_tdd_bash_guard.py", "Bash")
    assert _routes(pre, "blind_tdd_path_guard.py", MCP_NAME)


def test_plugin_hook_copies_match_templates():
    for name in ("blind_tdd_path_guard.py", "blind_tdd_bash_guard.py"):
        assert ((_REPO / "plugin" / "blind-tdd" / "hooks" / name).read_bytes()
                == (_HOOKS / name).read_bytes()), name


@pytest.mark.parametrize("role", ["writer", "runner", "arbiter"])
def test_role_settings_route_mcp_and_close_powershell(role):
    s = json.loads((_REPO / "templates" / "blind_tdd" / f"settings.blind-{role}.json")
                   .read_text(encoding="utf-8"))
    pre = s["hooks"]["PreToolUse"]
    assert _routes(pre, "blind_tdd_path_guard.py", MCP_NAME)
    if role == "runner":
        assert _routes(pre, "blind_tdd_bash_guard.py", "PowerShell")
    else:
        assert "PowerShell" in s["permissions"]["deny"]


def test_sdk_spawner_hook_config_routes_shells_and_mcp(tmp_path):
    from blind_tdd.spawners.agent_sdk_spawner import AgentSdkSpawner
    spawner = AgentSdkSpawner(sdk=object(), project_root=tmp_path,
                              themis_home=tmp_path, install_hooks=True)
    pre = spawner._build_hook_config()["PreToolUse"]
    assert _routes(pre, "blind_tdd_bash_guard.py", "PowerShell")
    assert _routes(pre, "blind_tdd_bash_guard.py", "Bash")
    assert _routes(pre, "blind_tdd_path_guard.py", MCP_NAME)
    assert _routes(pre, "blind_tdd_path_guard.py", "Read")
