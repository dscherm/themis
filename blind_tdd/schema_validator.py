"""Schema validator for blind-TDD task spec fields.

Validates that a plan.md task (parsed from its JSON block) carries the
required `acceptance_criteria` and `public_surface` fields with correct
structure. Called by blind_gate before spawning Agent #1 — tasks missing
required fields are rejected with a specific reason.

## Required fields

### acceptance_criteria (list of objects)

Each object must have:
- id: string like "AC-1", "AC-2" (must be unique within the task)
- given: string (precondition)
- when: string (action)
- then: string (expected outcome)

Optional fields:
- examples: list of dicts (parameterized cases)
- notes: string (edge cases, constraints)

### public_surface (object)

Required fields:
- module: string (import path, e.g. "src.engine.core" or "./components/Player")
- adds: list of strings (signatures for new functions/methods/classes)

Optional fields:
- class: string (if the task modifies a specific class)
- modifies: list of strings (signatures for changed members)
- external_refs: list of URL strings

## Usage

    from blind_tdd.schema_validator import validate_task, ValidationError

    task = {"id": "task-42", "acceptance_criteria": [...], "public_surface": {...}}
    try:
        validate_task(task)
    except ValidationError as e:
        print(f"Task invalid: {e}")
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


class ValidationError(Exception):
    """Raised when a task spec fails blind-TDD schema validation."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors) if errors else "validation failed")


@dataclass
class ValidationResult:
    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


_AC_ID_PATTERN = re.compile(r"^AC-\d+$")


def _validate_acceptance_criteria(
    criteria: Any, errors: list[str], warnings: list[str]
) -> None:
    if not isinstance(criteria, list):
        errors.append(
            f"acceptance_criteria must be a list, got {type(criteria).__name__}"
        )
        return

    if len(criteria) == 0:
        errors.append(
            "acceptance_criteria must have at least one criterion"
        )
        return

    seen_ids: set[str] = set()
    for i, crit in enumerate(criteria):
        prefix = f"acceptance_criteria[{i}]"

        if not isinstance(crit, dict):
            errors.append(f"{prefix} must be an object, got {type(crit).__name__}")
            continue

        # id
        crit_id = crit.get("id")
        if not crit_id:
            errors.append(f"{prefix}.id is required")
        elif not isinstance(crit_id, str):
            errors.append(f"{prefix}.id must be a string")
        elif not _AC_ID_PATTERN.match(crit_id):
            warnings.append(
                f"{prefix}.id={crit_id!r} should match pattern 'AC-<number>' "
                f"(e.g. AC-1)"
            )
        elif crit_id in seen_ids:
            errors.append(f"{prefix}.id={crit_id!r} is duplicated")
        else:
            seen_ids.add(crit_id)

        # given
        if "given" not in crit:
            errors.append(f"{prefix}.given is required")
        elif not isinstance(crit["given"], str) or not crit["given"].strip():
            errors.append(f"{prefix}.given must be a non-empty string")

        # when
        if "when" not in crit:
            errors.append(f"{prefix}.when is required")
        elif not isinstance(crit["when"], str) or not crit["when"].strip():
            errors.append(f"{prefix}.when must be a non-empty string")

        # then
        if "then" not in crit:
            errors.append(f"{prefix}.then is required")
        elif not isinstance(crit["then"], str) or not crit["then"].strip():
            errors.append(f"{prefix}.then must be a non-empty string")

        # Optional: examples (list of dicts)
        if "examples" in crit:
            ex = crit["examples"]
            if not isinstance(ex, list):
                errors.append(f"{prefix}.examples must be a list if present")
            else:
                for j, example in enumerate(ex):
                    if not isinstance(example, dict):
                        errors.append(
                            f"{prefix}.examples[{j}] must be an object"
                        )

        # Optional: notes (string)
        if "notes" in crit:
            if not isinstance(crit["notes"], str):
                errors.append(f"{prefix}.notes must be a string if present")

        # Warn on suspicious phrasing that indicates the criterion is
        # likely to need human escalation.
        _flag_subjective_language(prefix, crit, warnings)
        # Warn when multi-case phrasing is present but examples aren't.
        _flag_multi_case_without_examples(prefix, crit, warnings)


_SUBJECTIVE_WORDS = {
    "smooth", "smoothly", "nice", "nicely", "clean", "cleanly", "fast",
    "quickly", "slow", "slowly", "beautiful", "pretty", "ugly",
    "good", "bad", "better", "worse", "easy", "easily", "simple",
    "simply", "natural", "naturally", "intuitive", "intuitively",
    "responsive", "laggy", "snappy", "crisp", "polished",
    "feels", "looks", "appears", "seems",
    "user-friendly", "user friendly", "idiomatic",
}


def _flag_subjective_language(prefix: str, crit: dict, warnings: list[str]) -> None:
    """Warn if a criterion uses subjective language likely to need human escalation."""
    then_text = str(crit.get("then", "")).lower()
    hits = [w for w in _SUBJECTIVE_WORDS if f" {w} " in f" {then_text} "]
    if hits:
        warnings.append(
            f"{prefix}.then contains subjective language: {', '.join(hits)}. "
            f"Consider rewording to an objective, observable property, or "
            f"expect the test writer to escalate this criterion as needs_human."
        )


# Phrases in a criterion's `when`/`then` that imply multiple test cases.
# If these appear but the criterion has no `examples` field, warn so the
# author considers adding explicit examples for parametrize.
_MULTI_CASE_PHRASES = (
    "for every", "for each", "for all", "given any",
    "wraps around", "wraps across", "normalizes",
    "regardless of", "across the range",
    "inputs outside", "boundary", "boundaries",
    "each value", "every value",
    "in the range", "outside the range",
)


def _flag_multi_case_without_examples(prefix: str, crit: dict, warnings: list[str]) -> None:
    """Warn when a criterion uses multi-case phrasing but has no `examples` list."""
    existing = crit.get("examples")
    if isinstance(existing, list) and existing:
        return  # already has examples, no warning needed
    then_text = str(crit.get("then", "")).lower()
    when_text = str(crit.get("when", "")).lower()
    combined = f" {when_text} {then_text} "
    hits = []
    for phrase in _MULTI_CASE_PHRASES:
        # Allow terminal punctuation or whitespace on either side
        if f" {phrase} " in combined or f" {phrase}." in combined or f" {phrase}," in combined:
            hits.append(phrase)
    if hits:
        warnings.append(
            f"{prefix} uses multi-case phrasing ({', '.join(hits)}) but has "
            f"no `examples` field. Consider adding a list of example dicts "
            f"so the blind writer can emit a parameterized test."
        )


def _validate_public_surface(
    surface: Any, errors: list[str], warnings: list[str]
) -> None:
    if not isinstance(surface, dict):
        errors.append(
            f"public_surface must be an object, got {type(surface).__name__}"
        )
        return

    # module
    module = surface.get("module")
    if not module:
        errors.append("public_surface.module is required")
    elif not isinstance(module, str):
        errors.append("public_surface.module must be a string")

    # adds (list of signature strings)
    adds = surface.get("adds")
    if adds is None:
        errors.append("public_surface.adds is required (can be empty list)")
    elif not isinstance(adds, list):
        errors.append("public_surface.adds must be a list")
    else:
        for i, sig in enumerate(adds):
            if not isinstance(sig, str):
                errors.append(
                    f"public_surface.adds[{i}] must be a string (signature), "
                    f"got {type(sig).__name__}"
                )
            elif not sig.strip():
                errors.append(f"public_surface.adds[{i}] must be non-empty")

    # modifies (optional list of signature strings)
    if "modifies" in surface:
        mods = surface["modifies"]
        if not isinstance(mods, list):
            errors.append("public_surface.modifies must be a list if present")
        else:
            for i, sig in enumerate(mods):
                if not isinstance(sig, str):
                    errors.append(
                        f"public_surface.modifies[{i}] must be a string"
                    )

    # class (optional)
    if "class" in surface:
        if not isinstance(surface["class"], str):
            errors.append("public_surface.class must be a string if present")

    # external_refs (optional list of URLs)
    if "external_refs" in surface:
        refs = surface["external_refs"]
        if not isinstance(refs, list):
            errors.append("public_surface.external_refs must be a list if present")
        else:
            for i, ref in enumerate(refs):
                if not isinstance(ref, str):
                    errors.append(
                        f"public_surface.external_refs[{i}] must be a string URL"
                    )
                elif not (ref.startswith("http://") or ref.startswith("https://")):
                    warnings.append(
                        f"public_surface.external_refs[{i}]={ref!r} does not "
                        f"look like a URL"
                    )

    # Empty adds AND empty modifies is suspicious — task probably doesn't
    # need blind-TDD
    if isinstance(adds, list) and not adds:
        if "modifies" not in surface or not surface.get("modifies"):
            warnings.append(
                "public_surface has no adds or modifies. This task may not "
                "need blind-TDD validation."
            )


def validate_task(task: dict) -> ValidationResult:
    """Validate a task dict against the blind-TDD schema.

    Returns a ValidationResult with errors and warnings. Does not raise.
    """
    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(task, dict):
        return ValidationResult(
            valid=False,
            errors=[f"task must be an object, got {type(task).__name__}"],
        )

    # Basic fields (id is helpful but not strictly required by blind-TDD)
    if "id" not in task and "description" not in task:
        warnings.append("task has neither 'id' nor 'description' field")

    # acceptance_criteria
    if "acceptance_criteria" not in task:
        errors.append(
            "task missing required field 'acceptance_criteria' (list of "
            "Given/When/Then objects with criterion IDs)"
        )
    else:
        _validate_acceptance_criteria(
            task["acceptance_criteria"], errors, warnings
        )

    # public_surface
    if "public_surface" not in task:
        errors.append(
            "task missing required field 'public_surface' (object with module, "
            "adds, and optional modifies/external_refs)"
        )
    else:
        _validate_public_surface(task["public_surface"], errors, warnings)

    return ValidationResult(
        valid=len(errors) == 0,
        errors=errors,
        warnings=warnings,
    )


def validate_or_raise(task: dict) -> None:
    """Validate a task. Raises ValidationError on failure."""
    result = validate_task(task)
    if not result.valid:
        raise ValidationError(result.errors)


def get_criterion_ids(task: dict) -> list[str]:
    """Extract the list of criterion IDs from a validated task."""
    criteria = task.get("acceptance_criteria", [])
    if not isinstance(criteria, list):
        return []
    return [
        str(c["id"]) for c in criteria
        if isinstance(c, dict) and "id" in c
    ]


if __name__ == "__main__":
    # CLI: validate task JSON from stdin
    import json
    import sys

    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(f"Invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)

    # Accept either a single task or a list
    tasks = data if isinstance(data, list) else [data]

    total_errors = 0
    for i, task in enumerate(tasks):
        result = validate_task(task)
        label = task.get("id") or f"task[{i}]"
        if result.valid:
            print(f"[OK] {label}")
            if result.warnings:
                for w in result.warnings:
                    print(f"  WARN: {w}")
        else:
            print(f"[FAIL] {label}")
            for e in result.errors:
                print(f"  ERROR: {e}")
            for w in result.warnings:
                print(f"  WARN: {w}")
            total_errors += len(result.errors)

    sys.exit(0 if total_errors == 0 else 1)
