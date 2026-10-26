"""Quality-review briefs scan only the task's own tests (RQ-1)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from blind_tdd import gate_integration as gi

EMPTY = '"""Bead: {bead}"""\n\n\ndef test_x():\n    pass\n'


def _project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("THEMIS_HOME", raising=False)
    monkeypatch.delenv("RALPH_HOME", raising=False)
    tdir = tmp_path / "tests"
    tdir.mkdir()
    (tdir / "test_a.py").write_text(EMPTY.format(bead="T1"), encoding="utf-8")
    (tdir / "test_b.py").write_text(EMPTY.format(bead="T2"), encoding="utf-8")


def _candidates(tmp_path: Path, task_id: str) -> tuple[list[dict], str]:
    brief = tmp_path / ".themis/blind_tdd/quality_review_pending" / f"{task_id}.md"
    text = brief.read_text(encoding="utf-8")
    block = re.search(r"```json\n(.*?)\n```", text, re.S).group(1)
    return json.loads(block)["candidates"], text


def test_scan_only_the_tasks_green_report_files(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    gr = tmp_path / ".themis/blind_tdd/green_report"
    gr.mkdir(parents=True)
    (gr / "T1.json").write_text(
        json.dumps({"per_test": [{"name": "test_x", "result": "pass", "file": "tests/test_a.py"}]}),
        encoding="utf-8",
    )
    r = gi._run_quality_review_phase({}, "T1", ["tests"])
    assert r is not None and r.phase == "quality_review_pending"
    cands, text = _candidates(tmp_path, "T1")
    assert cands
    assert all(c["file"] == "tests/test_a.py" for c in cands)
    assert not any("test_b.py" in c["file"] for c in cands)
    assert "fell back to all test_dirs" not in text


def test_triage_and_bead_docstring_also_scope(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    tr = tmp_path / ".themis/blind_tdd/triage"
    tr.mkdir(parents=True)
    (tr / "T2.json").write_text(
        json.dumps({"writer_hash_test_files": {"tests/test_b.py": "h"}}), encoding="utf-8"
    )
    gi._run_quality_review_phase({}, "T2", ["tests"])
    cands, _ = _candidates(tmp_path, "T2")
    assert {c["file"] for c in cands} == {"tests/test_b.py"}
    # Bead docstring alone (no reports) also scopes, without falling back.
    gi._run_quality_review_phase({}, "T1", ["tests"])
    cands, text = _candidates(tmp_path, "T1")
    assert {c["file"] for c in cands} == {"tests/test_a.py"}
    assert "fell back to all test_dirs" not in text


def test_fallback_scans_all_and_says_so(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    gi._run_quality_review_phase({}, "NOPE", ["tests"])
    cands, text = _candidates(tmp_path, "NOPE")
    assert {c["file"] for c in cands} == {"tests/test_a.py", "tests/test_b.py"}
    assert "fell back to all test_dirs" in text


def test_every_candidate_has_function_and_posix_file(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    abs_dir = str(tmp_path / "tests")
    gi._run_quality_review_phase({}, "NOPE", [abs_dir])
    cands, _ = _candidates(tmp_path, "NOPE")
    assert cands
    for c in cands:
        assert c["function"] and c["file"]
        assert "\\" not in c["file"]
        assert not Path(c["file"]).is_absolute()
    assert {c["function"] for c in cands} == {"test_x"}


@pytest.mark.parametrize("mode", ["strict", "warn", "off"])
def test_pending_review_never_blocks(tmp_path, monkeypatch, mode):
    _project(tmp_path, monkeypatch)
    r = gi._run_quality_review_phase({}, "T1", ["tests"])
    assert r.passed is True
    assert r.phase == "quality_review_pending"
