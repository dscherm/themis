"""Tests for the security AC pack (security_pack.py).

The pack is operator-authored spec content, so the properties that matter
are trust-boundary properties: injection is deterministic (red and green
land on the same augmented task), the original task is never mutated, the
fingerprint pins the pack content, and the shipped default pack is valid
and survives schema validation + preflight once injected.
"""

from __future__ import annotations

import json
from pathlib import Path

from blind_tdd.preflight import preflight_task
from blind_tdd.schema_validator import validate_task
from blind_tdd.security_pack import (
    DEFAULT_PACK_RELPATH,
    apply_pack,
    load_pack,
    normalize_pack_config,
    pack_fingerprint,
    resolve_pack_path,
    validate_pack,
)

_REPO = Path(__file__).resolve().parents[1]
_DEFAULT_PACK = _REPO / DEFAULT_PACK_RELPATH


PACK = {
    "version": 7,
    "criteria": [
        {
            "category": "input-validation",
            "given": "an entry point",
            "when": "called with oversized input",
            "then": "it raises ValueError",
            "notes": "instantiate concretely",
        },
        {
            "category": "secrets",
            "given": "the implementation files",
            "when": "scanned for credentials",
            "then": "the scan matches 0 patterns",
        },
    ],
}

TASK = {
    "id": "task-9",
    "title": "Add parser",
    "files": ["src/parser.py"],
    "tags": ["security"],
    "public_surface": {"module": "src.parser", "adds": ["parse"]},
    "acceptance_criteria": [
        {"id": "AC-1", "given": "g", "when": "parse is called", "then": "parse returns True"},
        {"id": "AC-3", "given": "g", "when": "parse is called twice", "then": "parse returns False"},
    ],
}


# ---------------------------------------------------------------------------
# normalize / validate / load
# ---------------------------------------------------------------------------

def test_normalize_defaults_off_and_match_all():
    cfg = normalize_pack_config(None)
    assert cfg["enabled"] is False
    assert cfg["pack_path"] is None
    # No predicates → routing-normalized "all": pack applies to every gated task
    assert cfg["match"]["mode"] == "all"


def test_normalize_with_predicates_is_selective():
    cfg = normalize_pack_config({"enabled": True, "match": {"tags": ["security"]}})
    assert cfg["enabled"] is True
    assert cfg["match"]["mode"] == "selective"
    assert cfg["match"]["tags"] == ["security"]


def test_validate_pack_catches_structural_problems():
    assert validate_pack([]) != []
    assert any("version" in e for e in validate_pack({"criteria": [{"given": "g", "when": "w", "then": "t"}]}))
    assert any("criteria" in e for e in validate_pack({"version": 1, "criteria": []}))
    assert any(".then" in e for e in validate_pack(
        {"version": 1, "criteria": [{"given": "g", "when": "w", "then": ""}]}
    ))


def test_load_pack_missing_file_errors(tmp_path: Path):
    pack, err = load_pack(tmp_path / "nope.json")
    assert pack is None and "not found" in err


def test_load_pack_bad_json_errors(tmp_path: Path):
    p = tmp_path / "pack.json"
    p.write_text("{broken", encoding="utf-8")
    pack, err = load_pack(p)
    assert pack is None and "unreadable" in err


def test_resolve_pack_path_explicit_wins(monkeypatch):
    monkeypatch.setenv("THEMIS_HOME", "/some/home")
    assert resolve_pack_path("my/pack.json") == Path("my/pack.json")


def test_resolve_pack_path_falls_back_to_package(monkeypatch):
    monkeypatch.delenv("THEMIS_HOME", raising=False)
    monkeypatch.delenv("RALPH_HOME", raising=False)
    assert resolve_pack_path(None) == _DEFAULT_PACK


# ---------------------------------------------------------------------------
# apply_pack — determinism and trust properties
# ---------------------------------------------------------------------------

def test_apply_pack_continues_ac_numbering():
    augmented, injected = apply_pack(TASK, PACK)
    # Task's max is AC-3 → pack criteria become AC-4, AC-5
    assert injected == ["AC-4", "AC-5"]
    ids = [c["id"] for c in augmented["acceptance_criteria"]]
    assert ids == ["AC-1", "AC-3", "AC-4", "AC-5"]


def test_apply_pack_is_deterministic():
    a1, i1 = apply_pack(TASK, PACK)
    a2, i2 = apply_pack(TASK, PACK)
    assert a1 == a2 and i1 == i2


def test_apply_pack_does_not_mutate_original():
    before = json.dumps(TASK, sort_keys=True)
    apply_pack(TASK, PACK)
    assert json.dumps(TASK, sort_keys=True) == before


def test_apply_pack_tags_notes_with_provenance():
    augmented, _ = apply_pack(TASK, PACK)
    by_id = {c["id"]: c for c in augmented["acceptance_criteria"]}
    assert "[themis-security-pack v7: input-validation]" in by_id["AC-4"]["notes"]
    assert "instantiate concretely" in by_id["AC-4"]["notes"]
    assert "[themis-security-pack v7: secrets]" in by_id["AC-5"]["notes"]


def test_fingerprint_stable_and_content_sensitive():
    assert pack_fingerprint(PACK) == pack_fingerprint(json.loads(json.dumps(PACK)))
    weakened = json.loads(json.dumps(PACK))
    weakened["criteria"].pop()
    assert pack_fingerprint(weakened) != pack_fingerprint(PACK)


# ---------------------------------------------------------------------------
# The shipped default pack must survive the gate's own quality checks
# ---------------------------------------------------------------------------

def test_default_pack_loads_and_validates():
    pack, err = load_pack(_DEFAULT_PACK)
    assert pack is not None, err
    assert pack.get("version")
    assert len(pack["criteria"]) >= 4


def test_default_pack_injection_passes_schema_and_preflight(tmp_path: Path, monkeypatch):
    """An augmented task must not be rejected by the gate's own validators —
    otherwise enabling the pack bricks every matching task."""
    pack, err = load_pack(_DEFAULT_PACK)
    assert pack is not None, err
    augmented, injected = apply_pack(TASK, pack)
    assert len(injected) == len(pack["criteria"])

    vr = validate_task(augmented)
    assert vr.valid, vr.errors

    # Preflight needs public_api.md + test dirs on disk to pass its
    # environment checks; give it a minimal project.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "public_api.md").write_text("## src.parser\nparse\n", encoding="utf-8")
    (tmp_path / "tests" / "contracts").mkdir(parents=True)
    (tmp_path / "tests" / "integration").mkdir(parents=True)
    pf = preflight_task(augmented, {"gate": {"blind_tdd": {"preflight": "strict"}}})
    assert pf.ready, pf.errors


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
