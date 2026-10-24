"""The `red_pending` advisory must report what it checked, not what it assumes.

`ManualSpawner.spawn` decides a phase is unfinished when the role's output file
is missing, and its consumers turned that into the sentence *"the writer agent
has not run."* That is a claim about the world the check has no evidence for.
Measured on the in-the-loop-learning repo, it was false on five of six beads:
each had a blind contract committed BEFORE the implementation, and each was
reported as though no writer existed, because a writer dispatched by hand
through the Agent tool writes tests and never touches the handshake file.

The sixth — TD125 — is the reason the advisory cannot simply be deleted, and
also the reason it cannot be rebuilt on "evidence the writer leaves behind".
TD125 has a blind-session audit log recording `agent_role="test_writer"`, and a
committed `test_td125_*_blind.py`. Both traces are present. What makes it a
true positive is ORDERING: its contract was written against an implementation
that had already shipped, so it verified code rather than constraining it. No
scan of the repo can recover ordering that nobody recorded, which is exactly
what the handshake report exists to record.

So the fix is not to widen the evidence the check ACTS on. It is to:

  * keep the handshake as the completion signal, and give a hand-spawned
    writer a path that actually closes it (`record_writer_handshake`), with a
    brief that names the artifact instead of telling the operator to delete it;
  * split "never ran" from "ran before the brief was re-staged", which are
    different problems with different remedies;
  * name the artifact that was looked for and not found, and REPORT the traces
    of a run alongside it, so a reader can tell a missing handshake from a
    missing writer without opening orchestrator.py.

These tests pin all four.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from blind_tdd.orchestrator import (
    ManualSpawner,
    gather_manual_evidence,
    record_writer_handshake,
)

TEST_DIRS = ["tests/contracts/", "tests/integration/"]


# ---------------------------------------------------------------------------
# Fixture builders — each reproduces one real on-disk shape
# ---------------------------------------------------------------------------

def _audit_log(root: Path, session_id: str, task_id: str, role: str) -> Path:
    """A blind-session audit log, in the real hook's record shape."""
    d = root / ".themis" / "blind_audit"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{session_id}.jsonl"
    path.write_text(
        json.dumps({
            "timestamp": "2026-08-19T12:43:48.235828+00:00",
            "session_id": session_id,
            "agent_role": role,
            "task_id": task_id,
            "tool_name": "Write",
            "source": "posttooluse_hook",
        }) + "\n",
        encoding="utf-8",
    )
    return path


def _contract_file(root: Path, name: str) -> Path:
    """A committed blind contract test file."""
    d = root / "tests" / "contracts"
    d.mkdir(parents=True, exist_ok=True)
    path = d / name
    path.write_text('"""blind contract"""\n', encoding="utf-8")
    return path


def _triage(root: Path, task_id: str, *, mtime: float | None = None) -> Path:
    d = root / ".themis" / "blind_tdd" / "triage"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{task_id}.json"
    path.write_text(
        json.dumps({"task": task_id, "triage": [
            {"criterion": "AC-1", "status": "tested"},
        ]}),
        encoding="utf-8",
    )
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _brief(root: Path, role: str, task_id: str, *, mtime: float | None = None) -> Path:
    d = root / ".themis" / "blind_tdd" / "pending"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{role}-{task_id}.md"
    path.write_text("# staged brief\n", encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _spawn(task_id: str, role: str = "test_writer") -> dict:
    return ManualSpawner().spawn(
        role=role,
        prompt_template="(prompt body)",
        inputs={"task_id": task_id, "test_dirs": TEST_DIRS},
    )


# ---------------------------------------------------------------------------
# AC-1 — a hand-spawned writer has a completion path it can actually take
# ---------------------------------------------------------------------------

def test_ac1_a_hand_spawned_writer_can_close_the_handshake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """`record_writer_handshake` is the supported path, and the spawner honours it.

    Before this existed, a writer dispatched outside the orchestrator had no
    way at all to finish the protocol — it wrote tests, committed them, and
    was reported as never having run, on every re-invocation, forever.
    """
    monkeypatch.chdir(tmp_path)

    first = _spawn("TD146")
    assert first["manual_state"] == "pending"

    record_writer_handshake("TD146", {"triage": [
        {"criterion": "AC-1", "status": "tested"},
        {"criterion": "AC-2", "status": "needs_human", "reason": "wall clock"},
    ]})

    second = _spawn("TD146")
    assert second["manual_state"] == "complete"
    assert second["success"] is True
    assert second["manual_completed"] is True


def test_ac1_the_written_handshake_is_the_report_the_red_phase_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """It lands at the exact path `run_red_phase` step 5 reads, in that shape.

    A handshake written somewhere else, or shaped differently, would close the
    spawner's check and then fail the red phase with a confusing JSON error.
    """
    monkeypatch.chdir(tmp_path)
    path = record_writer_handshake("TD146", {"triage": [
        {"criterion": "AC-1", "status": "tested"},
    ]})
    assert path == Path(".themis") / "blind_tdd" / "triage" / "TD146.json"
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["task"] == "TD146"
    assert loaded["triage"] == [{"criterion": "AC-1", "status": "tested"}]


@pytest.mark.parametrize("bad", [
    "not a dict",
    {},
    {"triage": "not a list"},
    {"triage": [{"status": "tested"}]},
])
def test_ac1_a_malformed_handshake_is_refused_at_the_door(
    bad, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A handshake that cannot be verified must not be written.

    The point of keeping the handshake is that `verify_coverage` checks every
    claim in it against files on disk. A report it cannot parse would let the
    unchecked-assertion problem back in through the completion path.
    """
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError):
        record_writer_handshake("TD146", bad)
    assert not (Path(".themis") / "blind_tdd" / "triage" / "TD146.json").exists()


def test_ac1_the_brief_names_the_artifact_instead_of_asking_for_a_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The brief used to end "Delete this brief file to signal completion."

    Deletion is not, and never was, what the spawner checks. An operator who
    followed the brief to the letter got `red_pending` anyway.
    """
    monkeypatch.chdir(tmp_path)
    brief = Path(_spawn("TD146")["brief_path"]).read_text(encoding="utf-8")

    assert "Delete this brief file to signal completion" not in brief
    assert str(Path(".themis") / "blind_tdd" / "triage" / "TD146.json") in brief
    assert "record_writer_handshake" in brief


def test_ac1_a_role_with_no_mapped_artifact_says_so_in_the_brief(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """An unmapped role can never be detected as finished. Show the hole."""
    monkeypatch.chdir(tmp_path)
    brief = Path(_spawn("TD146", role="reviewer")["brief_path"]).read_text(encoding="utf-8")
    assert "No completion artifact is mapped" in brief


# ---------------------------------------------------------------------------
# AC-2 — the one true positive survives
# ---------------------------------------------------------------------------

def test_ac2_an_audit_log_and_a_contract_file_do_not_close_the_red_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The TD125 shape, exactly: both traces present, no handshake.

    TD125's blind session ran AFTER the implementation shipped, so its tests
    verified code instead of constraining it. It leaves the same two traces a
    genuine red phase leaves. If either trace closed the phase, the advisory
    would be silent on the only case it ever got right.
    """
    monkeypatch.chdir(tmp_path)
    _audit_log(tmp_path, "td125-independent-verify-2026-08-18", "TD125", "test_writer")
    _contract_file(tmp_path, "test_td125_lesson_spans_blind.py")

    result = _spawn("TD125")

    assert result["manual_state"] == "pending"
    assert result["success"] is False
    assert result["manual_mode"] is True


def test_ac2_the_evidence_is_reported_but_never_decides_the_handshake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Gathered facts describe; the expected output file alone decides."""
    monkeypatch.chdir(tmp_path)
    _audit_log(tmp_path, "td125-independent-verify-2026-08-18", "TD125", "test_writer")
    _contract_file(tmp_path, "test_td125_lesson_spans_blind.py")

    ev = gather_manual_evidence(
        role="test_writer", task_id="TD125", test_dirs=TEST_DIRS,
    )
    assert ev.audit_sessions == ["td125-independent-verify-2026-08-18"]
    assert ev.contract_files
    assert ev.handshake == "absent"


# ---------------------------------------------------------------------------
# AC-3 — "ran before the brief was re-staged" is not "never ran"
# ---------------------------------------------------------------------------

def test_ac3_a_report_older_than_a_restaged_brief_is_stale_not_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The TD10 / TD23 shape: a real report, under a brief re-staged later.

    Both had triage reports dated 2026-08-15 and briefs re-staged 2026-08-18.
    Completed work, reported as though the writer had never run.
    """
    monkeypatch.chdir(tmp_path)
    now = time.time()
    _triage(tmp_path, "TD10", mtime=now - 400_000)
    _brief(tmp_path, "test_writer", "TD10", mtime=now - 100_000)

    result = _spawn("TD10")

    assert result["manual_state"] == "stale"
    message = result["message"]
    assert "STALE" in message
    assert "not missing" in message.lower()
    assert "does not mean it never ran" in message
    # The remedy for stale is not the remedy for missing.
    assert "delete" in message.lower()


def test_ac3_a_stale_brief_is_not_restamped_on_every_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Rewriting the brief would make "stale" permanent and unfixable.

    The brief is compared against the report by mtime. Restaging it on each
    gate run pushes it past the report again every time, so the documented
    remedy — accept the report by deleting the brief — could never be reached
    by any action short of re-running the agent.
    """
    monkeypatch.chdir(tmp_path)
    now = time.time()
    _triage(tmp_path, "TD23", mtime=now - 400_000)
    brief = _brief(tmp_path, "test_writer", "TD23", mtime=now - 100_000)
    before = brief.stat().st_mtime

    _spawn("TD23")
    _spawn("TD23")

    assert brief.stat().st_mtime == before, "the stale brief was re-stamped"


def test_ac3_deleting_the_brief_accepts_the_existing_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The remedy the message names has to actually work."""
    monkeypatch.chdir(tmp_path)
    now = time.time()
    _triage(tmp_path, "TD23", mtime=now - 400_000)
    brief = _brief(tmp_path, "test_writer", "TD23", mtime=now - 100_000)

    assert _spawn("TD23")["manual_state"] == "stale"
    brief.unlink()
    assert _spawn("TD23")["manual_state"] == "complete"


# ---------------------------------------------------------------------------
# AC-4 — the message names the artifact, and claims nothing else
# ---------------------------------------------------------------------------

def test_ac4_the_message_names_the_artifact_it_looked_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.chdir(tmp_path)
    message = _spawn("TD146")["message"]
    assert str(Path(".themis") / "blind_tdd" / "triage" / "TD146.json") in message
    assert "looked for" in message


def test_ac4_the_message_does_not_claim_the_writer_did_not_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The one sentence this bead exists to delete.

    The check observes an absent file. "No writer ran" is a different
    proposition, and it was wrong five times out of six.
    """
    monkeypatch.chdir(tmp_path)
    _audit_log(tmp_path, "td146-writer-2026-08-19", "TD146", "test_writer")
    _contract_file(tmp_path, "test_td146_directive_erasure_blind.py")

    message = _spawn("TD146")["message"].lower()

    for claim in (
        "the writer agent has not run",
        "no blind tests exist",
        "the writer has not run",
    ):
        assert claim not in message, f"advisory still asserts: {claim!r}"


def test_ac4_the_message_reports_the_traces_of_a_run_it_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A reader must be able to tell a missing handshake from a missing writer."""
    monkeypatch.chdir(tmp_path)
    _audit_log(tmp_path, "td146-writer-2026-08-19", "TD146", "test_writer")
    _contract_file(tmp_path, "test_td146_directive_erasure_blind.py")

    message = _spawn("TD146")["message"]

    assert "td146-writer-2026-08-19" in message
    assert "test_td146_directive_erasure_blind.py" in message
    assert "missing handshake rather than a missing agent" in message


def test_ac4_with_no_traces_at_all_the_message_says_that_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Silence about evidence would read as absence of evidence. Say which."""
    monkeypatch.chdir(tmp_path)
    message = _spawn("TD999")["message"]
    assert "No audit log and no test file naming this task were found" in message


def test_ac4_the_searched_locations_are_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.chdir(tmp_path)
    message = _spawn("TD146")["message"]
    assert "searched:" in message
    assert "blind_audit" in message
    assert "tests" in message


# ---------------------------------------------------------------------------
# Evidence gathering reads bodies, not filenames
# ---------------------------------------------------------------------------

def test_audit_matching_reads_the_record_not_the_filename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A session is named by whoever opened it.

    `td125-independent-verify-2026-08-18` is a real session name for task
    TD125; `unknown.jsonl` is a real file holding records for several. Neither
    filename is a usable key.
    """
    monkeypatch.chdir(tmp_path)
    _audit_log(tmp_path, "unknown", "TD146", "test_writer")

    ev = gather_manual_evidence(role="test_writer", task_id="TD146", test_dirs=[])
    assert ev.audit_sessions == ["unknown"]


def test_audit_matching_respects_the_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A runner's log is not evidence that a writer ran."""
    monkeypatch.chdir(tmp_path)
    _audit_log(tmp_path, "sso1-runner", "SSO1", "test_runner")

    writer = gather_manual_evidence(role="test_writer", task_id="SSO1", test_dirs=[])
    runner = gather_manual_evidence(role="test_runner", task_id="SSO1", test_dirs=[])
    assert writer.audit_sessions == []
    assert runner.audit_sessions == ["sso1-runner"]


def test_a_malformed_audit_line_does_not_break_the_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Advisory evidence must never be able to crash the gate."""
    monkeypatch.chdir(tmp_path)
    d = tmp_path / ".themis" / "blind_audit"
    d.mkdir(parents=True)
    (d / "broken.jsonl").write_text("not json\n\n[1,2,3]\n", encoding="utf-8")
    _audit_log(tmp_path, "td146-writer", "TD146", "test_writer")

    ev = gather_manual_evidence(role="test_writer", task_id="TD146", test_dirs=[])
    assert ev.audit_sessions == ["td146-writer"]


def test_task_ids_match_as_whole_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """TD1 must not claim TD110's contract file.

    Substring matching on short ids is how an advisory silently borrows
    another task's evidence.
    """
    monkeypatch.chdir(tmp_path)
    _contract_file(tmp_path, "test_td110_activity_library_blind.py")

    assert gather_manual_evidence(
        role="test_writer", task_id="TD1", test_dirs=TEST_DIRS,
    ).contract_files == []
    assert gather_manual_evidence(
        role="test_writer", task_id="TD110", test_dirs=TEST_DIRS,
    ).contract_files


def test_compiled_artifacts_are_not_counted_as_contract_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """`__pycache__/test_td146_....pyc` is not a test file anyone wrote."""
    monkeypatch.chdir(tmp_path)
    cache = tmp_path / "tests" / "contracts" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "test_td146_directive_erasure_blind.cpython-39.pyc").write_bytes(b"\x00")

    ev = gather_manual_evidence(
        role="test_writer", task_id="TD146", test_dirs=TEST_DIRS,
    )
    assert ev.contract_files == []


# ---------------------------------------------------------------------------
# AC-5 — the six real task histories
# ---------------------------------------------------------------------------
#
# Reproduced from the recorded state of the in-the-loop-learning repo on
# 2026-08-19. Each row is the on-disk shape that repo actually had; `expected`
# is what the advisory must say about it.

_REAL_HISTORIES = [
    # task,  audit session,                          contract file,                              triage age, brief age, expected
    ("TD110", "td110-writer-2026-08-19",             "test_td110_activity_library_blind.py",      None, None, "pending"),
    ("TD128", "td128-writer-2026-08-19",             "test_td128_activity_payload_blind.py",      None, None, "pending"),
    ("TD146", "td146-writer-2026-08-19",             "test_td146_directive_erasure_blind.py",     None, None, "pending"),
    ("TD122", None,                                  "test_td122_segment_authoring_blind.py",     None, None, "pending"),
    ("TD124", None,                                  "test_td124_framework_editability_blind.py", None, None, "pending"),
    ("TD125", "td125-independent-verify-2026-08-18", "test_td125_lesson_spans_blind.py",          None, None, "pending"),
    # The second false-positive shape: completed work under a re-staged brief.
    ("TD10",  None, "test_td10_export_unit.py",              400_000, 100_000, "stale"),
    ("TD23",  None, "test_td23_criteria_assessment_blind.py", 400_000, 100_000, "stale"),
]


@pytest.mark.parametrize(
    "task_id,session_id,contract,triage_age,brief_age,expected",
    _REAL_HISTORIES,
    ids=[row[0] for row in _REAL_HISTORIES],
)
def test_ac5_the_real_histories_classify_correctly(
    task_id, session_id, contract, triage_age, brief_age, expected,
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.chdir(tmp_path)
    now = time.time()
    if session_id:
        _audit_log(tmp_path, session_id, task_id, "test_writer")
    if contract:
        _contract_file(tmp_path, contract)
    if triage_age is not None:
        _triage(tmp_path, task_id, mtime=now - triage_age)
    if brief_age is not None:
        _brief(tmp_path, "test_writer", task_id, mtime=now - brief_age)

    result = _spawn(task_id)

    assert result["manual_state"] == expected
    # Whatever the verdict, the advisory names the artifact and claims no more.
    assert f"{task_id}.json" in result["message"]
    assert "has not run" not in result["message"].lower()
