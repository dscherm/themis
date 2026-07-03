"""Tests for the artifact/ignore filter that keeps build and redirect
artifacts out of hashed / discovered file sets.

Regression coverage for the bug class where a `.pyc` in a locked test dir, a
literal `NUL` redirect file, or a vendored `test_*.py` under `node_modules/`
polluted a computed file set and produced a false gate verdict. The declared
oracle files stay byte-strict: a genuinely new `.py` test still changes the
hash set (see `test_new_real_test_file_still_changes_hash_set`).

Run with:
    python -m pytest blind_tdd/test_artifacts.py
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath

from blind_tdd.artifacts import is_artifact_path
from blind_tdd.coverage import extract_covers
from blind_tdd.orchestrator import _hash_test_files


# ---------------------------------------------------------------------------
# is_artifact_path — unit
# ---------------------------------------------------------------------------

def test_pyc_and_pyo_are_artifacts():
    assert is_artifact_path("tests/contracts/test_calc.pyc")
    assert is_artifact_path("tests/contracts/foo.pyo")
    assert is_artifact_path("tests/contracts/bar.pyd")


def test_pycache_dir_component_is_artifact():
    assert is_artifact_path(
        "tests/contracts/__pycache__/test_calc.cpython-39-pytest-8.4.2.pyc"
    )
    # Even a stray .py inside __pycache__ is treated as an artifact.
    assert is_artifact_path("tests/__pycache__/weird.py")


def test_cache_and_vendor_dirs_are_artifacts():
    assert is_artifact_path("tests/.pytest_cache/v/cache/lastfailed")
    assert is_artifact_path("tests/node_modules/pkg/test_vendored.py")
    assert is_artifact_path("tests/.mypy_cache/x.json")
    assert is_artifact_path("tests/.ruff_cache/y")
    assert is_artifact_path(".git/objects/ab/cdef")


def test_nul_file_is_artifact_case_insensitive():
    # The Windows `>NUL` redirect artifact created under git-bash.
    assert is_artifact_path("NUL")
    assert is_artifact_path("tests/contracts/NUL")
    assert is_artifact_path("tests/contracts/nul")


def test_real_test_file_is_not_an_artifact():
    assert not is_artifact_path("tests/contracts/test_calc.py")
    assert not is_artifact_path("tests/integration/test_flow.js")
    assert not is_artifact_path("src/calc.py")


def test_artifact_dir_match_is_component_exact_not_substring():
    # A legitimately-named directory that merely contains an artifact-dir name
    # as a substring must NOT be excluded.
    assert not is_artifact_path("tests/node_modules_shim/test_x.py")
    assert not is_artifact_path("tests/my__pycache__helper/test_y.py")


def test_matches_across_path_flavors():
    assert is_artifact_path(PureWindowsPath(r"tests\contracts\__pycache__\a.pyc"))
    assert is_artifact_path(PurePosixPath("tests/contracts/__pycache__/a.pyc"))
    assert is_artifact_path(Path("tests") / "node_modules" / "p" / "test_a.py")


# ---------------------------------------------------------------------------
# _hash_test_files — the hash-lock snapshot must ignore artifacts
# ---------------------------------------------------------------------------

def test_pyc_in_locked_dir_does_not_change_hash_set(tmp_path):
    """A .pyc appearing in a locked test dir between red and green must not
    change the sealed hash set — otherwise the gate fails on noise."""
    td = tmp_path / "tests" / "contracts"
    td.mkdir(parents=True)
    (td / "test_a.py").write_text("def test_a():\n    assert True\n", encoding="utf-8")

    before = _hash_test_files([tmp_path / "tests" / "contracts"])

    # Simulate pytest compiling bytecode into the locked dir.
    cache = td / "__pycache__"
    cache.mkdir()
    (cache / "test_a.cpython-39-pytest-8.4.2.pyc").write_bytes(b"\x00\x01BYTECODE")

    after = _hash_test_files([tmp_path / "tests" / "contracts"])

    assert set(before) == set(after), "artifact must not enter the hash set"
    assert before == after


def test_nul_file_in_locked_dir_does_not_change_hash_set(tmp_path):
    td = tmp_path / "tests" / "contracts"
    td.mkdir(parents=True)
    (td / "test_a.py").write_text("def test_a():\n    assert True\n", encoding="utf-8")
    before = _hash_test_files([td])

    (td / "NUL").write_bytes(b"redirect junk")
    after = _hash_test_files([td])

    assert before == after


def test_vendored_test_under_node_modules_is_not_hashed(tmp_path):
    td = tmp_path / "tests" / "contracts"
    td.mkdir(parents=True)
    (td / "test_a.py").write_text("def test_a():\n    assert True\n", encoding="utf-8")
    vendored = td / "node_modules" / "leftpad"
    vendored.mkdir(parents=True)
    (vendored / "test_vendored.py").write_text("def test_v():\n    pass\n", encoding="utf-8")

    hashes = _hash_test_files([td])
    assert len(hashes) == 1
    assert all("node_modules" not in k for k in hashes)


def test_new_real_test_file_still_changes_hash_set(tmp_path):
    """Guard against over-filtering: a genuinely new .py test (the sneak-in
    attack) must still change the hash set."""
    td = tmp_path / "tests" / "contracts"
    td.mkdir(parents=True)
    (td / "test_a.py").write_text("def test_a():\n    assert True\n", encoding="utf-8")
    before = _hash_test_files([td])

    (td / "test_sneaky.py").write_text("def test_s():\n    assert True\n", encoding="utf-8")
    after = _hash_test_files([td])

    assert set(after) - set(before), "a new real test file must break the seal"


# ---------------------------------------------------------------------------
# extract_covers — coverage discovery must ignore artifacts
# ---------------------------------------------------------------------------

def test_extract_covers_ignores_vendored_annotations(tmp_path):
    td = tmp_path / "tests" / "contracts"
    td.mkdir(parents=True)
    (td / "test_a.py").write_text(
        'def test_a():\n    """Covers: AC-1"""\n    assert True\n', encoding="utf-8"
    )
    vendored = td / "node_modules" / "pkg"
    vendored.mkdir(parents=True)
    (vendored / "test_vendored.py").write_text(
        'def test_v():\n    """Covers: AC-2"""\n    assert True\n', encoding="utf-8"
    )

    anns = extract_covers(td)
    covered = {ac for a in anns for ac in a.covers}
    assert covered == {"AC-1"}, "vendored node_modules test must not be discovered"
