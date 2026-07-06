"""Tests for the security-pack tailoring wizard engine (pack_wizard.py).

The wizard's load-bearing property is the objective exit gate: the interview
is "done" when the lint is clean, so the lint must reuse the gate's own
criterion-quality checks (subjective language, observability) and must flag
genericity — the exact weakness the interview exists to remove — without
blocking the deliberately-generic shipped default pack.
"""

from __future__ import annotations

import json
from pathlib import Path

from blind_tdd.pack_wizard import (
    DEFAULT_PACK_PATH,
    build_match,
    lint_pack_draft,
    main,
    merge_pack_config,
)
from blind_tdd.security_pack import DEFAULT_PACK_RELPATH

_REPO = Path(__file__).resolve().parents[1]

CONCRETE_PACK = {
    "version": 1,
    "name": "tailored",
    "criteria": [
        {
            "category": "input-validation-size",
            "given": "the parse_config function in src.config",
            "when": "parse_config is called with a string longer than 4096 characters",
            "then": "parse_config raises ValueError before reading the config",
            "notes": "4096 is the documented limit; triage needs_human if it changes.",
        },
    ],
}


# ---------------------------------------------------------------------------
# lint_pack_draft
# ---------------------------------------------------------------------------

def test_concrete_pack_lints_clean():
    errors, warnings = lint_pack_draft(CONCRETE_PACK)
    assert errors == []
    assert warnings == []


def test_structural_errors_propagate():
    errors, _ = lint_pack_draft({"criteria": []})
    assert errors  # validate_pack: missing version, empty criteria


def test_subjective_then_is_an_error():
    pack = json.loads(json.dumps(CONCRETE_PACK))
    pack["criteria"][0]["then"] = "the output looks clean"
    errors, _ = lint_pack_draft(pack)
    assert any("subjective" in e for e in errors)


def test_unobservable_then_is_an_error():
    pack = json.loads(json.dumps(CONCRETE_PACK))
    pack["criteria"][0]["then"] = "the input is handled appropriately"
    errors, _ = lint_pack_draft(pack)
    assert any("observable" in e for e in errors)


def test_generic_phrasing_is_a_warning_not_an_error():
    pack = json.loads(json.dumps(CONCRETE_PACK))
    pack["criteria"][0]["given"] = "any public entry point that accepts input"
    errors, warnings = lint_pack_draft(pack)
    assert errors == []
    assert any("generic" in w for w in warnings)


def test_missing_notes_is_a_warning():
    pack = json.loads(json.dumps(CONCRETE_PACK))
    del pack["criteria"][0]["notes"]
    errors, warnings = lint_pack_draft(pack)
    assert errors == []
    assert any("notes" in w for w in warnings)


def test_shipped_default_pack_has_no_errors_but_generic_warnings():
    """The default pack must stay installable (zero errors) while the wizard
    correctly identifies it as what it is: generic."""
    default = json.loads((_REPO / DEFAULT_PACK_RELPATH).read_text(encoding="utf-8"))
    errors, warnings = lint_pack_draft(default)
    assert errors == []
    assert any("generic" in w for w in warnings)


# ---------------------------------------------------------------------------
# build_match / merge_pack_config
# ---------------------------------------------------------------------------

def test_build_match_empty_applies_to_all():
    match = build_match(path_globs=[], tags=[], keywords=[], min_severity=None)
    assert match["mode"] == "all"


def test_build_match_with_globs_is_selective():
    match = build_match(path_globs=["auth/**"], tags=[], keywords=[], min_severity=None)
    assert match["mode"] == "selective"
    assert match["path_globs"] == ["auth/**"]


def test_merge_preserves_existing_config():
    existing = {
        "gate": {"blind_tdd": {"enabled": True, "routing": {"mode": "all"}}},
        "other_top_level": 42,
    }
    merged = merge_pack_config(
        existing, pack_path="themis.security_pack.json",
        match=build_match(path_globs=[], tags=[], keywords=[], min_severity=None),
    )
    btd = merged["gate"]["blind_tdd"]
    assert btd["security_ac_pack"]["enabled"] is True
    assert btd["security_ac_pack"]["pack_path"] == "themis.security_pack.json"
    assert btd["enabled"] is True                     # untouched
    assert btd["routing"] == {"mode": "all"}          # untouched
    assert merged["other_top_level"] == 42            # untouched
    assert "security_ac_pack" not in existing["gate"]["blind_tdd"]  # no mutation


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _write_draft(p: Path, pack: dict) -> Path:
    draft = p / "draft.json"
    draft.write_text(json.dumps(pack), encoding="utf-8")
    return draft


def test_cli_lint_clean_exits_zero(tmp_path, capsys):
    draft = _write_draft(tmp_path, CONCRETE_PACK)
    assert main(["--lint", str(draft)]) == 0
    assert "clean" in capsys.readouterr().out


def test_cli_lint_errors_exit_one(tmp_path, capsys):
    draft = _write_draft(tmp_path, {"criteria": []})
    assert main(["--lint", str(draft)]) == 1
    assert "ERROR" in capsys.readouterr().out


def test_cli_lint_missing_file_exits_one(tmp_path):
    assert main(["--lint", str(tmp_path / "nope.json")]) == 1


def test_cli_install_print_writes_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    draft = _write_draft(tmp_path, CONCRETE_PACK)
    assert main(["--install", str(draft), "--print"]) == 0
    assert not (tmp_path / DEFAULT_PACK_PATH).exists()
    assert not (tmp_path / "themis.config.json").exists()
    assert "security_ac_pack" in capsys.readouterr().out


def test_cli_install_yes_writes_pack_and_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "themis.config.json").write_text(
        json.dumps({"gate": {"blind_tdd": {"enabled": True}}}), encoding="utf-8",
    )
    draft = _write_draft(tmp_path, CONCRETE_PACK)
    assert main([
        "--install", str(draft), "--yes", "--match-glob", "auth/**",
    ]) == 0

    installed = json.loads((tmp_path / DEFAULT_PACK_PATH).read_text(encoding="utf-8"))
    assert installed == CONCRETE_PACK

    config = json.loads((tmp_path / "themis.config.json").read_text(encoding="utf-8"))
    sp = config["gate"]["blind_tdd"]["security_ac_pack"]
    assert sp["enabled"] is True
    assert sp["pack_path"] == DEFAULT_PACK_PATH
    assert sp["match"]["path_globs"] == ["auth/**"]
    assert config["gate"]["blind_tdd"]["enabled"] is True  # preserved


def test_cli_install_warnings_only_draft_still_installs(tmp_path, monkeypatch):
    """The engine blocks on errors, not warnings — accepting a generic pack is
    the human's call (the plugin command enforces zero-warnings as policy)."""
    monkeypatch.chdir(tmp_path)
    pack = json.loads(json.dumps(CONCRETE_PACK))
    pack["criteria"][0]["given"] = "any public entry point that accepts input"
    draft = _write_draft(tmp_path, pack)
    assert main(["--install", str(draft), "--yes"]) == 0
    assert (tmp_path / DEFAULT_PACK_PATH).exists()


def test_cli_install_interactive_abort_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    draft = _write_draft(tmp_path, CONCRETE_PACK)
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    assert main(["--install", str(draft)]) == 1
    assert not (tmp_path / DEFAULT_PACK_PATH).exists()
    assert not (tmp_path / "themis.config.json").exists()


def test_cli_install_interactive_default_is_yes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    draft = _write_draft(tmp_path, CONCRETE_PACK)
    monkeypatch.setattr("builtins.input", lambda *_: "")
    assert main(["--install", str(draft)]) == 0
    assert (tmp_path / DEFAULT_PACK_PATH).exists()


def test_merge_replaces_existing_pack_config():
    existing = {
        "gate": {"blind_tdd": {
            "enabled": True,
            "security_ac_pack": {
                "enabled": True, "pack_path": "old_pack.json",
                "match": {"mode": "all"},
            },
        }},
    }
    merged = merge_pack_config(
        existing, pack_path="themis.security_pack.json",
        match=build_match(path_globs=["auth/**"], tags=[], keywords=[], min_severity=None),
    )
    sp = merged["gate"]["blind_tdd"]["security_ac_pack"]
    assert sp["pack_path"] == "themis.security_pack.json"  # old block replaced whole
    assert sp["match"]["path_globs"] == ["auth/**"]
    assert merged["gate"]["blind_tdd"]["enabled"] is True


def test_cli_install_refuses_errored_draft(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    pack = json.loads(json.dumps(CONCRETE_PACK))
    pack["criteria"][0]["then"] = "it works nicely"
    draft = _write_draft(tmp_path, pack)
    assert main(["--install", str(draft), "--yes"]) == 1
    assert not (tmp_path / DEFAULT_PACK_PATH).exists()


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
