"""Tests for the advisory suppression-marker audit (suppression.py).

The failure mode this guards: AI-written code masking a problem instead of
fixing it — a leaked secret left in place behind a `# nosec`, a lint error
silenced with `# noqa`. Detection is a red-baseline / green-diff, so these
tests cover the scanner's pattern set, the repo walk's pruning, and the
diff semantics (new markers flagged, pre-existing and removed ones not).
"""

from __future__ import annotations

from pathlib import Path

from blind_tdd.suppression import (
    diff_suppressions,
    scan_repo,
    scan_text,
    summarize_findings,
)


# ---------------------------------------------------------------------------
# scan_text — pattern set
# ---------------------------------------------------------------------------

def test_detects_python_markers():
    text = (
        "x = secret  # noqa: E501\n"
        "y = password  # nosec B105\n"
        "z = 1  # type: ignore[assignment]\n"
        "w = 2  # pylint: disable=invalid-name\n"
        "def dead():  # pragma: no cover\n"
        "    pass\n"
    )
    counts = scan_text(text)
    assert counts["noqa"] == 1
    assert counts["nosec"] == 1
    assert counts["type-ignore"] == 1
    assert counts["pylint-disable"] == 1
    assert counts["no-cover"] == 1


def test_detects_js_ts_markers():
    text = (
        "// eslint-disable-next-line no-eval\n"
        "/* eslint-disable */\n"
        "// @ts-ignore\n"
        "// @ts-expect-error\n"
        "/* istanbul ignore next */\n"
    )
    counts = scan_text(text)
    assert counts["eslint-disable"] == 2
    assert counts["ts-ignore"] == 2
    assert counts["istanbul-ignore"] == 1


def test_detects_other_ecosystem_markers():
    text = (
        '@SuppressWarnings("unchecked")\n'
        "#pragma warning disable CS8618\n"
        "#pragma warning(disable: 4996)\n"
        "x, _ := f() //nolint:errcheck\n"
        "risky()  # nosemgrep\n"
        "def m; end # rubocop:disable Style/For\n"
    )
    counts = scan_text(text)
    assert counts["suppress-warnings"] == 1
    assert counts["pragma-warning-disable"] == 2
    assert counts["nolint"] == 1
    assert counts["nosemgrep"] == 1
    assert counts["rubocop-disable"] == 1


def test_clean_text_yields_empty():
    assert scan_text("def add(a, b):\n    return a + b\n") == {}


def test_case_insensitive():
    counts = scan_text("x = 1  # NOQA\ny = 2  # NoSec\n")
    assert counts["noqa"] == 1
    assert counts["nosec"] == 1


# ---------------------------------------------------------------------------
# scan_repo — walk, pruning, suffixes
# ---------------------------------------------------------------------------

def test_scan_repo_finds_markers_with_relative_paths(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x = 1  # noqa\n", encoding="utf-8")
    (tmp_path / "clean.py").write_text("y = 2\n", encoding="utf-8")
    result = scan_repo(tmp_path)
    assert result == {"src/app.py": {"noqa": 1}}


def test_scan_repo_skips_artifact_and_dot_dirs(tmp_path: Path):
    for d in ("node_modules", ".git", "__pycache__", ".venv", "vendor"):
        (tmp_path / d).mkdir()
        (tmp_path / d / "x.py").write_text("# noqa\n", encoding="utf-8")
    assert scan_repo(tmp_path) == {}


def test_scan_repo_skips_non_source_suffixes(tmp_path: Path):
    (tmp_path / "notes.md").write_text("# noqa everywhere\n", encoding="utf-8")
    (tmp_path / "data.json").write_text('{"k": "# nosec"}\n', encoding="utf-8")
    assert scan_repo(tmp_path) == {}


def test_scan_repo_deterministic(tmp_path: Path):
    (tmp_path / "a.py").write_text("# noqa\n", encoding="utf-8")
    (tmp_path / "b.js").write_text("// eslint-disable-next-line\n", encoding="utf-8")
    assert scan_repo(tmp_path) == scan_repo(tmp_path)


# ---------------------------------------------------------------------------
# diff_suppressions — the red/green semantics
# ---------------------------------------------------------------------------

def test_new_marker_in_new_file_is_flagged():
    findings = diff_suppressions({}, {"src/app.py": {"nosec": 1}})
    assert findings == [
        {"path": "src/app.py", "marker": "nosec", "baseline": 0, "current": 1}
    ]


def test_count_increase_is_flagged():
    findings = diff_suppressions(
        {"src/app.py": {"noqa": 1}},
        {"src/app.py": {"noqa": 3}},
    )
    assert findings == [
        {"path": "src/app.py", "marker": "noqa", "baseline": 1, "current": 3}
    ]


def test_preexisting_marker_not_flagged():
    baseline = {"src/app.py": {"noqa": 2}}
    assert diff_suppressions(baseline, {"src/app.py": {"noqa": 2}}) == []


def test_removed_marker_not_flagged():
    baseline = {"src/app.py": {"noqa": 2, "nosec": 1}}
    assert diff_suppressions(baseline, {"src/app.py": {"noqa": 1}}) == []
    assert diff_suppressions(baseline, {}) == []


def test_malformed_baseline_treated_as_empty():
    findings = diff_suppressions(None, {"a.py": {"noqa": 1}})  # type: ignore[arg-type]
    assert len(findings) == 1


def test_summarize_findings_readable():
    findings = diff_suppressions({}, {"a.py": {"nosec": 2}, "b.js": {"eslint-disable": 1}})
    s = summarize_findings(findings)
    assert "a.py: +2 nosec" in s
    assert "b.js: +1 eslint-disable" in s


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
