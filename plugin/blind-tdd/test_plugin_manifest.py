"""Manifest-wiring tests for the blind-tdd plugin (BT4 AC-1, regression guard).

The unit tests cover the hook *scripts*; these cover the plugin *manifest*, the
layer a live `/plugin install` actually reads. A real install (2026-06-22)
registered the agents but **0 hooks** because `hooks/hooks.json` listed the
events at the top level instead of under a `"hooks"` wrapper (and `plugin.json`
carried a redundant explicit `hooks` path). Claude Code auto-discovers
`hooks/hooks.json` and expects the wrapper — these tests pin the known-good
shape so the regression can't return silently.

Run: python -m pytest plugin/blind-tdd/test_plugin_manifest.py -q
"""

from __future__ import annotations

import json
from pathlib import Path

_HERE = Path(__file__).resolve()
_PLUGIN_DIR = _HERE.parent
_HOOKS_JSON = _PLUGIN_DIR / "hooks" / "hooks.json"
_PLUGIN_JSON = _PLUGIN_DIR / ".claude-plugin" / "plugin.json"


def test_hooks_json_wraps_events_under_hooks_key():
    """hooks.json must nest event arrays under a top-level "hooks" object."""
    data = json.loads(_HOOKS_JSON.read_text(encoding="utf-8"))
    assert "hooks" in data and isinstance(data["hooks"], dict), (
        "hooks.json must wrap events under a top-level 'hooks' object "
        "(the auto-discovery format); events at the root register as 0 hooks"
    )
    events = data["hooks"]
    assert "PreToolUse" in events, "PreToolUse hook missing"
    assert "PostToolUse" in events, "PostToolUse hook missing"
    # No event arrays leaked to the root (the exact shape that broke install).
    assert "PreToolUse" not in data and "PostToolUse" not in data


def test_hooks_reference_existing_scripts():
    """Every hook command points at a script that ships in the plugin."""
    events = json.loads(_HOOKS_JSON.read_text(encoding="utf-8"))["hooks"]
    referenced = []
    for entries in events.values():
        for entry in entries:
            for hook in entry["hooks"]:
                referenced.append(hook["command"])
    assert referenced, "no hook commands declared"
    for cmd in referenced:
        assert "${CLAUDE_PLUGIN_ROOT}/hooks/" in cmd, f"hook not rooted at plugin: {cmd}"
        script = cmd.split("${CLAUDE_PLUGIN_ROOT}/hooks/", 1)[1].strip().strip('"')
        assert (_PLUGIN_DIR / "hooks" / script).exists(), f"hook script missing: {script}"


def test_pretooluse_guards_reads_and_bash():
    """The PreToolUse matchers cover file-read tools and Bash (the two guards)."""
    events = json.loads(_HOOKS_JSON.read_text(encoding="utf-8"))["hooks"]
    matchers = [e.get("matcher", "") for e in events["PreToolUse"]]
    assert any("Read" in m for m in matchers), "no path-guard matcher covering Read"
    assert any(m == "Bash" or "Bash" in m for m in matchers), "no bash-guard matcher"


def test_plugin_json_relies_on_hook_autodiscovery():
    """plugin.json must NOT carry an explicit hooks path (auto-discovery only).

    Every shipped reference plugin omits it; carrying it coincided with the
    0-hooks install bug. Pin the known-good shape.
    """
    data = json.loads(_PLUGIN_JSON.read_text(encoding="utf-8"))
    assert "hooks" not in data, (
        "plugin.json should not declare a 'hooks' path — Claude Code "
        "auto-discovers hooks/hooks.json"
    )
    assert data.get("name") == "blind-tdd"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
