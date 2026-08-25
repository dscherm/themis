"""A shell-callable path to `record_writer_handshake` (TD274).

`record_writer_handshake` (TD150) has been importable from Python since the
first time "a hand-spawned writer has no way to close the red phase" was
measured. It stayed friction to *reach* from an orchestrator driving a shell
rather than a Python REPL, and that friction is what let the same defect
recur: measured on the in-the-loop-learning repo again on TD274, a real
pre-implementation writer (TD272) was dispatched by hand, its agent was
killed mid-task before it wrote the handshake, and nobody had a one-line way
to record it on the agent's behalf.

These tests pin the CLI added to close that gap, and — the property that
actually matters — that writing through it produces a handshake
`gather_manual_evidence` reports as "present", the same as if the writer had
written the file itself. The decision logic in `gather_manual_evidence`
itself is untouched; see `test_writer_handshake_evidence.py` for why it must
stay that way (ordering, not corroboration, is what the check can trust).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from blind_tdd.orchestrator import gather_manual_evidence, main


def test_cli_writes_the_same_artifact_record_writer_handshake_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.chdir(tmp_path)
    triage_in = tmp_path / "triage.json"
    triage_in.write_text(
        json.dumps({"triage": [{"criterion": "AC-1", "status": "tested"}]}),
        encoding="utf-8",
    )

    rc = main(["record-writer-handshake", "TD272", str(triage_in)])

    assert rc == 0
    out = Path(".themis") / "blind_tdd" / "triage" / "TD272.json"
    assert out.exists()
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["task"] == "TD272"
    assert written["triage"] == [{"criterion": "AC-1", "status": "tested"}]


def test_cli_reads_triage_from_stdin_when_path_is_a_dash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "sys.stdin",
        __import__("io").StringIO(
            json.dumps({"triage": [{"criterion": "AC-1", "status": "tested"}]})
        ),
    )

    rc = main(["record-writer-handshake", "TD272", "-"])

    assert rc == 0
    assert (Path(".themis") / "blind_tdd" / "triage" / "TD272.json").exists()


def test_cli_rejects_malformed_json_without_writing_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    monkeypatch.chdir(tmp_path)
    bad = tmp_path / "not_json.txt"
    bad.write_text("this is not json", encoding="utf-8")

    rc = main(["record-writer-handshake", "TD272", str(bad)])

    assert rc == 1
    assert "not valid JSON" in capsys.readouterr().err
    assert not (Path(".themis") / "blind_tdd" / "triage" / "TD272.json").exists()


def test_cli_rejects_a_report_not_shaped_like_a_triage_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    monkeypatch.chdir(tmp_path)
    wrong_shape = tmp_path / "wrong.json"
    wrong_shape.write_text(json.dumps({"overall": "pass"}), encoding="utf-8")

    rc = main(["record-writer-handshake", "TD272", str(wrong_shape)])

    assert rc == 1
    assert "triage" in capsys.readouterr().err
    assert not (Path(".themis") / "blind_tdd" / "triage" / "TD272.json").exists()


def test_unknown_subcommand_prints_usage_and_exits_nonzero(
    capsys: pytest.CaptureFixture,
):
    assert main([]) == 2
    assert main(["not-a-real-command"]) == 2
    assert "record-writer-handshake" in capsys.readouterr().err


def test_the_recorded_handshake_reads_as_present_not_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The actual property TD274 needed: a writer dispatched by hand, whose
    agent died before it could write the handshake itself, can have it
    recorded on its behalf — and the gate's own evidence-gatherer then sees
    exactly what it would have seen from a writer that wrote it directly."""
    monkeypatch.chdir(tmp_path)

    before = gather_manual_evidence(role="test_writer", task_id="TD272")
    assert before.handshake == "absent"

    triage_in = tmp_path / "triage.json"
    triage_in.write_text(
        json.dumps({"triage": [{"criterion": "AC-1", "status": "tested"}]}),
        encoding="utf-8",
    )
    assert main(["record-writer-handshake", "TD272", str(triage_in)]) == 0

    after = gather_manual_evidence(role="test_writer", task_id="TD272")
    assert after.handshake == "present"
