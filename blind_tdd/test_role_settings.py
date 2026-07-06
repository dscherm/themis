"""Settings-layer consistency for the blind roles — closes the second half of
the mandated-output gap.

Blindness is enforced at TWO layers: the PreToolUse path guard (allowed_paths,
tested in test_path_guard_hook) and the role's `.claude/settings.local.json`
permission set (allow/deny, copied from these templates by the claude_code
spawner). Both must agree that a role can produce its mandated output.

A live run exposed the gap: the writer/runner/arbiter each MUST write a report
(triage / green_report / ruling), yet the runner and arbiter settings DENIED
`Write` outright (to protect sealed tests) — so the agent, told to write a file
it had no tool for, flailed and never emitted its report. The fix allows Write
while keeping the path guard (matcher must include Write) and the locked-test
hash rule as the real enforcement: a role can write its report but not a sealed
test.

These tests assert, for every role, that the shipped settings template lets it
Write AND routes that Write through the path guard.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_TEMPLATES = Path(__file__).resolve().parents[1] / "templates" / "blind_tdd"

# Roles that must emit a report via Write, and the settings file each uses.
_WRITING_ROLES = {
    "test_writer": "settings.blind-writer.json",
    "test_runner": "settings.blind-runner.json",
    "arbiter": "settings.blind-arbiter.json",
}


def _load(settings_file: str) -> dict:
    return json.loads((_TEMPLATES / settings_file).read_text(encoding="utf-8"))


@pytest.mark.parametrize("role,settings_file", sorted(_WRITING_ROLES.items()))
def test_writing_role_permits_write(role: str, settings_file: str):
    """Each role that must emit a report has Write allowed, not denied."""
    perms = _load(settings_file)["permissions"]
    allow = perms.get("allow") or []
    deny = perms.get("deny") or []
    assert "Write" in allow, f"{role}: Write missing from allow — cannot emit its report"
    assert "Write" not in deny, f"{role}: Write is denied — cannot emit its report"


@pytest.mark.parametrize("role,settings_file", sorted(_WRITING_ROLES.items()))
def test_write_is_path_guarded(role: str, settings_file: str):
    """Allowing Write only stays safe if the path guard runs on Write calls —
    the PreToolUse matcher must include Write, or the allowed_paths scoping and
    locked-test hash rule never fire for the role's writes."""
    settings = _load(settings_file)
    pre = settings["hooks"]["PreToolUse"]
    guard_matchers = [
        h["matcher"] for h in pre
        if any("path_guard" in hook.get("command", "") for hook in h.get("hooks", []))
    ]
    assert guard_matchers, f"{role}: no path_guard PreToolUse hook configured"
    assert any("Write" in m for m in guard_matchers), (
        f"{role}: path guard matcher(s) {guard_matchers} do not cover Write — "
        f"the role could write outside its allowed_paths unguarded"
    )


@pytest.mark.parametrize("role,settings_file", sorted(_WRITING_ROLES.items()))
def test_edit_stays_denied_for_non_writer_or_guarded(role: str, settings_file: str):
    """Sealed tests must stay protected. The runner and arbiter never legitimately
    Edit a file in place, so Edit stays denied; the writer legitimately edits
    tests (during authoring / after an upheld challenge) and routes Edit through
    the guard instead."""
    perms = _load(settings_file)["permissions"]
    deny = perms.get("deny") or []
    if role == "test_writer":
        matcher = _load(settings_file)["hooks"]["PreToolUse"][0]["matcher"]
        assert "Edit" in matcher, "writer Edit must be path-guarded"
    else:
        assert "Edit" in deny, f"{role}: Edit should stay denied (tests are read-only)"


def test_all_role_settings_are_valid_json_with_guard_and_audit():
    """Every role settings template parses and wires both hooks."""
    for settings_file in _WRITING_ROLES.values():
        s = _load(settings_file)
        assert s["permissions"]["allow"]
        hooks = s["hooks"]
        assert any(
            "path_guard" in hook.get("command", "")
            for entry in hooks["PreToolUse"] for hook in entry.get("hooks", [])
        ), f"{settings_file}: missing path_guard PreToolUse hook"
        assert any(
            "audit" in hook.get("command", "")
            for entry in hooks["PostToolUse"] for hook in entry.get("hooks", [])
        ), f"{settings_file}: missing audit PostToolUse hook"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
