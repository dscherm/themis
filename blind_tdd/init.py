"""Setup wizard for the blind-TDD gate — choose which tasks it engages on.

Run interactively:

    python -m blind_tdd.init

or non-interactively (scriptable / CI):

    python -m blind_tdd.init --enable --path-glob 'billing/**' --tag security \
        --min-severity high --keyword password --yes

Either way it writes a routing policy into `gate.blind_tdd.routing` in your
`themis.config.json` (merging, never clobbering other keys). The policy is what
the gate consults per task — see `blind_tdd/routing.py`. Set this once, as a
human: the trigger must be a rule the implementing agent does not control.

The same interview is also driven by the `/blind-tdd:setup` plugin command,
which calls this module's flag form.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable

from .routing import SEVERITY_RANK, normalize_routing, routing_warnings

DEFAULT_CONFIG_PATH = "themis.config.json"


# --------------------------------------------------------------------------- #
# Pure config helpers (no I/O) — kept separate so they're directly testable.
# --------------------------------------------------------------------------- #

def load_config(path: str | Path) -> dict:
    """Read an existing config dict, or return {} if absent/unreadable."""
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def merge_config(existing: dict, *, enabled: bool | None, routing: dict) -> dict:
    """Return a new config with the routing policy (and optional enabled flag)
    merged into `gate.blind_tdd`, preserving every other key.
    """
    out = dict(existing)
    gate = dict(out.get("gate") or {})
    btd = dict(gate.get("blind_tdd") or {})
    btd["routing"] = routing
    if enabled is not None:
        btd["enabled"] = enabled
    gate["blind_tdd"] = btd
    out["gate"] = gate
    return out


def build_routing(
    *,
    mode: str | None,
    path_globs: list[str],
    tags: list[str],
    min_severity: str | None,
    keywords: list[str],
) -> dict:
    """Normalize raw routing inputs into a stored policy dict."""
    raw: dict = {
        "path_globs": path_globs,
        "tags": tags,
        "keywords": keywords,
    }
    if min_severity:
        raw["min_severity"] = min_severity
    if mode:
        raw["mode"] = mode
    return normalize_routing(raw)


# --------------------------------------------------------------------------- #
# Interactive interview
# --------------------------------------------------------------------------- #

def _split_csv(raw: str) -> list[str]:
    return [item.strip() for item in raw.replace("\n", ",").split(",") if item.strip()]


def run_interview(
    ask: Callable[[str], str] = input,
    out: Callable[[str], None] = print,
) -> tuple[bool, dict]:
    """Conduct the setup interview. Returns (enabled, routing_policy).

    `ask` / `out` are injectable so this is testable without a real terminal.
    """
    out("")
    out("Themis blind-TDD gate — task routing setup")
    out("=" * 44)
    out("The blind gate is expensive (several agent passes). Most tasks don't")
    out("need it. Choose which tasks it should engage on. This is set by you,")
    out("once — the implementing agent never gets to choose.")
    out("")

    choice = ask(
        "Gate [a]ll tasks, or [s]elective by type? (a/s) [s]: "
    ).strip().lower()
    selective = not choice.startswith("a")

    path_globs: list[str] = []
    tags: list[str] = []
    keywords: list[str] = []
    min_severity: str | None = None

    if selective:
        out("")
        out("Leave any prompt blank to skip that predicate. A task is gated if it")
        out("matches ANY of them.")
        out("")
        out("Path globs are the most evasion-resistant (matched against the task's")
        out("declared files/module, which the worker doesn't author).")
        path_globs = _split_csv(
            ask("  Path globs (comma-separated, e.g. billing/**, auth/**): ")
        )
        tags = _split_csv(
            ask("  Task tags to gate (comma-separated, e.g. security, high-stakes): ")
        )
        sev = ask(
            f"  Minimum severity to gate ({'/'.join(SEVERITY_RANK)}) [none]: "
        ).strip().lower()
        if sev in SEVERITY_RANK:
            min_severity = sev
        elif sev:
            out(f"  (ignoring unrecognized severity {sev!r})")
        keywords = _split_csv(
            ask("  Spec keywords to gate (comma-separated, e.g. payment, password): ")
        )

    mode = "selective" if selective else "all"
    routing = build_routing(
        mode=mode,
        path_globs=path_globs,
        tags=tags,
        min_severity=min_severity,
        keywords=keywords,
    )

    enable_raw = ask("\nEnable the gate now? (y/n) [y]: ").strip().lower()
    enabled = not enable_raw.startswith("n")

    return enabled, routing


def _summarize(out: Callable[[str], None], enabled: bool, routing: dict) -> None:
    out("")
    out("Resulting policy:")
    out(f"  enabled: {enabled}")
    out(f"  routing.mode: {routing['mode']}")
    if routing["mode"] == "selective":
        out(f"    path_globs:   {routing['path_globs'] or '(none)'}")
        out(f"    tags:         {routing['tags'] or '(none)'}")
        out(f"    min_severity: {routing['min_severity'] or '(none)'}")
        out(f"    keywords:     {routing['keywords'] or '(none)'}")
    for w in routing_warnings(routing):
        out(f"  ⚠ {w}")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m blind_tdd.init",
        description="Configure which tasks the blind-TDD gate engages on.",
    )
    ap.add_argument("--config", default=DEFAULT_CONFIG_PATH,
                    help=f"config file to write (default: {DEFAULT_CONFIG_PATH})")
    ap.add_argument("--mode", choices=["all", "selective"], default=None,
                    help="gate all tasks, or selectively (default: inferred)")
    ap.add_argument("--path-glob", action="append", default=[], metavar="GLOB",
                    help="gate tasks touching this path glob (repeatable)")
    ap.add_argument("--tag", action="append", default=[], metavar="TAG",
                    help="gate tasks carrying this tag (repeatable)")
    ap.add_argument("--min-severity", choices=list(SEVERITY_RANK), default=None,
                    help="gate tasks at or above this severity")
    ap.add_argument("--keyword", action="append", default=[], metavar="WORD",
                    help="gate tasks whose spec text contains this keyword (repeatable)")
    enable = ap.add_mutually_exclusive_group()
    enable.add_argument("--enable", dest="enabled", action="store_true", default=None,
                        help="set gate.blind_tdd.enabled = true")
    enable.add_argument("--disable", dest="enabled", action="store_false",
                        help="set gate.blind_tdd.enabled = false")
    ap.add_argument("--print", dest="print_only", action="store_true",
                    help="print the resulting config to stdout, don't write")
    ap.add_argument("--yes", action="store_true",
                    help="skip the write confirmation prompt")
    ns = ap.parse_args(argv)

    any_flag = bool(
        ns.path_glob or ns.tag or ns.keyword or ns.min_severity
        or ns.mode or ns.enabled is not None
    )

    if any_flag:
        # Non-interactive: build straight from flags.
        routing = build_routing(
            mode=ns.mode,
            path_globs=ns.path_glob,
            tags=ns.tag,
            min_severity=ns.min_severity,
            keywords=ns.keyword,
        )
        enabled = ns.enabled  # may be None (leave existing value as-is)
    else:
        # Interactive interview.
        enabled, routing = run_interview()

    existing = load_config(ns.config)
    merged = merge_config(existing, enabled=enabled, routing=routing)

    _summarize(print, bool(merged["gate"]["blind_tdd"].get("enabled")), routing)

    if ns.print_only:
        print("")
        print(json.dumps(merged, indent=2, ensure_ascii=False))
        return 0

    if not ns.yes:
        confirm = input(f"\nWrite to {ns.config}? (y/n) [y]: ").strip().lower()
        if confirm.startswith("n"):
            print("Aborted; nothing written.")
            return 1

    Path(ns.config).write_text(
        json.dumps(merged, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"\nWrote {ns.config}.")
    print("Next: mark a current task (THEMIS_TASK / .themis/current_task.json) "
          "and run the gate. See docs/adoption-guide.md.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
