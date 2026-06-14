"""Self-contained tests for AgentSdkSpawner.

Uses a mock `claude_agent_sdk` injected via the `sdk=` constructor
parameter so tests run without the real SDK installed. The mock
records what options the spawner passed and simulates the async
iterator message stream.

Covers:
- Successful spawn (mock SDK writes expected output file)
- Tool scoping per role (writer/runner/arbiter get different allow lists)
- Hook configuration injection when install_hooks=True
- ImportError surfaced cleanly when no SDK is available
- Unknown role fails fast before touching the SDK
- Missing output file triggers failure
- Exception inside SDK query is caught and reported
- _build_prompt concatenates template + context block
- Synchronous async shim runs asyncio.run() correctly
- Project root resolution for output verification

Run with:
    python -m blind_tdd.spawners.test_agent_sdk_spawner
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve()
_REPO = _HERE.parents[2]
sys.path.insert(0, str(_REPO))

from blind_tdd.spawners.agent_sdk_spawner import (  # noqa: E402
    AgentSdkSpawner,
    _ROLE_TO_TOOLS,
    _build_prompt,
    _expected_outputs_for,
)


# ---------------------------------------------------------------------------
# Mock SDK
# ---------------------------------------------------------------------------

class MockOptions:
    """Mock ClaudeAgentOptions — captures kwargs for inspection."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs


class MockSDK:
    """Fake claude_agent_sdk module.

    `query()` returns an async iterator that yields a few fake message
    objects, then — if `write_output` is set — writes the expected
    output file to simulate the agent's side effects.
    """

    def __init__(self, *, messages_to_yield: int = 3,
                 write_output: Path | None = None,
                 raise_on_query: Exception | None = None):
        self.ClaudeAgentOptions = MockOptions
        self._messages_to_yield = messages_to_yield
        self._write_output = write_output
        self._raise_on_query = raise_on_query
        self.last_prompt: str | None = None
        self.last_options: MockOptions | None = None

    def query(self, *, prompt: str, options: Any) -> Any:
        self.last_prompt = prompt
        self.last_options = options
        if self._raise_on_query:
            raise self._raise_on_query
        write_output = self._write_output
        count = self._messages_to_yield

        async def _iter():
            if write_output is not None:
                write_output.parent.mkdir(parents=True, exist_ok=True)
                write_output.write_text('{"ok": true}', encoding="utf-8")
            for i in range(count):
                yield {"type": "message", "idx": i}

        return _iter()


def _chdir(path: Path):
    class _Ctx:
        def __enter__(self_):
            self_.prev = os.getcwd()
            os.chdir(path)
            return path

        def __exit__(self_, *a):
            os.chdir(self_.prev)
    return _Ctx()


def _setup_fake_ralph_home(tmp: Path) -> Path:
    ralph = tmp / "ralph_home"
    (ralph / "templates" / "hooks").mkdir(parents=True)
    (ralph / "templates" / "hooks" / "blind_tdd_path_guard.py").write_text(
        "# fake\n", encoding="utf-8"
    )
    (ralph / "templates" / "hooks" / "blind_tdd_audit.py").write_text(
        "# fake\n", encoding="utf-8"
    )
    return ralph


# ---------------------------------------------------------------------------
# Successful spawns
# ---------------------------------------------------------------------------

def test_writer_spawn_success_writes_expected_output():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ralph = _setup_fake_ralph_home(tmp)
        project = tmp / "project"
        project.mkdir()

        expected = project / ".themis" / "blind_tdd" / "triage" / "t1.json"
        sdk = MockSDK(write_output=expected)

        spawner = AgentSdkSpawner(
            sdk=sdk, project_root=project, ralph_home=ralph,
            install_hooks=False,
        )
        result = spawner.spawn(
            role="test_writer",
            prompt_template="# writer",
            inputs={"task_id": "t1", "session_id": "sess-1",
                    "task": {"id": "t1"}},
        )
        assert result["success"], f"got {result}"
        assert result["agent_id"] == "sess-1"
        assert result["output_files"] == [".themis/blind_tdd/triage/t1.json"]
        assert result["diagnostics"]["message_count"] == 3
        assert result["diagnostics"]["role"] == "test_writer"


def test_runner_spawn_success():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ralph = _setup_fake_ralph_home(tmp)
        project = tmp / "project"
        project.mkdir()

        expected = project / ".themis" / "blind_tdd" / "green_report" / "t2.json"
        sdk = MockSDK(write_output=expected)

        spawner = AgentSdkSpawner(
            sdk=sdk, project_root=project, ralph_home=ralph,
            install_hooks=False,
        )
        result = spawner.spawn(
            role="test_runner",
            prompt_template="# runner",
            inputs={"task_id": "t2", "session_id": "sess-2",
                    "task": {"id": "t2"}},
        )
        assert result["success"]
        assert result["output_files"] == [".themis/blind_tdd/green_report/t2.json"]


def test_arbiter_spawn_uses_challenge_id():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ralph = _setup_fake_ralph_home(tmp)
        project = tmp / "project"
        project.mkdir()

        expected = project / ".themis" / "blind_tdd" / "rulings" / "chal-9.json"
        sdk = MockSDK(write_output=expected)

        spawner = AgentSdkSpawner(
            sdk=sdk, project_root=project, ralph_home=ralph,
            install_hooks=False,
        )
        result = spawner.spawn(
            role="arbiter",
            prompt_template="# arbiter",
            inputs={"task_id": "t3", "session_id": "sess-3",
                    "task": {"id": "t3"}, "challenge_id": "chal-9"},
        )
        assert result["success"]
        assert result["output_files"] == [".themis/blind_tdd/rulings/chal-9.json"]


# ---------------------------------------------------------------------------
# Tool scoping
# ---------------------------------------------------------------------------

def test_writer_tool_allow_list():
    tools = _ROLE_TO_TOOLS["test_writer"]
    assert "Read" in tools
    assert "Write" in tools
    assert "Edit" in tools
    assert "Bash" not in tools  # writer has no shell


def test_runner_tool_allow_list():
    tools = _ROLE_TO_TOOLS["test_runner"]
    assert "Bash" in tools  # runner needs shell to run pytest
    assert "Write" not in tools  # but cannot modify tests
    assert "Edit" not in tools


def test_arbiter_tool_allow_list():
    tools = _ROLE_TO_TOOLS["arbiter"]
    assert "Read" in tools
    assert "Write" not in tools  # judge-only
    assert "Edit" not in tools
    assert "Bash" not in tools


def test_spawn_passes_tools_in_options():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ralph = _setup_fake_ralph_home(tmp)
        project = tmp / "project"
        project.mkdir()

        expected = project / ".themis" / "blind_tdd" / "triage" / "t1.json"
        sdk = MockSDK(write_output=expected)

        spawner = AgentSdkSpawner(
            sdk=sdk, project_root=project, ralph_home=ralph,
            install_hooks=False,
        )
        spawner.spawn(
            role="test_writer", prompt_template="",
            inputs={"task_id": "t1", "session_id": "s", "task": {"id": "t1"}},
        )
        opts = sdk.last_options
        assert opts is not None
        assert opts.kwargs["allowed_tools"] == _ROLE_TO_TOOLS["test_writer"]
        assert opts.kwargs["cwd"] == str(project)
        assert opts.kwargs["permission_mode"] == "acceptEdits"


# ---------------------------------------------------------------------------
# Hook configuration
# ---------------------------------------------------------------------------

def test_hooks_included_when_install_hooks_true():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ralph = _setup_fake_ralph_home(tmp)
        project = tmp / "project"
        project.mkdir()

        expected = project / ".themis" / "blind_tdd" / "triage" / "t1.json"
        sdk = MockSDK(write_output=expected)

        spawner = AgentSdkSpawner(
            sdk=sdk, project_root=project, ralph_home=ralph,
            install_hooks=True,
        )
        spawner.spawn(
            role="test_writer", prompt_template="",
            inputs={"task_id": "t1", "session_id": "s", "task": {"id": "t1"}},
        )
        hooks = sdk.last_options.kwargs.get("hooks")
        assert hooks is not None
        assert "PreToolUse" in hooks
        assert "PostToolUse" in hooks
        pretoo = hooks["PreToolUse"][0]
        assert "Read" in pretoo["matcher"]
        assert "blind_tdd_path_guard.py" in pretoo["hooks"][0]["command"]


def test_hooks_omitted_when_install_hooks_false():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ralph = _setup_fake_ralph_home(tmp)
        project = tmp / "project"
        project.mkdir()

        expected = project / ".themis" / "blind_tdd" / "triage" / "t1.json"
        sdk = MockSDK(write_output=expected)

        spawner = AgentSdkSpawner(
            sdk=sdk, project_root=project, ralph_home=ralph,
            install_hooks=False,
        )
        spawner.spawn(
            role="test_writer", prompt_template="",
            inputs={"task_id": "t1", "session_id": "s", "task": {"id": "t1"}},
        )
        assert "hooks" not in sdk.last_options.kwargs


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------

def test_unknown_role_fails_before_sdk_call():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ralph = _setup_fake_ralph_home(tmp)
        project = tmp / "project"
        project.mkdir()

        sdk = MockSDK()
        spawner = AgentSdkSpawner(
            sdk=sdk, project_root=project, ralph_home=ralph,
        )
        result = spawner.spawn(
            role="not_a_role", prompt_template="",
            inputs={"task_id": "x", "session_id": "s"},
        )
        assert not result["success"]
        assert "unknown role" in result["error"]
        assert sdk.last_prompt is None  # SDK was never called


def test_missing_sdk_reports_import_error():
    """When no SDK is injected and the real one isn't installed."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ralph = _setup_fake_ralph_home(tmp)
        project = tmp / "project"
        project.mkdir()

        # No sdk= argument → spawner tries the real import
        spawner = AgentSdkSpawner(
            project_root=project, ralph_home=ralph,
        )
        result = spawner.spawn(
            role="test_writer", prompt_template="",
            inputs={"task_id": "t1", "session_id": "s", "task": {"id": "t1"}},
        )
        # Either the SDK is installed (rare here) → success OR import fails
        if not result["success"]:
            assert "claude_agent_sdk not available" in result["error"]


def test_sdk_exception_captured():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ralph = _setup_fake_ralph_home(tmp)
        project = tmp / "project"
        project.mkdir()

        sdk = MockSDK(raise_on_query=RuntimeError("simulated SDK failure"))
        spawner = AgentSdkSpawner(
            sdk=sdk, project_root=project, ralph_home=ralph,
            install_hooks=False,
        )
        result = spawner.spawn(
            role="test_writer", prompt_template="",
            inputs={"task_id": "t1", "session_id": "s", "task": {"id": "t1"}},
        )
        assert not result["success"]
        assert "SDK query raised" in result["error"]
        assert "simulated SDK failure" in result["error"]


def test_missing_output_file_reports_failure():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ralph = _setup_fake_ralph_home(tmp)
        project = tmp / "project"
        project.mkdir()

        # SDK runs fine but writes NO output file
        sdk = MockSDK(write_output=None)
        spawner = AgentSdkSpawner(
            sdk=sdk, project_root=project, ralph_home=ralph,
            install_hooks=False,
        )
        result = spawner.spawn(
            role="test_writer", prompt_template="",
            inputs={"task_id": "t1", "session_id": "s", "task": {"id": "t1"}},
        )
        assert not result["success"]
        assert "did not produce expected output files" in result["error"]


# ---------------------------------------------------------------------------
# Prompt / context
# ---------------------------------------------------------------------------

def test_build_prompt_contains_template_and_context():
    template = "# Stub prompt\nBe blind."
    prompt = _build_prompt(
        template,
        {"task_id": "t1", "session_id": "s",
         "task": {"id": "t1", "acceptance_criteria": [{"id": "AC-1"}]}},
        [".themis/blind_tdd/triage/t1.json"],
    )
    assert "# Stub prompt" in prompt
    assert "Be blind" in prompt
    assert "AC-1" in prompt
    assert "expected_output_files" in prompt
    assert "triage/t1.json" in prompt


def test_build_prompt_strips_none_values():
    prompt = _build_prompt(
        "", {"task_id": "t", "session_id": "s", "task": {"id": "t"}}, [],
    )
    assert "red_phase_hashes" not in prompt
    assert "challenge_id" not in prompt


def test_expected_outputs_per_role():
    assert _expected_outputs_for("test_writer", {"task_id": "t"}) == [
        ".themis/blind_tdd/triage/t.json"
    ]
    assert _expected_outputs_for("test_runner", {"task_id": "t"}) == [
        ".themis/blind_tdd/green_report/t.json"
    ]
    assert _expected_outputs_for(
        "arbiter", {"task_id": "t", "challenge_id": "c1"}
    ) == [".themis/blind_tdd/rulings/c1.json"]
    assert _expected_outputs_for("unknown", {"task_id": "t"}) == []


# ---------------------------------------------------------------------------
# Async shim
# ---------------------------------------------------------------------------

def test_spawner_is_synchronous_from_caller_perspective():
    """AgentSpawner.spawn() is sync; the SDK async call is wrapped internally."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ralph = _setup_fake_ralph_home(tmp)
        project = tmp / "project"
        project.mkdir()

        expected = project / ".themis" / "blind_tdd" / "triage" / "t.json"
        sdk = MockSDK(write_output=expected)
        spawner = AgentSdkSpawner(
            sdk=sdk, project_root=project, ralph_home=ralph,
            install_hooks=False,
        )
        # Call without await — must return a dict, not a coroutine
        result = spawner.spawn(
            role="test_writer", prompt_template="",
            inputs={"task_id": "t", "session_id": "s", "task": {"id": "t"}},
        )
        assert isinstance(result, dict)
        assert not asyncio.iscoroutine(result)


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
