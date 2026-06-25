"""Tests for blind-TDD task routing (which tasks the gate engages on)."""

from __future__ import annotations

from blind_tdd.routing import (
    RoutingDecision,
    evaluate_routing,
    normalize_routing,
    routing_warnings,
    task_paths,
    task_text,
)


# --------------------------------------------------------------------------- #
# normalize_routing
# --------------------------------------------------------------------------- #

def test_normalize_none_is_mode_all():
    """No routing block → gate everything (backward compatible)."""
    r = normalize_routing(None)
    assert r["mode"] == "all"
    assert r["path_globs"] == [] and r["tags"] == [] and r["keywords"] == []
    assert r["min_severity"] is None


def test_normalize_predicates_default_to_selective():
    r = normalize_routing({"tags": ["security"]})
    assert r["mode"] == "selective"


def test_normalize_explicit_mode_wins():
    r = normalize_routing({"mode": "all", "tags": ["security"]})
    assert r["mode"] == "all"


def test_normalize_unknown_mode_falls_back():
    assert normalize_routing({"mode": "weird"})["mode"] == "all"
    assert normalize_routing({"mode": "weird", "keywords": ["x"]})["mode"] == "selective"


def test_normalize_drops_blank_and_bad_severity():
    r = normalize_routing({"tags": ["", "  "], "min_severity": "nonsense"})
    assert r["tags"] == []
    assert r["min_severity"] is None
    # with no real predicate, mode collapses to "all"
    assert r["mode"] == "all"


def test_routing_warnings_flags_selective_with_no_predicates():
    r = {"mode": "selective", "path_globs": [], "tags": [], "keywords": [], "min_severity": None}
    assert routing_warnings(r)
    assert not routing_warnings(normalize_routing({"tags": ["x"]}))


# --------------------------------------------------------------------------- #
# mode: all
# --------------------------------------------------------------------------- #

def test_mode_all_gates_every_task():
    r = normalize_routing(None)
    assert evaluate_routing({"id": "t1"}, r).gate is True


# --------------------------------------------------------------------------- #
# path globs
# --------------------------------------------------------------------------- #

def test_path_glob_matches_files_list():
    r = normalize_routing({"path_globs": ["billing/**"]})
    task = {"id": "t1", "files": ["billing/charge.py"]}
    d = evaluate_routing(task, r)
    assert d.gate is True
    assert any("path:billing/**" in m for m in d.matched)


def test_path_glob_double_star_matches_nested():
    r = normalize_routing({"path_globs": ["billing/**"]})
    assert evaluate_routing({"files": ["billing/sub/deep/x.py"]}, r).gate is True


def test_path_glob_double_star_slash_matches_zero_dirs():
    r = normalize_routing({"path_globs": ["**/secrets.py"]})
    assert evaluate_routing({"files": ["secrets.py"]}, r).gate is True
    assert evaluate_routing({"files": ["a/b/secrets.py"]}, r).gate is True


def test_single_star_does_not_cross_slash():
    r = normalize_routing({"path_globs": ["billing/*.py"]})
    assert evaluate_routing({"files": ["billing/charge.py"]}, r).gate is True
    assert evaluate_routing({"files": ["billing/sub/charge.py"]}, r).gate is False


def test_path_glob_matches_dotted_module():
    r = normalize_routing({"path_globs": ["src/auth/**"]})
    task = {"public_surface": {"module": "src.auth.tokens", "adds": ["def mint()"]}}
    assert evaluate_routing(task, r).gate is True


def test_path_glob_no_match_is_excluded():
    r = normalize_routing({"path_globs": ["billing/**"]})
    d = evaluate_routing({"files": ["ui/button.py"]}, r)
    assert d.gate is False
    assert "matched no predicate" in d.reason


def test_task_paths_normalizes_backslashes_and_dotslash():
    paths = task_paths({"files": ["./billing\\charge.py"]})
    assert "billing/charge.py" in paths


# --------------------------------------------------------------------------- #
# tags
# --------------------------------------------------------------------------- #

def test_tag_match_case_insensitive():
    r = normalize_routing({"tags": ["High-Stakes"]})
    assert evaluate_routing({"tags": ["high-stakes"]}, r).gate is True


def test_tag_no_match_excluded():
    r = normalize_routing({"tags": ["security"]})
    assert evaluate_routing({"tags": ["chore"]}, r).gate is False


# --------------------------------------------------------------------------- #
# severity
# --------------------------------------------------------------------------- #

def test_severity_at_or_above_threshold_gates():
    r = normalize_routing({"min_severity": "high"})
    assert evaluate_routing({"severity": "high"}, r).gate is True
    assert evaluate_routing({"severity": "critical"}, r).gate is True


def test_severity_below_threshold_excluded():
    r = normalize_routing({"min_severity": "high"})
    assert evaluate_routing({"severity": "medium"}, r).gate is False


def test_missing_severity_does_not_match():
    r = normalize_routing({"min_severity": "high"})
    assert evaluate_routing({"id": "t1"}, r).gate is False


# --------------------------------------------------------------------------- #
# keywords
# --------------------------------------------------------------------------- #

def test_keyword_matches_description_and_criteria():
    r = normalize_routing({"keywords": ["password"]})
    task = {
        "id": "t1",
        "acceptance_criteria": [
            {"id": "AC-1", "given": "a user", "when": "they reset their PASSWORD", "then": "ok"}
        ],
    }
    d = evaluate_routing(task, r)
    assert d.gate is True
    assert any("keyword:password" in m for m in d.matched)


def test_task_text_lowercases_and_joins():
    blob = task_text({"description": "Handle Payment", "id": "PAY-1"})
    assert "payment" in blob and "pay-1" in blob


# --------------------------------------------------------------------------- #
# OR across predicate groups + edge cases
# --------------------------------------------------------------------------- #

def test_any_predicate_group_matching_gates():
    r = normalize_routing({"path_globs": ["billing/**"], "tags": ["security"]})
    # matches via tag even though path doesn't
    assert evaluate_routing({"files": ["ui/x.py"], "tags": ["security"]}, r).gate is True


def test_selective_with_no_predicates_gates_nothing():
    r = {"mode": "selective", "path_globs": [], "tags": [], "keywords": [], "min_severity": None}
    d = evaluate_routing({"id": "t1"}, r)
    assert d.gate is False
    assert "no predicates" in d.reason


def test_non_dict_task_gates_by_default():
    assert evaluate_routing("not-a-task", normalize_routing({"tags": ["x"]})).gate is True  # type: ignore[arg-type]


def test_decision_is_dataclass():
    d = evaluate_routing({"id": "t"}, normalize_routing(None))
    assert isinstance(d, RoutingDecision)


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
