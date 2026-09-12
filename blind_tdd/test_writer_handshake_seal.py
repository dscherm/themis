"""`record-writer-handshake --test-dir` seals test-file hashes in the writer's own record.

Before this, only `seal-red-state` (the orchestrator-authority path for a writer that did
NOT finish) hashed anything. The path a writer that finished is supposed to take left no
seal, so nothing downstream could tell a blind file from one edited afterwards.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from blind_tdd import orchestrator as O


def _in(tmp_path: Path, fn):
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        return fn()
    finally:
        os.chdir(cwd)


def test_writer_handshake_with_test_dir_seals_hashes(tmp_path, capsys):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n", encoding="utf-8")
    triage = tmp_path / "triage.json"
    triage.write_text(json.dumps({"triage": [{"criterion": "AC-1", "status": "tested"}]}), encoding="utf-8")

    rc = _in(tmp_path, lambda: O.main(["record-writer-handshake", "T1", str(triage), "--test-dir", "tests"]))
    assert rc == 0
    out = capsys.readouterr().out
    assert "1 test file(s) sealed" in out
    rec = json.loads((tmp_path / ".themis" / "blind_tdd" / "triage" / "T1.json").read_text(encoding="utf-8"))
    assert list(rec["writer_hash_test_files"]) == [str(Path("tests") / "test_a.py")] or \
        any(k.endswith("test_a.py") for k in rec["writer_hash_test_files"])
    assert rec["sealed_by"].startswith("the blind writer")


def test_a_single_file_seals_only_itself(tmp_path, capsys):
    """Sealing a directory claims every file in it for this session; a writer that wrote
    one file must be able to seal exactly that one."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_old.py").write_text("def test_old():\n    assert True\n", encoding="utf-8")
    (tmp_path / "tests" / "test_new.py").write_text("def test_new():\n    assert True\n", encoding="utf-8")
    triage = tmp_path / "triage.json"
    triage.write_text(json.dumps({"triage": [{"criterion": "AC-1", "status": "tested"}]}), encoding="utf-8")
    rc = _in(tmp_path, lambda: O.main(["record-writer-handshake", "T4", str(triage),
                                       "--test-dir", str(Path("tests") / "test_new.py")]))
    assert rc == 0
    rec = json.loads((tmp_path / ".themis" / "blind_tdd" / "triage" / "T4.json").read_text(encoding="utf-8"))
    keys = list(rec["writer_hash_test_files"])
    assert len(keys) == 1 and keys[0].endswith("test_new.py"), keys


def test_writer_handshake_without_test_dir_is_unchanged(tmp_path):
    triage = tmp_path / "triage.json"
    triage.write_text(json.dumps({"triage": [{"criterion": "AC-1", "status": "tested"}]}), encoding="utf-8")
    rc = _in(tmp_path, lambda: O.main(["record-writer-handshake", "T2", str(triage)]))
    assert rc == 0
    rec = json.loads((tmp_path / ".themis" / "blind_tdd" / "triage" / "T2.json").read_text(encoding="utf-8"))
    assert "writer_hash_test_files" not in rec


def test_an_empty_test_dir_is_refused(tmp_path, capsys):
    (tmp_path / "empty").mkdir()
    triage = tmp_path / "triage.json"
    triage.write_text(json.dumps({"triage": [{"criterion": "AC-1", "status": "tested"}]}), encoding="utf-8")
    rc = _in(tmp_path, lambda: O.main(["record-writer-handshake", "T3", str(triage), "--test-dir", "empty"]))
    assert rc == 1
    assert "refusing to seal an empty set" in capsys.readouterr().err
