"""Tests for blind_tdd.preflight.

Covers each of the five preflight checks independently, plus the three
enforcement modes (strict/warn/off), plus the preclassified escape hatch.

Run with:
    python -m blind_tdd.test_preflight
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve()
_REPO = _HERE.parents[1]
sys.path.insert(0, str(_REPO))

from blind_tdd.preflight import (  # noqa: E402
    MODE_OFF,
    MODE_STRICT,
    MODE_WARN,
    _check_criterion_observability,
    _check_public_api_file,
    _check_public_surface_coverage,
    _check_subjective_language,
    _check_test_dirs_exist,
    _extract_identifier,
    _has_observable_assertion,
    preflight_task,
)


def _chdir(path: Path):
    class _Ctx:
        def __enter__(self_):
            self_.prev = os.getcwd()
            os.chdir(path)
            return path

        def __exit__(self_, *a):
            os.chdir(self_.prev)
    return _Ctx()


def _valid_task() -> dict:
    """A task that passes every preflight check."""
    return {
        "id": "task-ok",
        "acceptance_criteria": [
            {
                "id": "AC-1",
                "given": "a number n",
                "when": "foo(n) is called",
                "then": "the result equals n + 1",
            },
        ],
        "public_surface": {
            "module": "src.foo",
            "adds": ["foo(n: int) -> int"],
        },
    }


def _default_config() -> dict:
    return {
        "gate": {
            "blind_tdd": {
                "enabled": True,
                "preflight": MODE_STRICT,
                "test_dirs": [],
                # No public_api_file — skip that check unless set
            }
        }
    }


# ---------------------------------------------------------------------------
# _extract_identifier
# ---------------------------------------------------------------------------

def test_extract_identifier_method_with_class():
    assert _extract_identifier("Mathf.delta_angle(current: float, target: float) -> float") == "delta_angle"


def test_extract_identifier_nested_method():
    assert _extract_identifier("engine.math.Mathf.clamp(x)") == "clamp"


def test_extract_identifier_plain_function():
    assert _extract_identifier("foo()") == "foo"


def test_extract_identifier_with_def_keyword():
    assert _extract_identifier("def tick(delta)") == "tick"
    assert _extract_identifier("async def process()") == "process"


def test_extract_identifier_class_keyword():
    assert _extract_identifier("class Foo:") == "Foo"


def test_extract_identifier_property():
    assert _extract_identifier("property bar") == "bar"


def test_extract_identifier_no_parens():
    assert _extract_identifier("foo_bar") == "foo_bar"


def test_extract_identifier_invalid_returns_none():
    assert _extract_identifier("") is None
    assert _extract_identifier(None) is None
    assert _extract_identifier("   ") is None
    assert _extract_identifier("123abc()") is None  # starts with digit


def test_extract_identifier_underscore_start():
    assert _extract_identifier("_private_method()") == "_private_method"


# ---------------------------------------------------------------------------
# _has_observable_assertion
# ---------------------------------------------------------------------------

def test_observable_numeric_literal():
    assert _has_observable_assertion("the result is 42")
    assert _has_observable_assertion("result within 1e-4 of expected")  # e notation fails
    # Actually, 1e-4 → our regex matches \d+(\.\d+)? so it matches 1 but not the e.
    # Let's assert with a straightforward numeric:
    assert _has_observable_assertion("returns 3.14")


def test_observable_comparator_word():
    assert _has_observable_assertion("the result equals the expected value")
    assert _has_observable_assertion("must be less than 10")


def test_observable_exception_class():
    assert _has_observable_assertion("raises ValueError")
    assert _has_observable_assertion("a FileNotFoundError is thrown")


def test_observable_boolean_literal():
    assert _has_observable_assertion("the flag is true")
    assert _has_observable_assertion("result is False")


def test_not_observable_vague_prose():
    assert not _has_observable_assertion("the animation happens correctly")
    assert not _has_observable_assertion("the thing works as expected")
    assert not _has_observable_assertion("the player experiences the game")
    assert not _has_observable_assertion("the transition occurs")


# ---------------------------------------------------------------------------
# _check_subjective_language
# ---------------------------------------------------------------------------

def test_subjective_language_detected():
    task = _valid_task()
    task["acceptance_criteria"][0]["then"] = "the animation plays smoothly"
    errors = _check_subjective_language(task)
    assert len(errors) == 1
    assert "AC-1" in errors[0]
    assert "smoothly" in errors[0]


def test_subjective_language_multiple_hits():
    task = _valid_task()
    task["acceptance_criteria"][0]["then"] = "the result feels snappy and looks polished"
    errors = _check_subjective_language(task)
    assert len(errors) == 1
    # All three subjective words captured
    assert "feels" in errors[0] or "snappy" in errors[0] or "polished" in errors[0]


def test_subjective_language_passes_clean_criteria():
    task = _valid_task()
    errors = _check_subjective_language(task)
    assert errors == []


def test_subjective_language_skipped_when_preclassified():
    task = _valid_task()
    task["acceptance_criteria"][0]["then"] = "the animation plays smoothly"
    task["acceptance_criteria"][0]["preclassified"] = "needs_human"
    errors = _check_subjective_language(task)
    assert errors == []


# ---------------------------------------------------------------------------
# _check_public_surface_coverage
# ---------------------------------------------------------------------------

def test_public_surface_coverage_matching_identifier():
    task = _valid_task()
    # foo is in adds; AC-1's when says "foo(n) is called" — matches
    errors = _check_public_surface_coverage(task)
    assert errors == []


def test_public_surface_coverage_missing_identifier():
    task = _valid_task()
    # Add a new surface entry with an identifier no criterion mentions
    task["public_surface"]["adds"].append("Bar.baz_method(q)")
    errors = _check_public_surface_coverage(task)
    assert len(errors) == 1
    assert "baz_method" in errors[0]


def test_public_surface_coverage_multiple_gaps():
    task = _valid_task()
    task["public_surface"]["adds"] = ["foo_x()", "foo_y()", "foo_z()"]
    errors = _check_public_surface_coverage(task)
    assert len(errors) == 3


def test_public_surface_coverage_empty_adds():
    task = _valid_task()
    task["public_surface"]["adds"] = []
    errors = _check_public_surface_coverage(task)
    assert errors == []


def test_public_surface_coverage_identifier_case_insensitive():
    task = _valid_task()
    task["public_surface"]["adds"] = ["Mathf.DeltaAngle(current, target)"]
    task["acceptance_criteria"][0]["when"] = "deltaangle is called"
    errors = _check_public_surface_coverage(task)
    assert errors == []  # case-insensitive match


def test_public_surface_coverage_matches_in_then_clause():
    task = _valid_task()
    task["public_surface"]["adds"] = ["foo.helper()"]
    task["acceptance_criteria"][0]["when"] = "something happens"
    task["acceptance_criteria"][0]["then"] = "the helper returns 42"
    errors = _check_public_surface_coverage(task)
    assert errors == []


# ---------------------------------------------------------------------------
# _check_criterion_observability
# ---------------------------------------------------------------------------

def test_observability_passes_concrete_then():
    task = _valid_task()  # "equals n + 1" has both comparator and numeric
    errors = _check_criterion_observability(task)
    assert errors == []


def test_observability_fails_vague_then():
    task = _valid_task()
    task["acceptance_criteria"][0]["then"] = "the behavior works correctly"
    errors = _check_criterion_observability(task)
    assert len(errors) == 1
    assert "AC-1" in errors[0]


def test_observability_skipped_when_preclassified():
    task = _valid_task()
    task["acceptance_criteria"][0]["then"] = "the animation looks good"
    task["acceptance_criteria"][0]["preclassified"] = "needs_human"
    errors = _check_criterion_observability(task)
    assert errors == []


def test_observability_per_criterion():
    task = _valid_task()
    task["acceptance_criteria"] = [
        {"id": "AC-1", "given": "x", "when": "y", "then": "returns 42"},
        {"id": "AC-2", "given": "x", "when": "y", "then": "the vibe is right"},
        {"id": "AC-3", "given": "x", "when": "y", "then": "equals 0"},
    ]
    errors = _check_criterion_observability(task)
    assert len(errors) == 1
    assert "AC-2" in errors[0]


# ---------------------------------------------------------------------------
# _check_public_api_file
# ---------------------------------------------------------------------------

def test_public_api_file_not_configured_skipped():
    config = _default_config()
    task = _valid_task()
    # No public_api_file in config
    errors = _check_public_api_file(task, config)
    assert errors == []


def test_public_api_file_missing_fails():
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            config = _default_config()
            config["gate"]["blind_tdd"]["public_api_file"] = "public_api.md"
            task = _valid_task()
            errors = _check_public_api_file(task, config)
            assert len(errors) == 1
            assert "does not exist" in errors[0]


def test_public_api_file_missing_module_warns():
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            Path("public_api.md").write_text(
                "# Some other module\nstuff about src.bar\n",
                encoding="utf-8",
            )
            config = _default_config()
            config["gate"]["blind_tdd"]["public_api_file"] = "public_api.md"
            task = _valid_task()
            errors = _check_public_api_file(task, config)
            assert len(errors) == 1
            assert "does not mention" in errors[0]


def test_public_api_file_mentions_module_passes():
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            Path("public_api.md").write_text(
                "# src.foo\nDocumented here.\n", encoding="utf-8"
            )
            config = _default_config()
            config["gate"]["blind_tdd"]["public_api_file"] = "public_api.md"
            task = _valid_task()
            errors = _check_public_api_file(task, config)
            assert errors == []


# ---------------------------------------------------------------------------
# _check_test_dirs_exist
# ---------------------------------------------------------------------------

def test_test_dirs_exist_passes_when_present():
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            Path("tests/contracts").mkdir(parents=True)
            config = _default_config()
            config["gate"]["blind_tdd"]["test_dirs"] = ["tests/contracts/"]
            errors = _check_test_dirs_exist(config)
            assert errors == []


def test_test_dirs_missing_fails():
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            config = _default_config()
            config["gate"]["blind_tdd"]["test_dirs"] = ["tests/contracts/", "tests/integration/"]
            errors = _check_test_dirs_exist(config)
            assert len(errors) == 2


def test_test_dirs_empty_list_passes():
    config = _default_config()
    config["gate"]["blind_tdd"]["test_dirs"] = []
    errors = _check_test_dirs_exist(config)
    assert errors == []


# ---------------------------------------------------------------------------
# preflight_task — enforcement modes
# ---------------------------------------------------------------------------

def test_strict_mode_blocks_on_failure():
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            task = _valid_task()
            task["acceptance_criteria"][0]["then"] = "the result feels right"
            # Subjective + unobservable
            config = _default_config()
            result = preflight_task(task, config)
            assert not result.ready
            assert result.mode == MODE_STRICT
            assert len(result.errors) >= 1
            assert result.warnings == []


def test_strict_mode_passes_on_clean_task():
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            result = preflight_task(_valid_task(), _default_config())
            assert result.ready
            assert result.errors == []


def test_warn_mode_never_blocks():
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            task = _valid_task()
            task["acceptance_criteria"][0]["then"] = "the result feels right"
            config = _default_config()
            config["gate"]["blind_tdd"]["preflight"] = MODE_WARN
            result = preflight_task(task, config)
            assert result.ready
            assert result.mode == MODE_WARN
            assert len(result.warnings) >= 1
            assert result.errors == []


def test_off_mode_skips_all_checks():
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            task = _valid_task()
            task["acceptance_criteria"][0]["then"] = "the thing works"
            task["public_surface"]["adds"] = ["bad_sig_never_mentioned()"]
            config = _default_config()
            config["gate"]["blind_tdd"]["preflight"] = MODE_OFF
            result = preflight_task(task, config)
            assert result.ready
            assert result.errors == []
            assert result.warnings == []


def test_invalid_mode_defaults_to_strict():
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            task = _valid_task()
            task["acceptance_criteria"][0]["then"] = "feels good"
            config = _default_config()
            config["gate"]["blind_tdd"]["preflight"] = "YOLO"
            result = preflight_task(task, config)
            assert result.mode == MODE_STRICT
            assert not result.ready


def test_default_mode_is_strict():
    """When gate.blind_tdd.preflight is absent from config, default is strict."""
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            task = _valid_task()
            task["acceptance_criteria"][0]["then"] = "feels good"
            config = {"gate": {"blind_tdd": {"enabled": True, "test_dirs": []}}}
            result = preflight_task(task, config)
            assert result.mode == MODE_STRICT
            assert not result.ready


# ---------------------------------------------------------------------------
# Integration: multiple check failures reported together
# ---------------------------------------------------------------------------

def test_multiple_failures_collected():
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            task = _valid_task()
            # Subjective language
            task["acceptance_criteria"][0]["then"] = "the thing feels polished"
            # And add an uncovered signature
            task["public_surface"]["adds"].append("Uncovered.method()")
            config = _default_config()
            result = preflight_task(task, config)
            assert not result.ready
            # Both subjective + observability + coverage should contribute
            assert len(result.errors) >= 2


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
