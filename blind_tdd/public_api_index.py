"""One heading-aware matcher for `public_api.md`, shared by every tool.

Two tools ask the same question of a project's `public_api.md` — "does this
file DECLARE a surface for module X?" — and until TD195 each answered it with
its own regex:

  * `blind_tdd.preflight._check_public_api_file` used a raw substring test
    (`if module not in content`), which both under- and over-matched. It
    missed `## server.unit_store` when the task said `server/unit_store.py`
    (a FORM mismatch — 13 of in-the-loop-learning's best-documented modules
    were warned about), and it would have accepted a module named only in a
    passing prose sentence (any mention counts).
  * `tools/spec_audit.py` (ralph-universal) anchored its dotted-heading
    regex with `\s*$`, so a heading carrying a trailing title —
    `## core.schemas — the unit taxonomy (TD45)`, which is what real
    headings look like — did not register as a module heading at all.

This module is the single answer, imported by both. `spec_audit` already
imports `blind_tdd` unconditionally (`from blind_tdd.coverage import
verify_coverage`), so sharing costs it no new dependency.

## What counts as a declaration

A module is DECLARED when a level-1 or level-2 ATX heading names it, in
either spelling:

    ## `server/unit_store.py`                       path, backtick-quoted
    ## server.unit_store                            dotted, bare
    ## core.schemas — the unit taxonomy (TD45)      dotted + trailing title
    ## `server.backup` — the copy of the record     backticked + title

A level-3-or-deeper heading is a SUB-section of the module above it and
declares nothing of its own: `### \`server.unit_store\` — the orphan store`
(in-the-loop-learning's public_api.md:865) documents part of the
`## server.unit_store` section it sits under, and a heading that names two
modules in passing (`### \`core.schemas\` / \`server.unit_store\` — a
milestone may REFERENCE a unit`, :1070) must not be read as declaring either.

A mention in prose is NOT a declaration. That is the standing rule
`a-declared-surface-is-a-name-list-not-a-contract`: widening the match to
the dotted spelling fixes a FORM mismatch, it does not lower the bar to
"the name appears somewhere in the file".
"""

from __future__ import annotations

import re


# Levels that DECLARE a module. Level 1 is included because a single-module
# document titles itself `# src.foo`; level 3+ is a sub-section of the
# module above it (see the module docstring).
DECLARING_LEVELS = (1, 2)

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
# A heading may open with the module name in backticks; take that token.
_LEADING_BACKTICK_RE = re.compile(r"^`([^`]+)`")
# `path/to/mod.py` — a file path ending in .py.
_PATH_TOKEN_RE = re.compile(r"^[\w./\-]+\.py$")
# `server.unit_store`, `core.schemas.Unit.summary` — two or more dot-joined
# identifiers. One bare identifier ("Notes") is not a module name, which is
# what keeps an ordinary prose heading out.
_DOTTED_TOKEN_RE = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+$")


def normalise_module_key(name: str) -> str:
    """Canonicalize a module spelling — dotted (``server.unit_store``) or
    path (``server/unit_store.py``) — to one dotted, extension-free key, so
    a heading and a `public_surface.module` written in either spelling
    resolve to the same thing."""
    n = name.strip().replace("\\", "/")
    if n.endswith(".py"):
        n = n[:-3]
    return n.replace("/", ".")


def heading_module_token(line: str) -> tuple[int, str] | None:
    """Return ``(heading_level, raw_module_token)`` for a markdown heading
    that names a module, or ``None``.

    The token is returned VERBATIM (``server/unit_store.py``, not
    ``server.unit_store``) because `spec_audit.parse_public_api` keys its
    sections by the spelling the document used; normalisation belongs at
    the lookup, via `normalise_module_key`.
    """
    m = _HEADING_RE.match(line)
    if not m:
        return None
    level = len(m.group(1))
    text = m.group(2).strip()
    if not text:
        return None

    bt = _LEADING_BACKTICK_RE.match(text)
    if bt:
        token = bt.group(1).strip()
    else:
        # Stop at the module token: everything after the first whitespace is
        # a trailing title ("— the unit taxonomy (TD45)"), not part of the
        # name. This is the anchor `spec_audit`'s `\s*$` got wrong.
        token = text.split()[0]

    if _PATH_TOKEN_RE.match(token) or _DOTTED_TOKEN_RE.match(token):
        return level, token
    return None


def declared_modules(text: str) -> set[str]:
    """Every module key `text` declares, normalised, from declaring-level
    headings only."""
    out: set[str] = set()
    for line in text.splitlines():
        hit = heading_module_token(line)
        if hit is None:
            continue
        level, token = hit
        if level in DECLARING_LEVELS:
            out.add(normalise_module_key(token))
    return out


def declares_module(text: str, module: str) -> bool:
    """True if `text` has a heading declaring `module`'s surface.

    Matches the module's own heading, and a heading for a named part of it
    (`## core.schemas.Unit.summary` declares part of `core.schemas`'s
    surface — see in-the-loop-learning's public_api.md:316). The prefix rule
    applies only to a module key that is itself dotted, so a one-segment key
    like ``server`` cannot swallow every `server.*` heading in the file.
    """
    want = normalise_module_key(module)
    if not want:
        return False
    declared = declared_modules(text)
    if want in declared:
        return True
    if "." not in want:
        return False
    prefix = want + "."
    return any(d.startswith(prefix) for d in declared)
