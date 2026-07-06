"""Security AC pack — operator-authored security criteria injected at red.

The gate's trust boundary is the spec: a task whose acceptance criteria never
mention input validation or secret handling earns a confident pass without
them. The pack closes that for the security domain. It is a versioned,
operator-authored catalog of security acceptance criteria (input rejection,
no hardcoded secrets, sanitized error surfaces, ...) that the gate appends to
a matching task's `acceptance_criteria` before the blind writer ever sees the
task — so the writer derives sealed, independent security tests exactly as it
does for the task's own criteria.

Trust placement mirrors routing: the pack file and its match predicates are
set by a human at setup time, never by the implementing agent. Two properties
keep it honest:

- **Determinism.** Injection is a pure function of (task, pack): pack criteria
  get IDs continuing the task's `AC-N` numbering, in pack order. Red and green
  both apply the pack and land on the same augmented task, so the green
  coverage check holds the implementation to the same sealed criteria the
  writer derived tests from.
- **Fingerprinting.** The pack's SHA-256 fingerprint is recorded in the
  red-state record (covered by the optional seal HMAC). A pack that changes
  between red and green — swapped, weakened, or newly enabled — fails the
  green phase with a specific reason instead of a confusing coverage error.

A pack criterion that genuinely doesn't apply to a task (no variable-size
input, no injection sink) escapes through the existing triage mechanism: the
blind writer reports it `needs_human` with a reason, exactly as for any other
untestable criterion. The pack does not need to fit every task to be safe on
every task.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from .routing import normalize_routing


DEFAULT_PACK_RELPATH = Path("templates") / "blind_tdd" / "security_ac_pack.json"

_AC_NUM_RE = re.compile(r"^AC-(\d+)$", re.IGNORECASE)


def normalize_pack_config(raw: object) -> dict:
    """Fully-defaulted `gate.blind_tdd.security_ac_pack` config.

    Shape:
        "security_ac_pack": {
          "enabled": false,
          "pack_path": null,          # null → the packaged default pack
          "match": { ... }            # routing-style predicates; empty → every gated task
        }
    """
    raw = raw if isinstance(raw, dict) else {}
    pack_path = raw.get("pack_path")
    return {
        "enabled": bool(raw.get("enabled", False)),
        "pack_path": str(pack_path) if isinstance(pack_path, str) and pack_path.strip() else None,
        "match": normalize_routing(raw.get("match")),
    }


def resolve_pack_path(pack_path: str | None, themis_home: str | None = None) -> Path:
    """Locate the pack file: explicit config path, else the packaged default
    (resolved like the prompt templates — configured home, env home, or
    relative to this package)."""
    if pack_path:
        return Path(pack_path)
    home = themis_home or os.environ.get("THEMIS_HOME") or os.environ.get("RALPH_HOME")
    if home:
        return Path(home) / DEFAULT_PACK_RELPATH
    return Path(__file__).resolve().parents[1] / DEFAULT_PACK_RELPATH


def validate_pack(pack: object) -> list[str]:
    """Structural errors in a loaded pack. Empty list means usable."""
    errors: list[str] = []
    if not isinstance(pack, dict):
        return [f"pack must be a JSON object, got {type(pack).__name__}"]
    if "version" not in pack:
        errors.append("pack.version is required")
    criteria = pack.get("criteria")
    if not isinstance(criteria, list) or not criteria:
        errors.append("pack.criteria must be a non-empty list")
        return errors
    for i, crit in enumerate(criteria):
        prefix = f"pack.criteria[{i}]"
        if not isinstance(crit, dict):
            errors.append(f"{prefix} must be an object")
            continue
        for key in ("given", "when", "then"):
            val = crit.get(key)
            if not isinstance(val, str) or not val.strip():
                errors.append(f"{prefix}.{key} must be a non-empty string")
    return errors


def load_pack(path: Path | str) -> tuple[dict | None, str]:
    """Load and validate a pack file. Returns (pack, "") or (None, error)."""
    p = Path(path)
    if not p.exists():
        return None, f"security AC pack not found at {p}"
    try:
        pack = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return None, f"security AC pack at {p} is unreadable: {e}"
    errors = validate_pack(pack)
    if errors:
        return None, f"security AC pack at {p} is invalid: " + "; ".join(errors)
    return pack, ""


def pack_fingerprint(pack: dict) -> str:
    """Canonical SHA-256 over the pack content — same pack, same fingerprint."""
    payload = json.dumps(
        pack, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def apply_pack(task: dict, pack: dict) -> tuple[dict, list[str]]:
    """Return (augmented copy of `task`, injected AC ids).

    Pack criteria are appended to `acceptance_criteria` with IDs continuing
    the task's `AC-N` numbering — the coverage parser only recognizes that
    shape — in pack order, so the result is deterministic for a given
    (task, pack). The input task is not mutated.

    `pack` must already be validated (`load_pack` does this): criterion
    fields are indexed directly, and an unvalidated pack missing given/
    when/then raises KeyError here — loud by design, not defended.
    """
    criteria = [dict(c) if isinstance(c, dict) else c
                for c in (task.get("acceptance_criteria") or [])]

    max_n = 0
    for c in criteria:
        if isinstance(c, dict):
            m = _AC_NUM_RE.match(str(c.get("id", "")))
            if m:
                max_n = max(max_n, int(m.group(1)))

    version = pack.get("version")
    injected: list[str] = []
    n = max_n
    for entry in pack.get("criteria", []):
        n += 1
        cid = f"AC-{n}"
        tag = f"[themis-security-pack v{version}: {entry.get('category', 'general')}]"
        notes = str(entry.get("notes", "")).strip()
        ac = {
            "id": cid,
            "given": entry["given"],
            "when": entry["when"],
            "then": entry["then"],
            "notes": f"{notes} {tag}".strip(),
        }
        if entry.get("preclassified"):
            ac["preclassified"] = entry["preclassified"]
        criteria.append(ac)
        injected.append(cid)

    augmented = dict(task)
    augmented["acceptance_criteria"] = criteria
    return augmented, injected
