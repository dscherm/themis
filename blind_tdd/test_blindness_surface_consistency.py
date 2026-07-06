"""Cross-surface consistency for the blind-role capability model.

The write capability of each blind role is encoded in FOUR independent places,
and a live run proved they can drift apart silently (three of them forbade a
role's own mandated output while the fourth required it):

  #1  session.default_allowed_paths(role)        — path scope the guard enforces
  #2  templates/blind_tdd/settings.blind-*.json  — tool allow/deny + guard matcher
  #3  plugin assemble.py ROLES[role]["tools"]    — plugin subagent tool grant
  #4  templates/blind_tdd/prompts/*.md           — the rule stated to the agent

Per-surface regression tests already exist (test_path_guard_hook,
test_role_settings, test_assemble). THIS test asserts the surfaces *agree with
each other*: for every role that must emit a report, all three enforcement
surfaces permit exactly that write, and the prose does not contradict them.
It is the safety net that makes a future consolidation to one source of truth
(role_capabilities) refactorable with confidence — and, until then, the thing
that fails if any single surface is edited out of step with the rest.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

from blind_tdd.orchestrator import _expected_manual_output
from blind_tdd.session import default_allowed_paths, default_blocked_paths

_REPO = Path(__file__).resolve().parents[1]
_TEMPLATES = _REPO / "templates" / "blind_tdd"
_GUARD_PATH = _REPO / "templates" / "hooks" / "blind_tdd_path_guard.py"
_ASSEMBLE_PATH = _REPO / "plugin" / "blind-tdd" / "assemble.py"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_GUARD = _load_module(_GUARD_PATH, "_bt_path_guard")
_ASSEMBLE = _load_module(_ASSEMBLE_PATH, "_bt_assemble")

# The single mapping the four surfaces are keyed by. engine_role is the
# authority (session.py / orchestrator.py use it); the rest hang off it.
ROLE_REGISTRY = {
    "test_writer": {"plugin_key": "writer", "settings": "settings.blind-writer.json",
                    "prompt": "prompts/test_writer.md"},
    "test_runner": {"plugin_key": "runner", "settings": "settings.blind-runner.json",
                    "prompt": "prompts/test_runner.md"},
    "arbiter": {"plugin_key": "arbiter", "settings": "settings.blind-arbiter.json",
                "prompt": "prompts/arbiter.md"},
}

# A flat denial of Write in prose, e.g. "Edit, Write, and NotebookEdit are
# denied" or "you have no Write access". "Write a structured report" must NOT
# trip this — hence the denial keyword is required near the Write token.
_WRITE_DENIED_RE = re.compile(
    r"(?:\bno\b[^.\n]{0,30}\bWrite\b[^.\n]{0,30}\baccess\b)"
    r"|(?:\bWrite\b[^.\n]{0,40}\b(?:are|is)\s+denied\b)",
    re.IGNORECASE,
)


def _load_json(name: str) -> dict:
    import json
    return json.loads((_TEMPLATES / name).read_text(encoding="utf-8"))


def _guard_matcher_covers_write(settings: dict) -> bool:
    for entry in settings["hooks"]["PreToolUse"]:
        if any("path_guard" in h.get("command", "") for h in entry.get("hooks", [])):
            if "Write" in entry.get("matcher", ""):
                return True
    return False


def _surface_verdicts(engine_role: str, reg: dict) -> dict:
    """Compute, per surface, whether the role may write its mandated output."""
    output = str(_expected_manual_output(engine_role, "T", {"challenge_id": "C"}))
    session = {
        "agent_role": engine_role,
        "allowed_paths": default_allowed_paths(engine_role),
        "blocked_paths": default_blocked_paths(),
    }
    # #1 — authoritative path-guard check against the real default paths.
    path_ok, _ = _GUARD._check_path(_GUARD._normalize_path(output), session)

    # #2 — settings grant Write, don't deny it, and route Write through the guard.
    settings = _load_json(reg["settings"])
    perms = settings["permissions"]
    settings_ok = (
        "Write" in (perms.get("allow") or [])
        and "Write" not in (perms.get("deny") or [])
        and _guard_matcher_covers_write(settings)
    )

    # #3 — plugin frontmatter tool grant includes Write.
    tools = _ASSEMBLE.ROLES[reg["plugin_key"]]["tools"]
    frontmatter_ok = "Write" in [t.strip() for t in tools.split(",")]

    # #4 — the prose does not flatly forbid Write, and DOES tell the agent to
    # write its report to the mandated location.
    prompt = (_TEMPLATES / reg["prompt"]).read_text(encoding="utf-8")
    out_dir = output.replace("\\", "/").rsplit("/", 1)[0]
    prose_ok = (_WRITE_DENIED_RE.search(prompt) is None) and (out_dir in prompt.replace("\\", "/"))

    return {"path": path_ok, "settings": settings_ok,
            "frontmatter": frontmatter_ok, "prose": prose_ok}


@pytest.mark.parametrize("engine_role", sorted(ROLE_REGISTRY))
def test_all_surfaces_agree_role_can_write_its_report(engine_role: str):
    """Every enforcement surface permits the role's mandated write, and the
    prose agrees. A single surface out of step fails here."""
    verdicts = _surface_verdicts(engine_role, ROLE_REGISTRY[engine_role])
    disagreeing = [k for k, v in verdicts.items() if not v]
    assert not disagreeing, (
        f"{engine_role}: surfaces disagree on writing its mandated output — "
        f"these forbid/contradict it: {disagreeing}. All four surfaces must "
        f"encode the same capability (see role_capabilities consolidation)."
    )


def test_registry_covers_every_role_with_a_mandated_output():
    """If a new blind role gains a mandated output, it must be wired into every
    surface here — this fails until it is, preventing a silent fourth gap."""
    for role in ("test_writer", "test_runner", "arbiter"):
        assert _expected_manual_output(role, "T", {"challenge_id": "C"}) is not None
        assert role in ROLE_REGISTRY, f"{role} has a mandated output but is not in ROLE_REGISTRY"
    # Every registered role really does declare a mandated output.
    for role in ROLE_REGISTRY:
        assert _expected_manual_output(role, "T", {"challenge_id": "C"}) is not None


def test_write_denial_regex_does_not_flag_a_report_instruction():
    """Guard against a false-positive prose check: the legitimate instruction
    'Write a structured report ...' must not read as a Write denial."""
    assert _WRITE_DENIED_RE.search("Write a structured report to .themis/...") is None
    assert _WRITE_DENIED_RE.search("`Edit`, `Write`, and `NotebookEdit` are denied") is not None
    assert _WRITE_DENIED_RE.search("you have no Write access") is not None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
