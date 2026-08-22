"""Heading-aware `public_api.md` matching (TD195, AC-2/AC-6/AC-7).

Every fixture below is copied VERBATIM from in-the-loop-learning's real
`public_api.md` (line numbers as of 2026-08-22 — that file grows daily, so
the headings are quoted in full and the numbers are a pointer, not a key).
The check they exercise used to be `if module not in content`: a raw
substring test that missed the dotted spelling entirely and would have
accepted a name that appeared only in prose.
"""

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

import pytest

from blind_tdd.preflight import _check_public_api_file
from blind_tdd.public_api_index import (
    declared_modules,
    declares_module,
    heading_module_token,
    normalise_module_key,
)


# --- real headings, verbatim ------------------------------------------------

# public_api.md:282
H_CORE_SCHEMAS = "## core.schemas — the unit taxonomy (TD45)"
# public_api.md:2775
H_SERVER_BACKUP = (
    "## server.backup — the copy of the student record, and the roots it "
    "covers (TD193)"
)
# public_api.md:542 — the one module heading with no trailing title at all
H_UNIT_STORE = "## server.unit_store"
# public_api.md:2208 — the coaching routes, added by c29a897 (AC-2)
H_COACHING = (
    "## server.api — the teacher's coaching surface: directive, nudge, "
    "proposals (TD192)"
)
# public_api.md:894 — a `###` SUB-heading inside the `## server.unit_store`
# section, added by cdeacec (AC-2)
H_ORPHAN_SUB = (
    "### `server.unit_store` — the orphan store: a milestone with no unit "
    "yet (TD189, D8s)"
)
# public_api.md:316 — a heading for one FIELD of a module's surface
H_UNIT_SUMMARY = (
    "## core.schemas.Unit.summary — the paragraph, not the backward-design "
    "sentences (TD103)"
)


@contextmanager
def _chdir(d):
    prev = os.getcwd()
    os.chdir(d)
    try:
        yield
    finally:
        os.chdir(prev)


def _write(text: str) -> Path:
    p = Path("public_api.md")
    p.write_text(text, encoding="utf-8")
    return p


def _config():
    return {"gate": {"blind_tdd": {"public_api_file": "public_api.md"}}}


def _task(module):
    return {
        "id": "T-1",
        "public_surface": {"module": module, "adds": []},
        "acceptance_criteria": [],
    }


# --- normalisation ----------------------------------------------------------

@pytest.mark.parametrize("spelling", [
    "server/unit_store.py",
    "server.unit_store",
    "server\\unit_store.py",
    "  server/unit_store.py  ",
])
def test_both_spellings_normalise_to_one_key(spelling):
    assert normalise_module_key(spelling) == "server.unit_store"


# --- heading token extraction ----------------------------------------------

@pytest.mark.parametrize("line,expected", [
    (H_CORE_SCHEMAS, (2, "core.schemas")),
    (H_SERVER_BACKUP, (2, "server.backup")),
    (H_UNIT_STORE, (2, "server.unit_store")),
    (H_COACHING, (2, "server.api")),
    (H_ORPHAN_SUB, (3, "server.unit_store")),
    ("## `server/unit_store.py`", (2, "server/unit_store.py")),
    ("## `server.backup` — the copy of the record", (2, "server.backup")),
])
def test_heading_token_stops_at_the_module_name(line, expected):
    assert heading_module_token(line) == expected


@pytest.mark.parametrize("line", [
    "## Notes",
    "## HTTP surface (server.api)",
    "### The BACKUP ROOTS — what is in an archive, and what is not",
    "### The storage → runtime seam",
    "not a heading at all: server.unit_store",
    "#### ",
])
def test_prose_headings_name_no_module(line):
    assert heading_module_token(line) is None


# --- AC-6: dotted heading WITH a trailing title counts ----------------------

@pytest.mark.parametrize("heading,module", [
    (H_CORE_SCHEMAS, "core/schemas.py"),
    (H_SERVER_BACKUP, "server/backup.py"),
    (H_UNIT_STORE, "server/unit_store.py"),
    (H_COACHING, "server/api.py"),
])
def test_ac6_dotted_heading_with_title_satisfies_a_path_form_module(heading, module):
    """AC-6. The best-documented modules in in-the-loop-learning were all
    warned about, because the task declares `core/schemas.py` while the
    document heads its section `## core.schemas — the unit taxonomy (TD45)`.
    """
    with tempfile.TemporaryDirectory() as td:
        with _chdir(td):
            _write("# Public API\n\n" + heading + "\n\n- `thing()` — does it.\n")
            assert _check_public_api_file(_task(module), _config()) == []


def test_ac6_dotted_module_string_also_passes():
    with tempfile.TemporaryDirectory() as td:
        with _chdir(td):
            _write("# Public API\n\n" + H_CORE_SCHEMAS + "\n")
            assert _check_public_api_file(_task("core.schemas"), _config()) == []


def test_a_backticked_path_heading_satisfies_a_dotted_module_string():
    """The mirror case: heading written as a path, task written dotted."""
    with tempfile.TemporaryDirectory() as td:
        with _chdir(td):
            _write("# Public API\n\n## `server/unit_store.py`\n")
            errors = _check_public_api_file(_task("server.unit_store"), _config())
            assert errors == []


def test_a_field_level_heading_declares_part_of_its_modules_surface():
    """`## core.schemas.Unit.summary — …` documents part of core.schemas."""
    with tempfile.TemporaryDirectory() as td:
        with _chdir(td):
            _write("# Public API\n\n" + H_UNIT_SUMMARY + "\n")
            assert _check_public_api_file(_task("core/schemas.py"), _config()) == []


# --- AC-7: a prose mention is still not a declared surface ------------------

def test_ac7_prose_mention_only_still_warns():
    """AC-7. Widening the match to the dotted spelling must NOT become 'any
    mention counts' — `a-declared-surface-is-a-name-list-not-a-contract`
    still governs. This is the control for every AC-6 case above."""
    with tempfile.TemporaryDirectory() as td:
        with _chdir(td):
            _write(
                "# Public API\n\n"
                "## server.api — the routes\n\n"
                "- `get_unit()` — reads a unit. Internally this calls into\n"
                "  server.unit_store and core.schemas, neither of which is\n"
                "  documented here. See also `core/tutor.py`.\n"
            )
            for module in ("server/unit_store.py", "core.schemas", "core/tutor.py"):
                errors = _check_public_api_file(_task(module), _config())
                assert len(errors) == 1, module
                assert "does not mention" in errors[0]


def test_ac7_a_sub_heading_does_not_declare_a_module_of_its_own():
    """A `###` belongs to the `##` above it. in-the-loop-learning's
    public_api.md:1099 heads a sub-section
    "### `core.schemas` / `server.unit_store` — a milestone may REFERENCE a
    unit" inside the server.unit_store section; reading that as a
    declaration of core.schemas would credit a surface nobody wrote."""
    with tempfile.TemporaryDirectory() as td:
        with _chdir(td):
            _write(
                "# Public API\n\n" + H_UNIT_STORE + "\n\n"
                "### `core.schemas` / `server.unit_store` — a milestone may "
                "REFERENCE a unit (TD181, D8w)\n"
            )
            text = Path("public_api.md").read_text(encoding="utf-8")
            assert declared_modules(text) == {"server.unit_store"}
            errors = _check_public_api_file(_task("core/schemas.py"), _config())
            assert len(errors) == 1
            assert "does not mention" in errors[0]


# --- AC-2: the two surfaces that ARE adequate must not be flagged ----------

def test_ac2_coaching_routes_and_orphan_store_are_recognised():
    """AC-2, measured against the two sections the bead names: the coaching
    routes (c29a897) head their own `## server.api` section; the orphan
    store (cdeacec) is a `###` INSIDE `## server.unit_store`, and is
    recognised through the module heading it sits under."""
    with tempfile.TemporaryDirectory() as td:
        with _chdir(td):
            _write(
                "# Public API\n\n"
                + H_UNIT_STORE + "\n\n"
                "- `load_unit(unit_id: str) -> Unit` — reads a unit.\n\n"
                + H_ORPHAN_SUB + "\n\n"
                "- `save_orphan(segment: Segment) -> str` — stores it.\n\n"
                + H_COACHING + "\n\n"
                "- `POST /api/teacher/directive` — steers a lesson.\n"
            )
            assert _check_public_api_file(_task("server/unit_store.py"), _config()) == []
            assert _check_public_api_file(_task("server/api.py"), _config()) == []
            assert _check_public_api_file(_task("server.unit_store"), _config()) == []


def test_a_one_segment_module_key_does_not_swallow_every_dotted_heading():
    """`server` must not be satisfied by `## server.api`; the prefix rule is
    for a dotted key naming a real module, not for a package prefix."""
    text = "# Public API\n\n" + H_COACHING + "\n"
    assert declares_module(text, "server.api") is True
    assert declares_module(text, "server") is False
