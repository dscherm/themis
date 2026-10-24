"""Task preflight checklist for the blind-TDD gate.

Runs between `schema_validator.validate_task` and the writer spawn in
`gate_integration.run_blind_tdd_gate`. Catches structural weaknesses in
tasks that are schema-valid but would waste API budget on a blind-writer
spawn:

  1. Subjective language in `then` clauses (schema_validator already
     warns; preflight PROMOTES to error under strict mode)
  2. Public-surface coverage gaps — every `public_surface.adds` must
     be mentioned by at least one criterion's `when` or `then`
  3. Criterion observability — every `then` must contain a numeric
     literal, a comparator/verb word, or an exception class reference
  4. public_api.md exists and DECLARES the module — a heading names
     it, in either spelling, with or without a trailing title. A
     mention in prose does not count; see `public_api_index`.
  5. Configured test directories exist

## Enforcement modes

`gate.blind_tdd.preflight` controls behavior:

  - `"strict"` (default): any error blocks the red phase. Warnings
    still logged.
  - `"warn"`: every check becomes a warning. Never blocks.
  - `"off"`: preflight skipped entirely.

## Escape hatch for genuinely qualitative criteria

A criterion can declare upfront that it will be escalated via
`"preclassified": "needs_human"`:

    {
      "id": "AC-5",
      "given": "the player triggers a win screen",
      "when": "the fade animation plays",
      "then": "the animation is visually smooth",
      "preclassified": "needs_human"
    }

Preflight skips subjective-language and observability checks for any
criterion with this flag, trusting that the blind writer will add it
to the triage report rather than try to assert on it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .public_api_index import declares_module


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

MODE_STRICT = "strict"
MODE_WARN = "warn"
MODE_OFF = "off"
_VALID_MODES = {MODE_STRICT, MODE_WARN, MODE_OFF}


# Check names, one per `_check_*` helper. They travel with each finding so a
# consumer can tell a missing public-api section from a subjective `then`
# clause without parsing the message prose — `lint_tasks` turns them into
# `preflight_<check>` finding kinds, which is what its LintFinding.kind
# docstring promised all along.
CHECK_SUBJECTIVE = "subjective"
CHECK_SURFACE = "surface"
CHECK_OBSERVABILITY = "observability"
CHECK_PUBLIC_API = "public_api"
CHECK_TEST_DIRS = "test_dirs"


@dataclass
class PreflightFinding:
    """One preflight complaint, tagged with the check that raised it."""
    check: str
    message: str


@dataclass
class PreflightResult:
    ready: bool
    mode: str = MODE_STRICT
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Every finding, whatever mode put it in `errors` or `warnings`, with
    # its originating check. `errors`/`warnings` stay plain strings so
    # existing callers keep working.
    findings: list[PreflightFinding] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ready": self.ready,
            "mode": self.mode,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "findings": [
                {"check": f.check, "message": f.message} for f in self.findings
            ],
        }


# ---------------------------------------------------------------------------
# Subjective language (duplicated-ish from schema_validator; the difference
# is that schema_validator warns at validate time while preflight can
# promote these to errors under strict mode)
# ---------------------------------------------------------------------------

# Same set as schema_validator._SUBJECTIVE_WORDS so behavior stays consistent.
_SUBJECTIVE_WORDS = frozenset({
    "smooth", "smoothly", "nice", "nicely", "clean", "cleanly", "fast",
    "quickly", "slow", "slowly", "beautiful", "pretty", "ugly",
    "good", "bad", "better", "worse", "easy", "easily", "simple",
    "simply", "natural", "naturally", "intuitive", "intuitively",
    "responsive", "laggy", "snappy", "crisp", "polished",
    "feels", "looks", "appears", "seems",
    "user-friendly", "user friendly", "idiomatic",
})


# ---------------------------------------------------------------------------
# Observability — regexes for concrete assertion language
# ---------------------------------------------------------------------------

# Numeric literal (integer or decimal, with optional sign)
_NUMERIC_RE = re.compile(r"(?:^|[\s(\[])-?\d+(?:\.\d+)?(?=$|[\s.,)\]])")

# Comparator/verb words. Whole-word matches only.
_COMPARATOR_WORDS = (
    "equal", "equals", "equaled", "matches", "matched",
    "returns", "returned", "raises", "raised", "throws", "thrown",
    "contains", "contained", "greater than", "less than",
    "exactly", "within", "no more than", "no less than",
    "at least", "at most", "must be",
)
_COMPARATOR_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(w) for w in _COMPARATOR_WORDS) + r")\b",
    re.IGNORECASE,
)

# Exception class reference (PascalCase ending in Error or Exception)
_EXCEPTION_CLASS_RE = re.compile(r"\b[A-Z][A-Za-z0-9_]*(Error|Exception)\b")

# Boolean literals
_BOOLEAN_RE = re.compile(r"\b(?:true|false|True|False)\b")


# ---------------------------------------------------------------------------
# Public surface identifier extraction
# ---------------------------------------------------------------------------

def _extract_identifier(signature: str) -> str | None:
    """Pull the most-referenced identifier out of a signature string.

    Examples:
        "Mathf.delta_angle(current: float) -> float" → "delta_angle"
        "GameObject.invoke_repeating(method, t)" → "invoke_repeating"
        "foo()" → "foo"
        "class Foo:" → "Foo"
        "property bar" → "bar"
        "async def tick()" → "tick"
    """
    if not signature or not isinstance(signature, str):
        return None

    s = signature.strip()
    # Strip leading keywords that aren't the identifier
    for kw in ("async def ", "async ", "def ", "class ", "static ",
               "public ", "private ", "protected ", "property "):
        if s.startswith(kw):
            s = s[len(kw):]

    # Take everything before the first `(` (call signature) or `:` (annotation)
    before_paren = s.split("(", 1)[0]
    before_annot = before_paren.split(":", 1)[0]
    # Tokenize on whitespace, take the last token
    tokens = before_annot.split()
    if not tokens:
        return None
    last = tokens[-1]
    # If it has dots (Class.method or module.Class.method), take the tail
    if "." in last:
        last = last.split(".")[-1]
    last = last.strip()
    if not last:
        return None
    # Identifier must start with letter or underscore
    if not (last[0].isalpha() or last[0] == "_"):
        return None
    # Strip trailing non-identifier characters
    last = re.sub(r"[^A-Za-z0-9_].*$", "", last)
    return last or None


# ---------------------------------------------------------------------------
# Per-check helpers
# ---------------------------------------------------------------------------

def _check_subjective_language(task: dict) -> list[str]:
    """Return error strings — one per criterion with subjective words
    in its `then` clause, EXCLUDING criteria flagged preclassified."""
    out: list[str] = []
    for crit in _criteria(task):
        if _is_preclassified(crit):
            continue
        then_text = str(crit.get("then", "")).lower()
        # Pad so word-boundary checks work at ends of string
        padded = f" {then_text} "
        hits = [w for w in _SUBJECTIVE_WORDS if f" {w} " in padded]
        if hits:
            cid = crit.get("id", "?")
            out.append(
                f"{cid}.then contains subjective language ({', '.join(hits)}). "
                f"Reword to an observable condition, or add "
                f'"preclassified": "needs_human" if the criterion is '
                f"genuinely qualitative."
            )
    return out


def _check_public_surface_coverage(task: dict) -> list[str]:
    """Return error strings for public_surface.adds entries that no
    criterion references by identifier."""
    surface = task.get("public_surface") or {}
    adds = surface.get("adds") or []
    if not isinstance(adds, list) or not adds:
        return []

    criteria = list(_criteria(task))
    # Collect all text from when/then of all criteria
    haystack = " ".join(
        f"{c.get('when', '')} {c.get('then', '')}"
        for c in criteria
        if isinstance(c, dict)
    ).lower()

    out: list[str] = []
    for sig in adds:
        if not isinstance(sig, str):
            continue
        ident = _extract_identifier(sig)
        if not ident:
            continue
        # Case-insensitive word boundary match
        pattern = r"\b" + re.escape(ident.lower()) + r"\b"
        if not re.search(pattern, haystack):
            out.append(
                f"public_surface.adds entry {sig!r} (identifier {ident!r}) "
                f"is not mentioned by any criterion's `when` or `then`. "
                f"Either add a criterion that exercises it or remove it "
                f"from public_surface."
            )
    return out


def _check_criterion_observability(task: dict) -> list[str]:
    """Return error strings for criteria whose `then` clause has no
    concrete assertion language (numeric, comparator, exception, boolean)."""
    out: list[str] = []
    for crit in _criteria(task):
        if _is_preclassified(crit):
            continue
        then_text = str(crit.get("then", ""))
        if not then_text.strip():
            continue  # schema_validator already caught empty
        if _has_observable_assertion(then_text):
            continue
        cid = crit.get("id", "?")
        out.append(
            f"{cid}.then has no observable assertion language "
            f"(no numeric value, comparator word, exception class, or "
            f"boolean literal). Rewrite with a concrete value/behavior, "
            f'or add "preclassified": "needs_human".'
        )
    return out


def _has_observable_assertion(text: str) -> bool:
    """True if `text` contains at least one concrete assertion signal."""
    if _NUMERIC_RE.search(text):
        return True
    if _COMPARATOR_RE.search(text):
        return True
    if _EXCEPTION_CLASS_RE.search(text):
        return True
    if _BOOLEAN_RE.search(text):
        return True
    return False


def _check_public_api_file(task: dict, config: dict) -> list[str]:
    """Verify `public_api.md` exists and mentions public_surface.module."""
    btd = (config.get("gate") or {}).get("blind_tdd") or {}
    api_file = btd.get("public_api_file")
    if not api_file:
        return []  # not configured, nothing to check

    surface = task.get("public_surface") or {}
    module = surface.get("module")
    if not module or not isinstance(module, str):
        return []  # schema_validator covers this

    p = Path(api_file)
    if not p.exists():
        return [
            f"public_api_file {api_file!r} does not exist. Create it and "
            f"document the public surface of {module!r}."
        ]
    try:
        content = p.read_text(encoding="utf-8")
    except OSError as e:
        return [f"could not read {api_file!r}: {e}"]

    # Heading-aware, not substring. `module` may be written either way
    # ("server/unit_store.py" or "server.unit_store") and the document may
    # head its section either way, with or without a trailing title —
    # `declares_module` normalises both spellings to one key. A name that
    # appears only in prose still warns: a mention is not a declared
    # surface (see public_api_index's module docstring).
    if not declares_module(content, module):
        return [
            f"public_api_file {api_file!r} does not mention {module!r}. "
            f"The blind writer needs this file to understand the surface "
            f"it's testing — add a section for the module."
        ]
    return []


def _check_test_dirs_exist(config: dict) -> list[str]:
    """Verify every configured test_dir exists on disk."""
    btd = (config.get("gate") or {}).get("blind_tdd") or {}
    test_dirs = btd.get("test_dirs") or []
    out: list[str] = []
    for d in test_dirs:
        p = Path(d)
        if not p.exists():
            out.append(
                f"test_dir {d!r} does not exist. The blind writer will "
                f"fail to create tests there. Create the directory or "
                f"remove it from gate.blind_tdd.test_dirs."
            )
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _criteria(task: dict) -> Iterable[dict]:
    """Yield criterion dicts from a task, skipping non-dict entries."""
    crits = (task or {}).get("acceptance_criteria") or []
    if not isinstance(crits, list):
        return
    for c in crits:
        if isinstance(c, dict):
            yield c


def _is_preclassified(crit: dict) -> bool:
    """True if a criterion opts out of observability checks via
    `preclassified: needs_human`."""
    return str(crit.get("preclassified", "")).lower() == "needs_human"


def _normalize_mode(mode: str) -> str:
    mode = (mode or MODE_STRICT).lower().strip()
    if mode not in _VALID_MODES:
        return MODE_STRICT
    return mode


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------

def preflight_task(task: dict, config: dict) -> PreflightResult:
    """Run all preflight checks against a task spec.

    Args:
        task: the task dict (should already have passed schema_validator)
        config: the project config (used to read gate.blind_tdd.preflight,
            public_api_file, and test_dirs)

    Returns:
        PreflightResult with mode and ready flag. Under `"strict"`, any
        check failure sets ready=False. Under `"warn"`, failures become
        warnings and ready=True. Under `"off"`, all checks are skipped
        and ready=True.
    """
    btd = (config.get("gate") or {}).get("blind_tdd") or {}
    mode = _normalize_mode(btd.get("preflight", MODE_STRICT))

    if mode == MODE_OFF:
        return PreflightResult(ready=True, mode=mode)

    # Collect findings from every check — structural problems are
    # reported independently (each can fail without masking others).
    tagged: list[PreflightFinding] = []
    for check, messages in (
        (CHECK_SUBJECTIVE, _check_subjective_language(task)),
        (CHECK_SURFACE, _check_public_surface_coverage(task)),
        (CHECK_OBSERVABILITY, _check_criterion_observability(task)),
        (CHECK_PUBLIC_API, _check_public_api_file(task, config)),
        (CHECK_TEST_DIRS, _check_test_dirs_exist(config)),
    ):
        tagged.extend(PreflightFinding(check=check, message=m) for m in messages)

    messages = [f.message for f in tagged]

    if mode == MODE_WARN:
        return PreflightResult(
            ready=True,
            mode=mode,
            warnings=messages,
            findings=tagged,
        )

    # strict
    return PreflightResult(
        ready=len(tagged) == 0,
        mode=mode,
        errors=messages,
        findings=tagged,
    )
