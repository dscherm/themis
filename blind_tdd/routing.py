"""Task routing for the blind-TDD gate — *which* tasks the gate engages on.

The blind gate is expensive (several agent passes where an in-loop verifier
makes one). Most tasks do not need it. Routing lets a project reserve the gate
for high-stakes work and let an ordinary assigned verifier cover the rest,
without the implementing agent getting a vote.

The policy lives in `gate.blind_tdd.routing` and is set once, by a human, at
setup time (`python -m blind_tdd.init`, or the `/blind-tdd:setup` plugin
command). That placement is deliberate: the trigger must be a rule the worker
does not control. If the implementing agent could choose whether the gate
fires, it would skip it exactly when the pressure to cheat is highest — the one
moment the gate exists for.

## Trust note (read before relying on selective routing)

Routing only redirects effort *honestly* if the task metadata it reads comes
from a trusted source — the spec, or the loop's task-selection — never the
implementing agent. A worker that authors its own task `tags`/`severity` could
label itself "low-stakes" to dodge the gate. Of the four predicates,
`path_globs` (matched against operator-authored task `files` / `public_surface`)
is the most evasion-resistant; `tags`, `min_severity`, and `keywords` are only
as trustworthy as whoever writes them. Routing decides *coverage*, not gate
integrity — a task that IS gated still gets the full sealed, blind treatment.

## Policy shape

    "routing": {
      "mode": "all" | "selective",   # default: "selective" iff any predicate set, else "all"
      "path_globs": ["billing/**", "auth/**", "*.sql"],
      "tags": ["high-stakes", "security"],
      "min_severity": "high",        # low < medium < high < critical
      "keywords": ["payment", "password", "token"]
    }

`mode: "all"` gates every resolved task — the pre-routing default, backward
compatible with a config that has no `routing` block. `mode: "selective"` gates
a task iff it matches at least one active predicate (OR across predicate
groups); a non-matching task is skipped with a clear reason.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


# Severity ladder. Tasks at or above the configured `min_severity` match.
SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}


@dataclass
class RoutingDecision:
    """The outcome of evaluating a task against the routing policy."""

    gate: bool
    reason: str
    matched: list[str] = field(default_factory=list)


def normalize_routing(raw: object) -> dict:
    """Return a fully-defaulted routing policy from a raw config value.

    Accepts None (no routing block) or a dict. Unknown keys are ignored.
    `mode` defaults to "selective" when any predicate is configured, else
    "all" (so a config with no routing block keeps the legacy gate-everything
    behavior).
    """
    raw = raw if isinstance(raw, dict) else {}

    path_globs = [str(g) for g in (raw.get("path_globs") or []) if str(g).strip()]
    tags = [str(t).strip() for t in (raw.get("tags") or []) if str(t).strip()]
    keywords = [str(k).strip() for k in (raw.get("keywords") or []) if str(k).strip()]

    min_sev_raw = raw.get("min_severity")
    min_severity = None
    if isinstance(min_sev_raw, str) and min_sev_raw.strip().lower() in SEVERITY_RANK:
        min_severity = min_sev_raw.strip().lower()

    has_predicate = bool(path_globs or tags or keywords or min_severity)

    mode = str(raw.get("mode", "")).strip().lower()
    if mode not in ("all", "selective"):
        mode = "selective" if has_predicate else "all"

    return {
        "mode": mode,
        "path_globs": path_globs,
        "tags": tags,
        "min_severity": min_severity,
        "keywords": keywords,
    }


def routing_warnings(routing: dict) -> list[str]:
    """Config-time sanity warnings (used by the setup CLI), never fatal."""
    warnings: list[str] = []
    has_predicate = bool(
        routing.get("path_globs")
        or routing.get("tags")
        or routing.get("keywords")
        or routing.get("min_severity")
    )
    if routing.get("mode") == "selective" and not has_predicate:
        warnings.append(
            "routing.mode is 'selective' but no predicates are configured — "
            "this gates NO tasks. Add a path_glob/tag/severity/keyword, or set "
            "mode to 'all'."
        )
    return warnings


def _glob_to_regex(pattern: str) -> re.Pattern:
    """Translate a path glob to a regex.

    Supports `**` (any path segments, including none, when written `**/`),
    `*` (any run of non-slash chars), and `?` (one non-slash char). Matching
    is case-sensitive and anchored to the whole path.
    """
    pat = pattern.replace("\\", "/")
    out: list[str] = []
    i, n = 0, len(pat)
    while i < n:
        if pat[i:i + 3] == "**/":
            out.append("(?:.*/)?")
            i += 3
        elif pat[i:i + 2] == "**":
            out.append(".*")
            i += 2
        elif pat[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pat[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pat[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def _module_to_path(module: str) -> str:
    """Best-effort path form of a `public_surface.module` value.

    Dotted Python modules ("src.engine.core") become slash paths; values that
    already look like paths ("./components/Player") are kept (leading ./ stripped).
    """
    m = module.strip()
    if m.startswith("./"):
        m = m[2:]
    if "/" in m:
        return m
    return m.replace(".", "/")


def task_paths(task: dict) -> list[str]:
    """Candidate filesystem paths a task touches, for glob matching.

    Drawn from operator-authored spec fields only — `files` (explicit list)
    and `public_surface.module` — never from anything the implementing agent
    writes. Paths are normalized to forward slashes.
    """
    paths: list[str] = []
    files = task.get("files")
    if isinstance(files, list):
        paths.extend(str(f).replace("\\", "/").lstrip("./") for f in files if str(f).strip())

    surface = task.get("public_surface")
    if isinstance(surface, dict):
        module = surface.get("module")
        if isinstance(module, str) and module.strip():
            paths.append(_module_to_path(module))
    return paths


def task_text(task: dict) -> str:
    """Lowercased text blob of a task, for keyword matching."""
    parts: list[str] = []
    for key in ("id", "title", "description", "name"):
        val = task.get(key)
        if isinstance(val, str):
            parts.append(val)
    criteria = task.get("acceptance_criteria")
    if isinstance(criteria, list):
        for crit in criteria:
            if isinstance(crit, dict):
                for key in ("given", "when", "then", "notes"):
                    val = crit.get(key)
                    if isinstance(val, str):
                        parts.append(val)
    return " ".join(parts).lower()


def task_severity_rank(task: dict) -> int | None:
    """The task's severity rank, or None if absent/unrecognized."""
    sev = task.get("severity")
    if isinstance(sev, str) and sev.strip().lower() in SEVERITY_RANK:
        return SEVERITY_RANK[sev.strip().lower()]
    return None


def evaluate_routing(task: dict, routing: dict) -> RoutingDecision:
    """Decide whether the blind gate should engage for `task`.

    `routing` must be a normalized policy (see `normalize_routing`). In "all"
    mode every task gates. In "selective" mode a task gates iff it matches at
    least one active predicate.
    """
    if not isinstance(task, dict):
        return RoutingDecision(gate=True, reason="non-dict task — gating by default")

    if routing.get("mode") == "all":
        return RoutingDecision(gate=True, reason="routing mode 'all' — every task is gated")

    matched: list[str] = []

    # Path globs (most evasion-resistant — operator-authored paths).
    globs = routing.get("path_globs") or []
    if globs:
        paths = task_paths(task)
        for glob in globs:
            rx = _glob_to_regex(glob)
            for p in paths:
                if rx.match(p):
                    matched.append(f"path:{glob} ~ {p}")
                    break

    # Tags.
    policy_tags = {t.lower() for t in (routing.get("tags") or [])}
    if policy_tags:
        task_tags = task.get("tags")
        if isinstance(task_tags, list):
            for t in task_tags:
                if isinstance(t, str) and t.strip().lower() in policy_tags:
                    matched.append(f"tag:{t.strip()}")

    # Severity threshold.
    min_sev = routing.get("min_severity")
    if min_sev:
        rank = task_severity_rank(task)
        threshold = SEVERITY_RANK[min_sev]
        if rank is not None and rank >= threshold:
            matched.append(f"severity:{task.get('severity')}>={min_sev}")

    # Keywords (substring, case-insensitive, over the task text blob).
    keywords = routing.get("keywords") or []
    if keywords:
        blob = task_text(task)
        for kw in keywords:
            if kw.lower() in blob:
                matched.append(f"keyword:{kw}")

    if matched:
        return RoutingDecision(
            gate=True,
            reason="selective routing matched " + "; ".join(matched),
            matched=matched,
        )

    has_predicate = bool(globs or policy_tags or min_sev or keywords)
    if not has_predicate:
        return RoutingDecision(
            gate=False,
            reason="selective routing has no predicates configured — gates nothing",
        )
    return RoutingDecision(
        gate=False,
        reason="selective routing matched no predicate for this task",
    )
