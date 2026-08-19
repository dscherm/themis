"""The manual brief must name a settings template that actually exists.

`ManualSpawner.spawn` stages a markdown brief whose step 2 tells a human which
settings template to copy into `.claude/settings.json`. That template is not
cosmetic: it is what denies Bash and installs the PreToolUse path guard. A
brief that names a file which does not exist gets shrugged past, and the agent
then runs with **no guard at all** — unenforced blindness under a brief whose
step 2 implies it was configured.

The original defect derived the filename from the role
(`settings.blind-{role.replace('_','-')}.json`), which produced
`settings.blind-test-writer.json` for the `test_writer` role. No such file has
ever shipped.

These tests pin the brief against the real filenames on disk, and pin the
mapping itself to a single shared copy so the brief-writer and the spawner
cannot drift apart.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import blind_tdd.session as session
from blind_tdd.orchestrator import ManualSpawner

_TEMPLATES = Path(__file__).resolve().parents[1] / "templates" / "blind_tdd"

# Written out literally, on purpose: this is the pin. Deriving it from the code
# under test would let both sides drift together silently.
_EXPECTED = {
    "test_writer": "settings.blind-writer.json",
    "test_runner": "settings.blind-runner.json",
    "arbiter": "settings.blind-arbiter.json",
}

_ANY_SETTINGS_FILENAME = re.compile(r"settings\.blind-[A-Za-z0-9._-]+\.json")


def _stage_brief(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, role: str) -> str:
    """Run ManualSpawner in an isolated cwd and return the staged brief text."""
    monkeypatch.chdir(tmp_path)
    result = ManualSpawner().spawn(
        role=role,
        prompt_template="(prompt body)",
        inputs={"task_id": "T1"},
    )
    return Path(result["brief_path"]).read_text(encoding="utf-8")


@pytest.mark.parametrize("role,settings_file", sorted(_EXPECTED.items()))
def test_brief_names_the_real_settings_template(
    role: str, settings_file: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Step 2 of the brief names the template that actually ships for the role."""
    brief = _stage_brief(tmp_path, monkeypatch, role)
    assert settings_file in brief, (
        f"{role}: brief does not name {settings_file}; "
        f"named instead: {sorted(set(_ANY_SETTINGS_FILENAME.findall(brief)))}"
    )
    named = set(_ANY_SETTINGS_FILENAME.findall(brief))
    assert named == {settings_file}, (
        f"{role}: brief names a settings file it should not: {sorted(named)}"
    )
    assert (_TEMPLATES / settings_file).is_file(), (
        f"{role}: brief names {settings_file}, which is not on disk"
    )


def test_mapped_settings_templates_exist_on_disk():
    """Every filename in the shared mapping is a real template file.

    Both templates and mapping live in this repo, so this is a full check —
    the mapping cannot drift from what ships.
    """
    mapping = session.ROLE_SETTINGS_TEMPLATES
    assert mapping, "role → settings mapping is empty"
    missing = [
        f"{role} → {name}"
        for role, name in mapping.items()
        if not (_TEMPLATES / name).is_file()
    ]
    assert not missing, f"mapped settings template(s) not on disk: {missing}"


def test_orchestrator_and_spawner_read_one_mapping():
    """Not two mappings that happen to agree today — literally the same object."""
    from blind_tdd.spawners import claude_code_spawner

    assert (
        claude_code_spawner._ROLE_TO_SETTINGS is session.ROLE_SETTINGS_TEMPLATES
    ), "the spawner holds its own copy of the role → settings mapping"


def test_unmapped_role_does_not_fabricate_a_filename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A guess that looks like an answer is worse than a visible gap.

    Printing a plausible filename for an unmapped role is exactly how the
    original defect shipped, so the brief must show the hole instead.
    """
    brief = _stage_brief(tmp_path, monkeypatch, "reviewer")
    named = _ANY_SETTINGS_FILENAME.findall(brief)
    assert not named, f"brief fabricated a settings filename for an unmapped role: {named}"
    assert "no settings template" in brief.lower(), (
        "brief does not say out loud that no template is mapped for this role"
    )
