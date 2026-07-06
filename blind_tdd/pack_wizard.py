"""Security-pack tailoring wizard — the engine behind the pack interview.

The default security AC pack is generic by necessity ("any public entry
point"), and generic criteria derive weaker tests — the documented biggest
weakness of the pack (see limitations.md). The fix is a tailored,
project-specific pack, and the honest way to get one is an interview with
the operator: explore the repo's real entry points and sinks, ask the human
to confirm what matters, draft concrete criteria, and install the result.

The interview dialog itself is LLM-driven (the `/blind-tdd:security-pack-setup`
plugin command conducts it in-session, one question at a time, exploring the
repo before asking). This module is the deterministic engine under it, and it
gives the interview something deep-interview-style workflows usually lack: an
**objective exit gate**. Instead of a subjective ambiguity score, a draft pack
is done when `lint_pack_draft` is clean — the same preflight checks the gate
itself applies to acceptance criteria (observable `then` language, no
subjective words), plus structural pack validation and a genericity warning
for criteria that never name a concrete surface.

Trust placement: the LLM only *drafts*. The human approves every criterion,
and installation goes through this CLI with an explicit confirmation (or
`--yes` from the command after the human said yes). The installed pack is a
project-root file meant to be committed and reviewed like any spec change —
the gate fingerprints it at red and polices it at green (security_pack.py).

CLI:

    python -m blind_tdd.pack_wizard --lint draft.json
    python -m blind_tdd.pack_wizard --install draft.json \
        [--pack-path themis.security_pack.json] \
        [--match-glob 'auth/**' ...] [--match-tag security ...] \
        [--match-keyword password ...] [--match-min-severity high] \
        [--config themis.config.json] [--print] [--yes]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .init import DEFAULT_CONFIG_PATH, load_config
from .preflight import (
    _check_criterion_observability,
    _check_subjective_language,
)
from .routing import SEVERITY_RANK, normalize_routing
from .security_pack import validate_pack

# Default install location: project root, next to themis.config.json — the
# pack is spec content that belongs in version control, not in .themis state.
DEFAULT_PACK_PATH = "themis.security_pack.json"

# Phrases that mark a criterion as untailored. Presence is a warning, never an
# error — the shipped default pack is deliberately generic and must stay
# installable; the wizard's job is to do better than it.
_GENERIC_PHRASES = (
    "any public entry point",
    "a public entry point",
    "public_surface",
)


def lint_pack_draft(pack: object) -> tuple[list[str], list[str]]:
    """(errors, warnings) for a draft pack — the interview's objective exit gate.

    Errors block install: structural problems (validate_pack) and the same
    criterion-quality failures the gate's preflight would raise once the pack
    is injected (subjective `then` language, no observable assertion).
    Warnings don't block: genericity (a criterion that names no concrete
    surface) and a missing recommended field.
    """
    errors = validate_pack(pack)
    if errors:
        return errors, []
    assert isinstance(pack, dict)  # validate_pack guarantees this

    warnings: list[str] = []

    # Reuse preflight's checks by dressing the criteria as a synthetic task.
    criteria = [
        {**crit, "id": f"AC-{i + 1}"}
        for i, crit in enumerate(pack.get("criteria", []))
        if isinstance(crit, dict)
    ]
    synthetic_task = {"acceptance_criteria": criteria}
    errors.extend(_check_subjective_language(synthetic_task))
    errors.extend(_check_criterion_observability(synthetic_task))

    for i, crit in enumerate(criteria):
        label = crit.get("category") or crit["id"]
        blob = " ".join(
            str(crit.get(k, "")) for k in ("given", "when", "then")
        ).lower()
        if any(phrase in blob for phrase in _GENERIC_PHRASES):
            warnings.append(
                f"criteria[{i}] ({label}) looks generic — it speaks of "
                f"'a public entry point' instead of naming the project's "
                f"actual functions/modules. Generic criteria derive weaker "
                f"tests; name the concrete surface."
            )
        if not str(crit.get("notes", "")).strip():
            warnings.append(
                f"criteria[{i}] ({label}) has no notes. Notes carry the "
                f"instantiation guidance the blind writer reads — say what "
                f"to test and when to triage needs_human."
            )

    return errors, warnings


def build_match(
    *,
    path_globs: list[str],
    tags: list[str],
    keywords: list[str],
    min_severity: str | None,
) -> dict:
    """Normalize match predicates for the security_ac_pack config block.

    Empty predicates → normalized mode "all": the pack applies to every
    gated task, which is the safe default (routing already scoped the gate).
    """
    raw: dict = {"path_globs": path_globs, "tags": tags, "keywords": keywords}
    if min_severity:
        raw["min_severity"] = min_severity
    return normalize_routing(raw)


def merge_pack_config(existing: dict, *, pack_path: str, match: dict) -> dict:
    """Return a new config with `security_ac_pack` merged into
    `gate.blind_tdd`, preserving every other key (same contract as
    init.merge_config)."""
    out = dict(existing)
    gate = dict(out.get("gate") or {})
    btd = dict(gate.get("blind_tdd") or {})
    btd["security_ac_pack"] = {
        "enabled": True,
        "pack_path": pack_path,
        "match": match,
    }
    gate["blind_tdd"] = btd
    out["gate"] = gate
    return out


def _load_draft(path: str) -> tuple[dict | None, str]:
    p = Path(path)
    if not p.exists():
        return None, f"draft pack not found at {p}"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return None, f"draft pack at {p} is unreadable: {e}"
    if not isinstance(data, dict):
        return None, f"draft pack at {p} must be a JSON object"
    return data, ""


def _print_findings(errors: list[str], warnings: list[str]) -> None:
    for e in errors:
        print(f"  ERROR    {e}")
    for w in warnings:
        print(f"  warning  {w}")
    if not errors and not warnings:
        print("  clean — no findings")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m blind_tdd.pack_wizard",
        description="Lint and install a tailored security AC pack.",
    )
    action = ap.add_mutually_exclusive_group(required=True)
    action.add_argument("--lint", metavar="DRAFT.json",
                        help="lint a draft pack; exit 1 on errors (warnings pass)")
    action.add_argument("--install", metavar="DRAFT.json",
                        help="lint, then install the pack and enable it in config")
    ap.add_argument("--pack-path", default=DEFAULT_PACK_PATH,
                    help=f"where to install the pack (default: {DEFAULT_PACK_PATH})")
    ap.add_argument("--config", default=DEFAULT_CONFIG_PATH,
                    help=f"config file to merge into (default: {DEFAULT_CONFIG_PATH})")
    ap.add_argument("--match-glob", action="append", default=[], metavar="GLOB",
                    help="apply the pack to tasks touching this path glob (repeatable)")
    ap.add_argument("--match-tag", action="append", default=[], metavar="TAG",
                    help="apply the pack to tasks carrying this tag (repeatable)")
    ap.add_argument("--match-keyword", action="append", default=[], metavar="WORD",
                    help="apply the pack to tasks whose spec contains this keyword")
    ap.add_argument("--match-min-severity", choices=list(SEVERITY_RANK), default=None,
                    help="apply the pack to tasks at or above this severity")
    ap.add_argument("--print", dest="print_only", action="store_true",
                    help="print the resulting config, don't write anything")
    ap.add_argument("--yes", action="store_true",
                    help="skip the write confirmation prompt")
    ns = ap.parse_args(argv)

    draft_path = ns.lint or ns.install
    draft, err = _load_draft(draft_path)
    if draft is None:
        print(f"ERROR: {err}", file=sys.stderr)
        return 1

    errors, warnings = lint_pack_draft(draft)
    print(f"Lint of {draft_path}:")
    _print_findings(errors, warnings)
    if errors:
        print("\nDraft has errors — fix them and re-lint.", file=sys.stderr)
        return 1

    if ns.lint:
        return 0

    # ---- install ----
    match = build_match(
        path_globs=ns.match_glob,
        tags=ns.match_tag,
        keywords=ns.match_keyword,
        min_severity=ns.match_min_severity,
    )
    existing = load_config(ns.config)
    merged = merge_pack_config(existing, pack_path=ns.pack_path, match=match)

    print("")
    print("Resulting security_ac_pack config:")
    print(json.dumps(merged["gate"]["blind_tdd"]["security_ac_pack"], indent=2))
    print(f"Pack file: {ns.pack_path} ({len(draft.get('criteria', []))} criteria, "
          f"version {draft.get('version')})")

    if ns.print_only:
        print("")
        print(json.dumps(merged, indent=2, ensure_ascii=False))
        return 0

    if not ns.yes:
        confirm = input(
            f"\nWrite {ns.pack_path} and update {ns.config}? (y/n) [y]: "
        ).strip().lower()
        if confirm.startswith("n"):
            print("Aborted; nothing written.")
            return 1

    Path(ns.pack_path).write_text(
        json.dumps(draft, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    Path(ns.config).write_text(
        json.dumps(merged, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"\nWrote {ns.pack_path} and updated {ns.config}.")
    print("Commit the pack file — it is spec content, reviewed like any spec "
          "change. The gate fingerprints it at red and fails green if it "
          "changes mid-task.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
