"""Tests for blind_tdd.schema_validator — closes the audit gap.

Covers every failure mode in validate_task:
- Missing/empty acceptance_criteria
- Criteria with missing id/given/when/then
- Duplicate criterion IDs
- Non-AC-N pattern warnings
- Missing/wrong-type public_surface fields
- Subjective-language warnings
- validate_or_raise / get_criterion_ids helpers

Run with:
    python -m blind_tdd.test_schema_validator
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_REPO = _HERE.parents[1]
sys.path.insert(0, str(_REPO))

from blind_tdd.schema_validator import (  # noqa: E402
    ValidationError,
    validate_task,
    validate_or_raise,
    get_criterion_ids,
)


def _valid_task() -> dict:
    return {
        "id": "task-x",
        "acceptance_criteria": [
            {"id": "AC-1", "given": "a", "when": "b", "then": "c"},
        ],
        "public_surface": {
            "module": "src.foo",
            "adds": ["foo()"],
        },
    }


# ---------------------------------------------------------------------------
# validate_task — happy path
# ---------------------------------------------------------------------------

def test_valid_task_passes():
    r = validate_task(_valid_task())
    assert r.valid
    assert r.errors == []


def test_multiple_criteria_with_unique_ids_passes():
    t = _valid_task()
    t["acceptance_criteria"] = [
        {"id": "AC-1", "given": "x", "when": "y", "then": "z"},
        {"id": "AC-2", "given": "p", "when": "q", "then": "r"},
    ]
    r = validate_task(t)
    assert r.valid


# ---------------------------------------------------------------------------
# Missing acceptance_criteria
# ---------------------------------------------------------------------------

def test_missing_acceptance_criteria_fails():
    t = _valid_task()
    del t["acceptance_criteria"]
    r = validate_task(t)
    assert not r.valid
    assert any("acceptance_criteria" in e for e in r.errors)


def test_empty_acceptance_criteria_fails():
    t = _valid_task()
    t["acceptance_criteria"] = []
    r = validate_task(t)
    assert not r.valid
    assert any("at least one" in e for e in r.errors)


def test_acceptance_criteria_wrong_type_fails():
    t = _valid_task()
    t["acceptance_criteria"] = "not a list"
    r = validate_task(t)
    assert not r.valid
    assert any("must be a list" in e for e in r.errors)


def test_criterion_not_a_dict_fails():
    t = _valid_task()
    t["acceptance_criteria"] = ["just a string"]
    r = validate_task(t)
    assert not r.valid
    assert any("must be an object" in e for e in r.errors)


# ---------------------------------------------------------------------------
# Missing criterion fields
# ---------------------------------------------------------------------------

def test_criterion_missing_id_fails():
    t = _valid_task()
    t["acceptance_criteria"] = [{"given": "a", "when": "b", "then": "c"}]
    r = validate_task(t)
    assert not r.valid
    assert any("id is required" in e for e in r.errors)


def test_criterion_missing_given_fails():
    t = _valid_task()
    t["acceptance_criteria"] = [{"id": "AC-1", "when": "b", "then": "c"}]
    r = validate_task(t)
    assert not r.valid
    assert any("given is required" in e for e in r.errors)


def test_criterion_missing_when_fails():
    t = _valid_task()
    t["acceptance_criteria"] = [{"id": "AC-1", "given": "a", "then": "c"}]
    r = validate_task(t)
    assert not r.valid
    assert any("when is required" in e for e in r.errors)


def test_criterion_missing_then_fails():
    t = _valid_task()
    t["acceptance_criteria"] = [{"id": "AC-1", "given": "a", "when": "b"}]
    r = validate_task(t)
    assert not r.valid
    assert any("then is required" in e for e in r.errors)


def test_criterion_empty_given_fails():
    t = _valid_task()
    t["acceptance_criteria"] = [{"id": "AC-1", "given": "   ", "when": "b", "then": "c"}]
    r = validate_task(t)
    assert not r.valid
    assert any("given must be a non-empty" in e for e in r.errors)


def test_duplicate_criterion_ids_fails():
    t = _valid_task()
    t["acceptance_criteria"] = [
        {"id": "AC-1", "given": "a", "when": "b", "then": "c"},
        {"id": "AC-1", "given": "d", "when": "e", "then": "f"},
    ]
    r = validate_task(t)
    assert not r.valid
    assert any("duplicated" in e for e in r.errors)


def test_non_ac_pattern_id_warns_but_valid():
    t = _valid_task()
    t["acceptance_criteria"] = [
        {"id": "criterion-1", "given": "a", "when": "b", "then": "c"},
    ]
    r = validate_task(t)
    # ID pattern mismatch is a warning, not an error
    assert r.valid
    assert any("AC-<number>" in w for w in r.warnings)


# ---------------------------------------------------------------------------
# Criterion examples / notes (optional fields)
# ---------------------------------------------------------------------------

def test_examples_wrong_type_fails():
    t = _valid_task()
    t["acceptance_criteria"][0]["examples"] = "not a list"
    r = validate_task(t)
    assert not r.valid
    assert any("examples must be a list" in e for e in r.errors)


def test_examples_entry_wrong_type_fails():
    t = _valid_task()
    t["acceptance_criteria"][0]["examples"] = [{"ok": True}, "bad"]
    r = validate_task(t)
    assert not r.valid
    assert any("examples[1]" in e for e in r.errors)


def test_notes_wrong_type_fails():
    t = _valid_task()
    t["acceptance_criteria"][0]["notes"] = ["list instead of string"]
    r = validate_task(t)
    assert not r.valid
    assert any("notes must be a string" in e for e in r.errors)


# ---------------------------------------------------------------------------
# public_surface
# ---------------------------------------------------------------------------

def test_missing_public_surface_fails():
    t = _valid_task()
    del t["public_surface"]
    r = validate_task(t)
    assert not r.valid
    assert any("public_surface" in e for e in r.errors)


def test_public_surface_wrong_type_fails():
    t = _valid_task()
    t["public_surface"] = "not an object"
    r = validate_task(t)
    assert not r.valid
    assert any("public_surface must be an object" in e for e in r.errors)


def test_public_surface_missing_module_fails():
    t = _valid_task()
    del t["public_surface"]["module"]
    r = validate_task(t)
    assert not r.valid
    assert any("public_surface.module is required" in e for e in r.errors)


def test_public_surface_missing_adds_fails():
    t = _valid_task()
    del t["public_surface"]["adds"]
    r = validate_task(t)
    assert not r.valid
    assert any("public_surface.adds is required" in e for e in r.errors)


def test_public_surface_adds_wrong_type_fails():
    t = _valid_task()
    t["public_surface"]["adds"] = "not a list"
    r = validate_task(t)
    assert not r.valid
    assert any("adds must be a list" in e for e in r.errors)


def test_public_surface_adds_entry_wrong_type_fails():
    t = _valid_task()
    t["public_surface"]["adds"] = ["valid", 123]
    r = validate_task(t)
    assert not r.valid
    assert any("adds[1] must be a string" in e for e in r.errors)


def test_public_surface_adds_empty_string_fails():
    t = _valid_task()
    t["public_surface"]["adds"] = [""]
    r = validate_task(t)
    assert not r.valid
    assert any("must be non-empty" in e for e in r.errors)


def test_public_surface_modifies_optional():
    t = _valid_task()
    t["public_surface"]["modifies"] = ["foo()", "bar()"]
    r = validate_task(t)
    assert r.valid


def test_public_surface_modifies_wrong_type_fails():
    t = _valid_task()
    t["public_surface"]["modifies"] = "not a list"
    r = validate_task(t)
    assert not r.valid


def test_public_surface_class_wrong_type_fails():
    t = _valid_task()
    t["public_surface"]["class"] = ["list", "not", "string"]
    r = validate_task(t)
    assert not r.valid


def test_public_surface_external_refs_wrong_type_fails():
    t = _valid_task()
    t["public_surface"]["external_refs"] = "not a list"
    r = validate_task(t)
    assert not r.valid


def test_public_surface_external_refs_non_url_warns():
    t = _valid_task()
    t["public_surface"]["external_refs"] = ["docs/foo.md"]
    r = validate_task(t)
    assert r.valid
    assert any("does not look like a URL" in w for w in r.warnings)


def test_public_surface_external_refs_https_ok():
    t = _valid_task()
    t["public_surface"]["external_refs"] = ["https://example.com/foo"]
    r = validate_task(t)
    assert r.valid
    assert not any("does not look like" in w for w in r.warnings)


def test_empty_adds_and_no_modifies_warns():
    t = _valid_task()
    t["public_surface"]["adds"] = []
    r = validate_task(t)
    assert r.valid  # only a warning
    assert any("may not need blind-TDD" in w for w in r.warnings)


# ---------------------------------------------------------------------------
# Subjective language detection
# ---------------------------------------------------------------------------

def test_subjective_language_in_then_warns():
    t = _valid_task()
    t["acceptance_criteria"] = [
        {"id": "AC-1", "given": "a", "when": "b",
         "then": "the animation plays smoothly and feels responsive"},
    ]
    r = validate_task(t)
    assert r.valid  # warning only
    assert any("subjective language" in w for w in r.warnings)


def test_no_subjective_language_no_warning():
    t = _valid_task()
    t["acceptance_criteria"] = [
        {"id": "AC-1", "given": "a", "when": "b",
         "then": "the position changes by exactly 1.0 units"},
    ]
    r = validate_task(t)
    assert r.valid
    assert not any("subjective language" in w for w in r.warnings)


# ---------------------------------------------------------------------------
# Multi-case phrasing without examples
# ---------------------------------------------------------------------------

def test_multi_case_phrasing_without_examples_warns():
    t = _valid_task()
    t["acceptance_criteria"] = [
        {"id": "AC-1", "given": "n", "when": "foo(n) is called",
         "then": "the result is correct for every n in the range 0 to 100"},
    ]
    r = validate_task(t)
    assert r.valid  # warning only
    assert any("multi-case phrasing" in w for w in r.warnings)


def test_multi_case_phrasing_with_examples_no_warning():
    t = _valid_task()
    t["acceptance_criteria"] = [
        {"id": "AC-1", "given": "n", "when": "foo(n) is called",
         "then": "the result is correct for every n",
         "examples": [{"n": 0, "expected": 0}, {"n": 1, "expected": 1}]},
    ]
    r = validate_task(t)
    assert r.valid
    assert not any("multi-case phrasing" in w for w in r.warnings)


def test_single_case_phrasing_no_multi_case_warning():
    t = _valid_task()
    # Default fixture uses "the result is 42" — no multi-case phrases
    r = validate_task(t)
    assert r.valid
    assert not any("multi-case phrasing" in w for w in r.warnings)


def test_multi_case_empty_examples_list_still_warns():
    """An empty examples: [] doesn't cover the cases — still warn."""
    t = _valid_task()
    t["acceptance_criteria"] = [
        {"id": "AC-1", "given": "x", "when": "y",
         "then": "works for every input outside the range",
         "examples": []},
    ]
    r = validate_task(t)
    assert r.valid
    assert any("multi-case phrasing" in w for w in r.warnings)


def test_multi_case_phrase_in_when_clause_triggers_warning():
    t = _valid_task()
    t["acceptance_criteria"] = [
        {"id": "AC-1", "given": "x",
         "when": "normalizes any input",
         "then": "returns True"},
    ]
    r = validate_task(t)
    assert r.valid
    assert any("multi-case phrasing" in w for w in r.warnings)


# ---------------------------------------------------------------------------
# Top-level edge cases
# ---------------------------------------------------------------------------

def test_task_not_a_dict_fails():
    r = validate_task("not a dict")  # type: ignore[arg-type]
    assert not r.valid
    assert any("task must be an object" in e for e in r.errors)


def test_task_missing_id_and_description_warns_but_valid():
    t = _valid_task()
    del t["id"]
    r = validate_task(t)
    assert r.valid
    assert any("neither 'id' nor 'description'" in w for w in r.warnings)


# ---------------------------------------------------------------------------
# validate_or_raise
# ---------------------------------------------------------------------------

def test_validate_or_raise_succeeds_on_valid():
    validate_or_raise(_valid_task())  # should not raise


def test_validate_or_raise_raises_on_invalid():
    bad = {"id": "x"}  # missing required fields
    try:
        validate_or_raise(bad)
    except ValidationError as e:
        assert len(e.errors) >= 2  # missing criteria + missing surface
        return
    assert False, "should have raised ValidationError"


# ---------------------------------------------------------------------------
# get_criterion_ids
# ---------------------------------------------------------------------------

def test_get_criterion_ids_returns_all_ids():
    t = _valid_task()
    t["acceptance_criteria"] = [
        {"id": "AC-1", "given": "a", "when": "b", "then": "c"},
        {"id": "AC-2", "given": "d", "when": "e", "then": "f"},
    ]
    assert get_criterion_ids(t) == ["AC-1", "AC-2"]


def test_get_criterion_ids_handles_malformed():
    assert get_criterion_ids({"acceptance_criteria": "not a list"}) == []
    assert get_criterion_ids({}) == []
    assert get_criterion_ids({"acceptance_criteria": [{"no_id": True}, {"id": "AC-1"}]}) == ["AC-1"]


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
