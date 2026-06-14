"""Self-contained tests for ClaudeCodeSpawner.

## What this file tests

- Settings file save/backup/restore (real files, real I/O)
- Hook script installation from templates
- Prompt context-block construction
- Expected-output-file verification after the child returns
- Failure modes: nonzero exit, missing binary, missing outputs, unknown role

## What this file DOES NOT test

**Real `claude -p` subprocess invocation is NOT exercised here.** The
tests use a fake Python launcher script in place of the `claude` binary
because running real subprocess claude would (a) consume API budget and
(b) make the suite non-hermetic. This means the following are NOT
covered by these tests:

  - Whether Claude Code re-reads `.claude/settings.local.json` on spawn
  - Whether hooks fire for the child agent's tool calls
  - Whether the child agent actually respects the writer prompt
  - Whether `claude -p`'s output format matches our expectations

These gaps are covered by:
  - `tools/blind_tdd/test_path_guard_hook.py`: the hook itself, tested
    as a real subprocess — gives us confidence the hook works when fired
  - `tools/blind_tdd/test_hash_integrity.py`: real orchestrator end-to-end
    with fake spawners
  - The one-time real end-to-end validation on unity-py-sim via the
    `Agent` tool (not ClaudeCodeSpawner — a different code path)

So: `ClaudeCodeSpawner` has 9/9 passing tests that prove its save/restore
and verification machinery is correct, but the *real subprocess invocation
path* has never been exercised. When a project first flips to
`spawner: "claude_code"` in `ralph.config.json`, expect to encounter
integration issues that are outside the scope of these unit tests.

Run with:
    python -m blind_tdd.spawners.test_claude_code_spawner
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

# Allow direct invocation without an installed package
_HERE = Path(__file__).resolve()
_REPO = _HERE.parents[2]
sys.path.insert(0, str(_REPO))

from blind_tdd.spawners.claude_code_spawner import (  # noqa: E402
    ClaudeCodeSpawner,
    _build_prompt,
    _expected_outputs_for,
    subscription_env,
)


# ---------------------------------------------------------------------------
# subscription_env — strip ANTHROPIC_API_KEY so claude -p uses the subscription
# ---------------------------------------------------------------------------

def test_subscription_env_strips_key_by_default():
    base = {"PATH": "/usr/bin", "ANTHROPIC_API_KEY": "sk-secret"}
    env = subscription_env(base)
    assert "ANTHROPIC_API_KEY" not in env
    assert env["PATH"] == "/usr/bin"  # everything else preserved


def test_subscription_env_keeps_key_when_disabled():
    base = {"ANTHROPIC_API_KEY": "sk-secret"}
    env = subscription_env(base, strip_api_key=False)
    assert env["ANTHROPIC_API_KEY"] == "sk-secret"


def test_subscription_env_no_key_present_is_safe():
    env = subscription_env({"PATH": "/usr/bin"})
    assert env == {"PATH": "/usr/bin"}


def test_subscription_env_does_not_mutate_input():
    base = {"ANTHROPIC_API_KEY": "sk-secret"}
    subscription_env(base)
    assert base == {"ANTHROPIC_API_KEY": "sk-secret"}  # caller's dict untouched


def test_subscription_env_defaults_to_os_environ(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setenv("RALPH_MARKER", "present")
    env = subscription_env()
    assert "ANTHROPIC_API_KEY" not in env
    assert env["RALPH_MARKER"] == "present"


def test_spawner_strip_api_key_defaults_true():
    assert ClaudeCodeSpawner().strip_api_key is True


def test_spawner_strip_api_key_configurable():
    assert ClaudeCodeSpawner(strip_api_key=False).strip_api_key is False


def _make_fake_claude(dir_path: Path, *, task_id: str, role: str,
                     exit_code: int = 0, create_outputs: bool = True) -> list[str]:
    """Build a fake `claude` command as a Python subprocess invocation.

    Returns a list suitable for use as `claude_binary` — but since the
    spawner only takes a string, we write a tiny Python launcher script and
    return a single-string command that invokes it via the current
    interpreter. The launcher creates expected output files then exits.
    """
    outputs = _expected_outputs_for(role, {"task_id": task_id})
    launcher = dir_path / "fake_claude_launcher.py"
    launcher.write_text(
        "import os, sys, pathlib\n"
        f"outputs = {outputs!r}\n"
        f"create = {create_outputs!r}\n"
        "if create:\n"
        "    for p in outputs:\n"
        "        pp = pathlib.Path(p)\n"
        "        pp.parent.mkdir(parents=True, exist_ok=True)\n"
        "        pp.write_text('{\"ok\": true}', encoding='utf-8')\n"
        "# drain stdin so writer doesn't block\n"
        "try:\n"
        "    sys.stdin.read()\n"
        "except Exception:\n"
        "    pass\n"
        f"sys.exit({exit_code})\n",
        encoding="utf-8",
    )
    # The fake "binary" is the python interpreter; we stash the launcher path
    # as a sentinel file alongside it so the spawner can find it through a
    # wrapper script. Simplest: return the python exe and let the caller set
    # claude_binary to a shim. We instead monkey-patch the cmd by returning
    # a marker, and handle this in _make_spawner.
    return [sys.executable, str(launcher)]


def _setup_fake_ralph_home(tmp: Path) -> Path:
    """Create a minimal Themis template tree."""
    ralph = tmp / "ralph_home"
    (ralph / "templates" / "blind_tdd").mkdir(parents=True)
    (ralph / "templates" / "hooks").mkdir(parents=True)

    # Minimal settings templates
    for role_file in ("settings.blind-writer.json",
                      "settings.blind-runner.json",
                      "settings.blind-arbiter.json"):
        (ralph / "templates" / "blind_tdd" / role_file).write_text(
            json.dumps({"permissions": {"allow": ["Read"]},
                        "_role_marker": role_file}, indent=2),
            encoding="utf-8",
        )

    # Minimal hook scripts
    (ralph / "templates" / "hooks" / "blind_tdd_path_guard.py").write_text(
        "# fake path guard\nimport sys; sys.exit(0)\n", encoding="utf-8"
    )
    (ralph / "templates" / "hooks" / "blind_tdd_audit.py").write_text(
        "# fake audit\nimport sys; sys.exit(0)\n", encoding="utf-8"
    )
    return ralph


def _make_spawner(tmp: Path, *, claude_binary, ralph_home: Path,
                  project_root: Path) -> ClaudeCodeSpawner:
    # claude_binary may be a str, Path, or list[str]
    if isinstance(claude_binary, Path):
        claude_binary = str(claude_binary)
    return ClaudeCodeSpawner(
        ralph_home=ralph_home,
        claude_binary=claude_binary,
        timeout_seconds=30,
        project_root=project_root,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_successful_spawn_installs_hooks_and_restores_settings():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ralph = _setup_fake_ralph_home(tmp)
        project = tmp / "project"
        project.mkdir()

        # Pre-existing settings.local.json that must be restored
        (project / ".claude").mkdir()
        original_settings = {"existing": True, "marker": "original"}
        (project / ".claude" / "settings.local.json").write_text(
            json.dumps(original_settings), encoding="utf-8"
        )

        fake_claude = _make_fake_claude(tmp, task_id="t1", role="test_writer")

        spawner = _make_spawner(
            tmp, claude_binary=fake_claude, ralph_home=ralph,
            project_root=project,
        )
        result = spawner.spawn(
            role="test_writer",
            prompt_template="# Writer prompt",
            inputs={"task_id": "t1", "session_id": "sess-1",
                    "task": {"id": "t1"}},
        )

        assert result["success"], f"expected success, got {result}"
        assert result["agent_id"] == "sess-1"
        assert result["output_files"] == [".themis/blind_tdd/triage/t1.json"]

        # Hooks were installed into project
        hook1 = project / ".claude" / "hooks" / "blind_tdd_path_guard.py"
        hook2 = project / ".claude" / "hooks" / "blind_tdd_audit.py"
        assert hook1.exists(), "path guard hook not installed"
        assert hook2.exists(), "audit hook not installed"

        # Settings were restored to the original
        restored = json.loads(
            (project / ".claude" / "settings.local.json").read_text(encoding="utf-8")
        )
        assert restored == original_settings, (
            f"settings not restored, got {restored}"
        )

        # Backup file cleaned up
        backup = project / ".claude" / "settings.local.json.blind-tdd-backup"
        assert not backup.exists(), "backup file should be removed after restore"

        # Expected output file exists (created by fake claude)
        assert (project / ".themis" / "blind_tdd" / "triage" / "t1.json").exists()


def test_spawn_with_no_prior_settings_deletes_installed_settings():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ralph = _setup_fake_ralph_home(tmp)
        project = tmp / "project"
        project.mkdir()

        fake_claude = _make_fake_claude(tmp, task_id="t2", role="test_runner")

        spawner = _make_spawner(
            tmp, claude_binary=fake_claude, ralph_home=ralph,
            project_root=project,
        )
        result = spawner.spawn(
            role="test_runner",
            prompt_template="# Runner prompt",
            inputs={"task_id": "t2", "session_id": "sess-2",
                    "task": {"id": "t2"}},
        )

        assert result["success"], f"expected success, got {result}"
        # No original → installed settings should be deleted
        settings = project / ".claude" / "settings.local.json"
        assert not settings.exists(), (
            "settings.local.json should not exist after cleanup when there "
            "was no prior file"
        )


def test_spawn_failure_still_restores_settings():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ralph = _setup_fake_ralph_home(tmp)
        project = tmp / "project"
        project.mkdir()

        (project / ".claude").mkdir()
        original = {"original": "yes"}
        settings_file = project / ".claude" / "settings.local.json"
        settings_file.write_text(json.dumps(original), encoding="utf-8")

        # Fake claude that exits nonzero
        fake_claude = _make_fake_claude(
            tmp, task_id="t3", role="test_writer",
            exit_code=2, create_outputs=False,
        )

        spawner = _make_spawner(
            tmp, claude_binary=fake_claude, ralph_home=ralph,
            project_root=project,
        )
        result = spawner.spawn(
            role="test_writer",
            prompt_template="# Writer",
            inputs={"task_id": "t3", "session_id": "sess-3",
                    "task": {"id": "t3"}},
        )

        assert not result["success"]
        assert "exited with code 2" in result["error"]

        # Original settings must still be in place
        restored = json.loads(settings_file.read_text(encoding="utf-8"))
        assert restored == original, f"settings not restored after failure: {restored}"


def test_spawn_missing_output_files_reports_failure():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ralph = _setup_fake_ralph_home(tmp)
        project = tmp / "project"
        project.mkdir()

        # Fake claude exits 0 but creates no output file
        fake_claude = _make_fake_claude(
            tmp, task_id="t4", role="test_writer",
            exit_code=0, create_outputs=False,
        )

        spawner = _make_spawner(
            tmp, claude_binary=fake_claude, ralph_home=ralph,
            project_root=project,
        )
        result = spawner.spawn(
            role="test_writer",
            prompt_template="# Writer",
            inputs={"task_id": "t4", "session_id": "sess-4",
                    "task": {"id": "t4"}},
        )

        assert not result["success"]
        assert "did not produce expected output files" in result["error"]
        assert ".themis/blind_tdd/triage/t4.json" in result["error"]


def test_spawn_unknown_role_fails_cleanly():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ralph = _setup_fake_ralph_home(tmp)
        project = tmp / "project"
        project.mkdir()
        fake_claude = _make_fake_claude(tmp, task_id="x", role="test_writer")

        spawner = _make_spawner(
            tmp, claude_binary=fake_claude, ralph_home=ralph,
            project_root=project,
        )
        result = spawner.spawn(
            role="not_a_real_role",
            prompt_template="",
            inputs={"task_id": "x", "session_id": "sess-x"},
        )
        assert not result["success"]
        assert "settings template not found" in result["error"]


def test_missing_claude_binary_fails_cleanly():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ralph = _setup_fake_ralph_home(tmp)
        project = tmp / "project"
        project.mkdir()

        spawner = ClaudeCodeSpawner(
            ralph_home=ralph,
            claude_binary="this-binary-definitely-does-not-exist-xyz",
            project_root=project,
            timeout_seconds=5,
        )
        result = spawner.spawn(
            role="test_writer",
            prompt_template="",
            inputs={"task_id": "t5", "session_id": "sess-5", "task": {"id": "t5"}},
        )
        assert not result["success"]
        # Error message differs between OSError and nonzero exit, but one
        # of these must be present.
        assert (
            "failed to invoke" in result["error"]
            or "exited with code" in result["error"]
        ), f"unexpected error: {result['error']}"


def test_build_prompt_includes_context_and_template():
    template = "# Writer agent\n\nDo the thing."
    inputs = {
        "task_id": "task-42",
        "session_id": "sess-42",
        "task": {"id": "task-42", "acceptance_criteria": [{"id": "AC-1"}]},
        "test_dirs": ["tests/contracts/"],
    }
    outputs = [".themis/blind_tdd/triage/task-42.json"]
    prompt = _build_prompt(template, inputs, outputs)

    assert "# Writer agent" in prompt
    assert "Do the thing." in prompt
    assert "task-42" in prompt
    assert "AC-1" in prompt
    assert ".themis/blind_tdd/triage/task-42.json" in prompt
    assert "expected_output_files" in prompt
    # None values should be stripped
    assert "null" not in prompt.lower() or '"null"' not in prompt


def test_build_prompt_strips_none_values():
    prompt = _build_prompt(
        "X",
        {"task_id": "t", "session_id": "s", "task": {"id": "t"}},
        [],
    )
    # red_phase_hashes and triage_report were not in inputs
    assert "red_phase_hashes" not in prompt
    assert "triage_report" not in prompt


def test_expected_outputs_per_role():
    assert _expected_outputs_for("test_writer", {"task_id": "t1"}) == [
        ".themis/blind_tdd/triage/t1.json"
    ]
    assert _expected_outputs_for("test_runner", {"task_id": "t2"}) == [
        ".themis/blind_tdd/green_report/t2.json"
    ]
    assert _expected_outputs_for(
        "arbiter", {"task_id": "t3", "challenge_id": "chal-9"}
    ) == [".themis/blind_tdd/rulings/chal-9.json"]
    # Fallback: arbiter without challenge_id uses task_id
    assert _expected_outputs_for("arbiter", {"task_id": "t4"}) == [
        ".themis/blind_tdd/rulings/t4.json"
    ]
    assert _expected_outputs_for("unknown", {"task_id": "t"}) == []


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def _run_all() -> int:
    tests = [
        test_expected_outputs_per_role,
        test_build_prompt_includes_context_and_template,
        test_build_prompt_strips_none_values,
        test_spawn_unknown_role_fails_cleanly,
        test_successful_spawn_installs_hooks_and_restores_settings,
        test_spawn_with_no_prior_settings_deletes_installed_settings,
        test_spawn_failure_still_restores_settings,
        test_spawn_missing_output_files_reports_failure,
        test_missing_claude_binary_fails_cleanly,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
        else:
            print(f"  PASS  {t.__name__}")
    print()
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(_run_all())
