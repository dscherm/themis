"""Attribution of a new test file after the red seal (docs/shared-checkout-seal.md).

GF-4 sealed, then COV-1's blind tests landed as test_cov1_*.py with a
`Bead: COV-1` docstring line and unqualified tags. Order: Bead line, file
name, qualified tags, unqualified tags.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from blind_tdd.orchestrator import _new_file_scope, _task_id_pattern  # noqa: E402

TASK = {"id": "COV-1", "acceptance_criteria": [{"id": "AC-1"}]}
KNOWN = ["COV-1", "GF-4", "TD1"]


def _scope(tmp_path, name, body, task=TASK, known=KNOWN):
    f = tmp_path / name
    f.write_text(body, encoding="utf-8")
    return _new_file_scope(str(f), task=task, known_task_ids=known)


@pytest.mark.parametrize("name", ["test_cov1_x.py", "test_cov-1_x.py", "test_cov_1_x.py"])
def test_separator_forms_match(name):
    assert _task_id_pattern("COV-1").search(name)


@pytest.mark.parametrize("tid,name", [("COV-1", "test_cov12_x.py"), ("TD1", "test_td110_x.py")])
def test_whole_token_negatives(tid, name):
    assert not _task_id_pattern(tid).search(name)


def test_name_with_collapsed_separator_is_in_scope(tmp_path):
    assert not _scope(tmp_path, "test_cov1_x.py", "def test_a():\n    pass\n").startswith("out of scope")


def test_bead_line_for_this_task_is_in_scope(tmp_path):
    body = '"""Blind tests.\n\nBead: COV-1\n"""\ndef test_a():\n    """Covers: AC-1"""\n'
    assert not _scope(tmp_path, "test_neutral.py", body).startswith("out of scope")


def test_bead_line_for_other_task_is_out_of_scope(tmp_path):
    body = '"""Bead: GF-4"""\ndef test_a():\n    """Covers: AC-1"""\n'
    assert _scope(tmp_path, "test_neutral.py", body) == "out of scope: Bead: GF-4"


def test_bead_line_beats_file_name(tmp_path):
    body = '"""Bead: GF-4"""\n'
    assert _scope(tmp_path, "test_cov1_x.py", body).startswith("out of scope: Bead")


def test_syntax_error_falls_through_to_name(tmp_path):
    assert not _scope(tmp_path, "test_cov1_x.py", "def (:\n").startswith("out of scope")


def test_qualified_other_task_tags_are_out_of_scope(tmp_path):
    body = 'def test_a():\n    """Covers: AC-1 (GF-4)"""\n'
    assert _scope(tmp_path, "test_neutral.py", body).startswith("out of scope")


def test_qualified_for_this_task_is_in_scope(tmp_path):
    body = 'def test_a():\n    """Covers: AC-1 (COV-1)"""\n'
    assert not _scope(tmp_path, "test_neutral.py", body).startswith("out of scope")


def test_unqualified_tag_without_bead_or_name_breaks(tmp_path):
    body = 'def test_a():\n    """Covers: AC-1"""\n'
    assert not _scope(tmp_path, "test_neutral.py", body).startswith("out of scope")


def test_conftest_stays_in_scope(tmp_path):
    assert not _scope(tmp_path, "conftest.py", '"""Bead: GF-4"""\n').startswith("out of scope")
