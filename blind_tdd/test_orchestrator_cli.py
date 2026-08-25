"""Shell-callable paths to the writer-handshake and red-state recorders (TD274).

TD274 measured a tier-2 "the writer agent has not run" yellow firing on
beads that had genuinely done blind-TDD work, and set out to fix it. The
first cut wrapped `record_writer_handshake` (TD150) in a CLI and used it for
every case — including TD272, where the dispatched writer agent was "killed
by an expired login before it could commit" (its own commit message,
e762ffc). That is exactly the move
`lessons/a-hand-spawned-blind-writer-leaves-no-handshake-seal-it-yourself.md`
documents Dan declining for TD188: writing a first-person triage report FOR
a writer that never produced one is forged provenance, not a completed
handshake, even when the tests that exist are real.

The lesson's own answer is what this file now pins, as two DIFFERENT
commands for two DIFFERENT situations:

  * `record-writer-handshake` — the writer ran to completion outside the
    orchestrator's own spawn and can speak in its own voice; this is a thin
    CLI over `record_writer_handshake` (TD150), unchanged in what it accepts.
  * `seal-red-state` — the writer did NOT reach its own last step. Nobody
    can honestly write a first-person report, so the ORCHESTRATOR seals the
    red state directly (`gate_integration.save_red_state`), signs it
    `"orchestrator, NOT the blind writer"`, and is forced to state
    `--ordering-evidence` (the field the lesson calls load-bearing) and
    `--why-not-the-writer`.

Both are additive: neither touches `gather_manual_evidence`'s decision logic
(see `test_writer_handshake_evidence.py`) or `verify_coverage`, which still
runs at the green phase against whatever `test_dirs` were sealed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from blind_tdd import gate_integration as gi
from blind_tdd.orchestrator import gather_manual_evidence, main


# ---------------------------------------------------------------------------
# record-writer-handshake — the writer speaks for itself
# ---------------------------------------------------------------------------

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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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


def test_the_recorded_handshake_reads_as_present_not_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """For the writer-voice case: once the writer's own report is relayed,
    the gate's evidence-gatherer sees exactly what it would have seen from a
    writer that wrote the file itself."""
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


# ---------------------------------------------------------------------------
# seal-red-state — the orchestrator seals on its own authority, and says so
# ---------------------------------------------------------------------------

def _write_test_file(root: Path, name: str = "test_td272_x.py") -> Path:
    d = root / "tests" / "contracts"
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_text("def test_x():\n    assert False\n", encoding="utf-8")
    return f


def test_seal_red_state_requires_ordering_evidence_and_why_not_the_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """argparse enforces both required flags — the CLI cannot be called
    without stating the load-bearing field."""
    monkeypatch.chdir(tmp_path)
    _write_test_file(tmp_path)

    with pytest.raises(SystemExit):
        main(["seal-red-state", "TD272", "--test-dir", "tests/contracts"])
    assert "--ordering-evidence" in capsys.readouterr().err

    with pytest.raises(SystemExit):
        main([
            "seal-red-state", "TD272", "--test-dir", "tests/contracts",
            "--ordering-evidence", "measured now, impl absent",
        ])
    assert "--why-not-the-writer" in capsys.readouterr().err


def test_seal_red_state_refuses_an_empty_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tests" / "contracts").mkdir(parents=True)

    rc = main([
        "seal-red-state", "TD272", "--test-dir", "tests/contracts",
        "--ordering-evidence", "x", "--why-not-the-writer", "y",
    ])

    assert rc == 1
    assert "no test files found" in capsys.readouterr().err
    assert gi.load_red_state("TD272") is None


def test_seal_red_state_writes_a_record_signed_as_the_orchestrator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The property the lesson demands: the record must say WHO sealed it
    and WHY the ordering is known — never silently assert a managed writer
    session that did not happen."""
    monkeypatch.chdir(tmp_path)
    test_file = _write_test_file(tmp_path)

    rc = main([
        "seal-red-state", "TD272",
        "--test-dir", "tests/contracts",
        "--ordering-evidence",
        "writer killed mid-task; test committed with no implementation in the diff",
        "--why-not-the-writer",
        "agent killed by an expired login before its last step",
        "--measured-at-commit", "e762ffc",
        "--counts-failed", "1",
        "--counts-passed", "0",
    ])

    assert rc == 0
    state = gi.load_red_state("TD272")
    assert state is not None
    triage = state["triage_report"]
    assert triage["sealed_by"] == "orchestrator, NOT the blind writer"
    assert "expired login" in triage["why_not_the_writer"]
    assert "killed mid-task" in triage["ordering_evidence"]
    assert triage["measured_at_commit"] == "e762ffc"
    assert triage["counts"] == {"failed": 1, "passed": 0}
    rel = str(test_file.relative_to(tmp_path))
    assert any(rel.replace("/", "\\") in k or rel in k for k in state["test_file_hashes"])


def test_seal_red_state_is_what_makes_the_gate_skip_the_red_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The actual property TD272 needed: `_run_blind_tdd_gate` branches on
    `load_red_state(task_id) is None` to decide whether to re-enter the red
    phase (and re-stage a writer brief) at all. Before sealing, there is
    nothing to load; after, the gate has a baseline and moves straight to
    green — no ManualSpawner, no re-staged brief, no handshake file check
    involved."""
    monkeypatch.chdir(tmp_path)
    _write_test_file(tmp_path)

    assert gi.load_red_state("TD272") is None

    assert main([
        "seal-red-state", "TD272", "--test-dir", "tests/contracts",
        "--ordering-evidence", "measured now, impl absent",
        "--why-not-the-writer", "session killed mid-task",
    ]) == 0

    assert gi.load_red_state("TD272") is not None


def test_seal_red_state_hashes_do_not_touch_the_writer_handshake_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """seal-red-state and record-writer-handshake write to different
    artifacts (red_state/ vs triage/) — sealing one must not fake the other."""
    monkeypatch.chdir(tmp_path)
    _write_test_file(tmp_path)

    main([
        "seal-red-state", "TD272", "--test-dir", "tests/contracts",
        "--ordering-evidence", "x", "--why-not-the-writer", "y",
    ])

    assert not (Path(".themis") / "blind_tdd" / "triage" / "TD272.json").exists()
    evidence = gather_manual_evidence(role="test_writer", task_id="TD272")
    assert evidence.handshake == "absent"


def test_unknown_subcommand_prints_usage_and_exits_nonzero(
    capsys: pytest.CaptureFixture,
):
    assert main([]) == 2
    assert main(["not-a-real-command"]) == 2
    err = capsys.readouterr().err
    assert "record-writer-handshake" in err
    assert "seal-red-state" in err
