"""Every role in the protocol can close its own phase — not just the writer.

TD150 gave a hand-spawned `test_writer` a supported completion path
(`record_writer_handshake`) and stopped there, because a writer was the case
that had been *observed* failing. The defect it fixed was not "writers cannot
finish"; it was **a role dispatched outside the orchestrator has no way to
complete its half of the protocol**, and that lived in all three branches of
the role table.

The bill arrived on TD118 in the in-the-loop-learning repo: 155 blind contract
tests genuinely passing, the full suite green, and a gate that said

    green phase did not pass: blind-TDD test_runner handshake for task 'TD118'
    was NOT FOUND. looked for: .themis/blind_tdd/green_report/TD118.json

with no supported way to produce that file, because `grep 'def record_'`
returned exactly one recorder and it wrote triage reports.

So the tests below pin the general property, not the next instance of it:

  * `ROLE_HANDSHAKES` is the one place a role is declared, and every entry in
    it has a path, a validator and a recorder that exists. A fourth role added
    without a completion path fails here.
  * the runner's report is MEASURED, never asserted — `record_runner_handshake`
    has no parameter through which a caller can say the tests passed.
  * a handshake recorded on a task whose tests genuinely fail does not turn the
    gate green.
  * all three recorders share one implementation, so the third copy that made
    this bead necessary cannot be written.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from blind_tdd import orchestrator
from blind_tdd.orchestrator import (
    BlindTddOrchestrator,
    ManualSpawner,
    RedPhaseResult,
    ROLE_HANDSHAKES,
    _hash_test_files,
    measure_test_run,
    record_arbiter_handshake,
    record_handshake,
    record_runner_handshake,
    record_writer_handshake,
)
from blind_tdd.session import ROLE_SETTINGS_TEMPLATES

TASK = {
    "id": "TD118",
    "acceptance_criteria": [
        {"id": "AC-1", "given": "a", "when": "b", "then": "c"},
    ],
    "public_surface": {"module": "src.foo", "adds": ["foo()"]},
}


# ---------------------------------------------------------------------------
# Fixture builders — a minimal project the real green phase can run against
# ---------------------------------------------------------------------------

def _contract(root: Path, *, passing: bool) -> Path:
    d = root / "tests" / "contracts"
    d.mkdir(parents=True, exist_ok=True)
    path = d / "test_ac1_blind.py"
    path.write_text(
        "def test_ac1_the_thing_holds():\n"
        '    """Covers: AC-1"""\n'
        f"    assert {'1 == 1' if passing else '1 == 2'}\n",
        encoding="utf-8",
    )
    return path


def _prompts(root: Path) -> Path:
    d = root / "templates" / "blind_tdd" / "prompts"
    d.mkdir(parents=True, exist_ok=True)
    for name in ("test_writer.md", "test_runner.md", "arbiter.md"):
        (d / name).write_text(f"# stub {name}\n", encoding="utf-8")
    return d


def _project(root: Path, *, passing: bool) -> BlindTddOrchestrator:
    """A project with one blind contract and a manual spawner — TD118's shape."""
    _contract(root, passing=passing)
    base = _prompts(root)
    return BlindTddOrchestrator(
        spawner=ManualSpawner(),
        test_dirs=["tests/contracts/"],
        writer_prompt_path=str(base / "test_writer.md"),
        runner_prompt_path=str(base / "test_runner.md"),
        arbiter_prompt_path=str(base / "arbiter.md"),
    )


def _red(root: Path) -> RedPhaseResult:
    return RedPhaseResult(
        passed=True,
        test_file_hashes=_hash_test_files([Path("tests/contracts/")]),
        triage_report={"task": "TD118", "triage": []},
    )


def _spawn(task_id: str, role: str) -> dict:
    return ManualSpawner().spawn(
        role=role,
        prompt_template="(prompt body)",
        inputs={"task_id": task_id, "test_dirs": ["tests/contracts/"]},
    )


# ---------------------------------------------------------------------------
# AC-1 — the implementer that made a contract green can say so
# ---------------------------------------------------------------------------

def test_ac1_a_hand_run_green_phase_can_be_closed_at_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """TD118, end to end: green phase blocked, handshake recorded, green phase passes.

    This is the exact failure the bead was filed for. Before
    `record_runner_handshake` existed there was no second half of this test to
    write — the phase could not be closed by any supported means.
    """
    monkeypatch.chdir(tmp_path)
    orch = _project(tmp_path, passing=True)
    red = _red(tmp_path)

    blocked = orch.run_green_phase(TASK, red)
    assert blocked.passed is False
    assert "NOT FOUND" in blocked.reason

    record_runner_handshake("TD118", test_dirs=["tests/contracts/"])

    green = orch.run_green_phase(TASK, red)
    assert green.passed is True, green.reason
    assert green.green_report["overall"] == "pass"


def test_ac1_the_handshake_lands_where_the_green_phase_looks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A report written anywhere else closes nothing, however correct it is."""
    monkeypatch.chdir(tmp_path)
    _contract(tmp_path, passing=True)

    path = record_runner_handshake("TD118", test_dirs=["tests/contracts/"])

    assert path == Path(".themis") / "blind_tdd" / "green_report" / "TD118.json"
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["task"] == "TD118"


def test_ac1_the_reported_names_are_the_ones_coverage_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """`verify_coverage` matches bare function names parsed from the files.

    A report naming pytest node ids (`file.py::test_x`) or parametrised cases
    (`test_x[case]`) would satisfy the spawner and then fail the green phase
    with a coverage gap on a criterion that is, in fact, tested.
    """
    monkeypatch.chdir(tmp_path)
    d = tmp_path / "tests" / "contracts"
    d.mkdir(parents=True)
    (d / "test_param_blind.py").write_text(
        "import pytest\n\n"
        '@pytest.mark.parametrize("n", [1, 2])\n'
        "def test_ac1_each_case(n):\n"
        '    """Covers: AC-1"""\n'
        "    assert n\n",
        encoding="utf-8",
    )

    path = record_runner_handshake("TD118", test_dirs=["tests/contracts/"])
    report = json.loads(path.read_text(encoding="utf-8"))

    assert [e["name"] for e in report["per_test"]] == ["test_ac1_each_case"]
    assert report["per_test"][0]["cases"] == 2


def test_ac1_a_parametrised_test_is_passing_only_when_every_case_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Collapsing cases onto the function must not launder a failing case.

    The name is what coverage checks, so a function reported `pass` while one
    of its parametrisations fails would cover its criterion with a red test.
    """
    monkeypatch.chdir(tmp_path)
    d = tmp_path / "tests" / "contracts"
    d.mkdir(parents=True)
    (d / "test_param_blind.py").write_text(
        "import pytest\n\n"
        '@pytest.mark.parametrize("n", [1, 0])\n'
        "def test_ac1_each_case(n):\n"
        '    """Covers: AC-1"""\n'
        "    assert n\n",
        encoding="utf-8",
    )

    path = record_runner_handshake("TD118", test_dirs=["tests/contracts/"])
    report = json.loads(path.read_text(encoding="utf-8"))

    assert report["per_test"][0]["result"] == "fail"
    assert report["overall"] == "fail"


# ---------------------------------------------------------------------------
# AC-2 — the role table, enumerated
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("role", sorted(ROLE_HANDSHAKES))
def test_ac2_every_role_in_the_table_has_a_recorder_that_exists(role: str):
    """The point of the bead. TD150 fixed one row; this iterates the table.

    A fourth role added to `ROLE_HANDSHAKES` without a `record_*` function
    fails here, rather than six weeks later as an agent that did the work and
    cannot close its phase.
    """
    spec = ROLE_HANDSHAKES[role]
    recorder = getattr(orchestrator, spec.recorder, None)
    assert callable(recorder), (
        f"role {role!r} names recorder {spec.recorder!r}, which does not exist"
    )
    assert not spec.recorder.startswith("_"), "the recorder must be callable by an operator"


@pytest.mark.parametrize("role", sorted(ROLE_HANDSHAKES))
def test_ac2_every_role_maps_to_an_artifact_and_a_validator(role: str):
    spec = ROLE_HANDSHAKES[role]
    expected = orchestrator._expected_manual_output(
        role, "TD118", {"challenge_id": "CH-1"}
    )
    assert expected is not None
    assert expected.suffix == ".json"
    assert expected.parent.name == spec.subdir
    with pytest.raises(ValueError):
        spec.validate({})


@pytest.mark.parametrize("role", sorted(ROLE_HANDSHAKES))
def test_ac2_every_roles_brief_names_its_own_recorder(
    role: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The operator learns the completion path from the brief, or not at all."""
    monkeypatch.chdir(tmp_path)
    brief = Path(_spawn("TD118", role)["brief_path"]).read_text(encoding="utf-8")
    spec = ROLE_HANDSHAKES[role]

    assert spec.recorder in brief
    assert str(orchestrator._expected_manual_output(role, "TD118", {})) in brief


def test_ac2_the_role_table_and_the_settings_table_agree():
    """Two tables key on the same roles; a role in one only is a hole.

    A role with a settings template but no handshake spec can be spawned blind
    and never finish; one with a handshake but no template runs unguarded.
    """
    assert set(ROLE_HANDSHAKES) == set(ROLE_SETTINGS_TEMPLATES)


def test_ac2_an_unmapped_role_is_refused_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Never guess a filename for a role nobody declared."""
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError) as exc:
        record_handshake("reviewer", "TD118", {"anything": True})
    assert "ROLE_HANDSHAKES" in str(exc.value)


# ---------------------------------------------------------------------------
# AC-3 — the runner's report is checked, not asserted
# ---------------------------------------------------------------------------

def test_ac3_the_recorder_has_no_parameter_for_claiming_a_result():
    """There is no `overall=`, no `report=`, no `passed=`.

    The report is built from pytest's JUnit XML inside the call. An agent that
    wants to record a green has to produce one.
    """
    params = set(inspect.signature(record_runner_handshake).parameters)
    for forbidden in ("overall", "report", "passed", "result", "per_test"):
        assert forbidden not in params


def test_ac3_the_recorded_report_carries_the_evidence_it_was_built_from(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A reader can re-run the command that produced the numbers."""
    monkeypatch.chdir(tmp_path)
    _contract(tmp_path, passing=True)

    report = json.loads(
        record_runner_handshake(
            "TD118", test_dirs=["tests/contracts/"]
        ).read_text(encoding="utf-8")
    )

    measured = report["measured"]
    assert measured["exit_code"] == 0
    assert "pytest" in measured["command"]
    assert measured["targets"] == ["tests/contracts/"]


def test_ac3_a_report_that_contradicts_its_own_measurement_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The cheap forgery: run the recorder, get a fail, edit `overall` to pass.

    The evidence block stays behind and disagrees. This is the runner-side
    counterpart of TD150's property that an overclaiming writer report fails
    `verify_coverage` — the claim is falsifiable against something observable.
    """
    monkeypatch.chdir(tmp_path)
    orch = _project(tmp_path, passing=False)
    red = _red(tmp_path)

    path = record_runner_handshake("TD118", test_dirs=["tests/contracts/"])
    forged = json.loads(path.read_text(encoding="utf-8"))
    forged["overall"] = "pass"
    forged["tests_failed"] = 0
    forged["tests_passed"] = 1
    for entry in forged["per_test"]:
        entry["result"] = "pass"
    path.write_text(json.dumps(forged), encoding="utf-8")

    green = orch.run_green_phase(TASK, red)

    assert green.passed is False
    assert "measurement" in green.reason
    assert "exited 1" in green.reason


def test_ac3_a_hand_written_report_naming_tests_that_do_not_exist_fails_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Bypassing the recorder does not buy a green.

    The green phase verifies coverage against the test files on disk, so a
    passing name that no test carries covers no criterion.
    """
    monkeypatch.chdir(tmp_path)
    orch = _project(tmp_path, passing=True)
    red = _red(tmp_path)

    out = Path(".themis") / "blind_tdd" / "green_report"
    out.mkdir(parents=True, exist_ok=True)
    (out / "TD118.json").write_text(
        json.dumps({
            "task": "TD118",
            "overall": "pass",
            "tests_passed": 1,
            "tests_failed": 0,
            "per_test": [{"name": "test_i_made_this_up", "result": "pass"}],
        }),
        encoding="utf-8",
    )

    green = orch.run_green_phase(TASK, red)

    assert green.passed is False
    assert "coverage gap" in green.reason


@pytest.mark.parametrize("bad", [
    "not a dict",
    {},
    {"overall": "pass", "per_test": []},
    {"overall": "pass", "per_test": [{"result": "pass"}]},
    {"overall": "pass", "per_test": [{"name": "t", "result": "probably"}]},
    {"overall": "green", "per_test": [{"name": "t", "result": "pass"}]},
])
def test_ac3_a_green_report_the_gate_could_not_read_is_refused_at_the_door(
    bad, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """`run_green_phase` reads `overall` and `per_test[].name/result` — only."""
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError):
        record_handshake("test_runner", "TD118", bad)
    assert not (
        Path(".themis") / "blind_tdd" / "green_report" / "TD118.json"
    ).exists()


# ---------------------------------------------------------------------------
# AC-4 — a recorder is not a way to declare green
# ---------------------------------------------------------------------------

def test_ac4_recording_a_handshake_on_failing_tests_does_not_go_green(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The whole risk of adding a recorder, tested directly.

    The contract here genuinely fails. Writing the handshake records that, and
    the green phase stays red — as it must, or the fix for TD118 would have
    been a rubber stamp for every task after it.
    """
    monkeypatch.chdir(tmp_path)
    orch = _project(tmp_path, passing=False)
    red = _red(tmp_path)

    path = record_runner_handshake("TD118", test_dirs=["tests/contracts/"])
    assert json.loads(path.read_text(encoding="utf-8"))["overall"] == "fail"

    green = orch.run_green_phase(TASK, red)

    assert green.passed is False
    # The criterion whose only test failed is reported as uncovered by a
    # PASSING test, which is the accurate reading — not "no test exists".
    assert "not_passing=['AC-1']" in green.reason


def test_ac4_the_measurement_reflects_the_tests_not_the_caller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Same call, opposite outcomes, decided by the code under test."""
    monkeypatch.chdir(tmp_path)

    _contract(tmp_path, passing=False)
    red_run = measure_test_run(["tests/contracts/"])
    _contract(tmp_path, passing=True)
    green_run = measure_test_run(["tests/contracts/"])

    assert red_run["overall"] == "fail"
    assert red_run["tests_failed"] == 1
    assert green_run["overall"] == "pass"
    assert green_run["tests_passed"] == 1


def test_ac4_a_run_that_collected_nothing_is_not_a_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """An empty test dir must not read as "no failures, therefore green"."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tests" / "contracts").mkdir(parents=True)

    result = measure_test_run(["tests/contracts/"])

    assert result["overall"] == "fail"
    assert result["tests_passed"] == 0


def test_ac4_a_recorder_with_nothing_to_run_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Silently recording a report for zero targets would be the rubber stamp."""
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError) as exc:
        record_runner_handshake("TD118", test_dirs=["tests/nonexistent/"])
    assert "nothing to run" in str(exc.value)


# ---------------------------------------------------------------------------
# AC-5 — one implementation, three roles
# ---------------------------------------------------------------------------

def test_ac5_every_recorder_writes_through_the_one_implementation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Three near-duplicate writers is how the runner's got forgotten.

    Each `record_*_handshake` builds and names its payload; none of them owns
    the validate-and-write.
    """
    monkeypatch.chdir(tmp_path)
    _contract(tmp_path, passing=True)
    seen: list[str] = []

    def _spy(role, artifact_id, payload, *, directory=None):
        seen.append(role)
        return Path("recorded")

    monkeypatch.setattr(orchestrator, "record_handshake", _spy)

    record_writer_handshake("TD118", {"triage": [{"criterion": "AC-1"}]})
    record_runner_handshake("TD118", test_dirs=["tests/contracts/"])
    record_arbiter_handshake("CH-1", {
        "ruling": "upheld", "criterion_affected": "AC-1", "reasoning": "why",
    })

    assert seen == ["test_writer", "test_runner", "arbiter"]


def test_ac5_the_arbiters_ruling_lands_where_its_consumer_reads_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """`challenge.spawn_arbiter` reads `rulings/<challenge_id>.json`, and
    discards — silently — any verdict outside the three it knows."""
    monkeypatch.chdir(tmp_path)

    path = record_arbiter_handshake(
        "CH-1",
        {"ruling": "upheld", "criterion_affected": "ac-1", "reasoning": "flawed"},
        task_id="TD118",
    )

    assert path == Path(".themis") / "blind_tdd" / "rulings" / "CH-1.json"
    ruling = json.loads(path.read_text(encoding="utf-8"))
    assert ruling["challenge_id"] == "CH-1"
    assert ruling["task"] == "TD118"

    with pytest.raises(ValueError):
        record_arbiter_handshake("CH-2", {
            "ruling": "sustained", "criterion_affected": "AC-1", "reasoning": "x",
        })


def test_ac5_the_writers_handshake_still_behaves_as_td150_left_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Generalising the recorder must not move the writer's artifact or shape."""
    monkeypatch.chdir(tmp_path)

    path = record_writer_handshake("TD146", {"triage": [
        {"criterion": "AC-1", "status": "tested"},
    ]})

    assert path == Path(".themis") / "blind_tdd" / "triage" / "TD146.json"
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["task"] == "TD146"
    assert loaded["triage"] == [{"criterion": "AC-1", "status": "tested"}]
