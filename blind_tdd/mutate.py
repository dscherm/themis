"""Mutation testing — the test-*strength* layer the blind gate deliberately omits.

The blind gate proves a test's **independence** (written without sight of the
implementation) and **integrity** (not edited afterward). It does not prove the
test is **strong** — that it would actually fail a wrong implementation. A test
can be independent, untampered, AC-tagged, and still too weak to catch a bug (it
might assert a return *type* but never the *value*).

Measuring strength means perturbing the implementation and checking that some test
fails — mutation testing — which needs something that *can* read the code, the
opposite of the blind writer. So it is necessarily a separate, advisory pass, not
part of the gate's guarantee. A mutant that **survives** (all tests still pass
after the change) marks a weak spot in the suite.

Design notes:
- **Stdlib only.** AST in, AST out, deterministic operator set and walk order, so
  the same source always yields the same mutants in the same order.
- **Every mutant runs in a wall-clock-bounded subprocess.** A mutated conditional
  can spin (e.g. flipping `i += 1` to `i -= 1` removes a loop's exit). A spin is
  recorded as a `timeout`, never allowed to hang the run. (The implementation under
  mutation is, by definition, not trusted to terminate.)

Usage:
    from blind_tdd.mutate import run_mutation
    result = run_mutation("src/calc.py", "python -m pytest tests/", timeout=20)
    print(result.summary())

    # or as a CLI
    python -m blind_tdd.mutate src/calc.py --test-cmd "python -m pytest tests/"
"""

from __future__ import annotations

import argparse
import ast
import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# --- operators: each maps a node op to its mutated counterpart -----------------
_CMP_SWAP = {
    ast.Lt: ast.GtE, ast.GtE: ast.Lt,
    ast.Gt: ast.LtE, ast.LtE: ast.Gt,
    ast.Eq: ast.NotEq, ast.NotEq: ast.Eq,
}
_ARITH_SWAP = {ast.Add: ast.Sub, ast.Sub: ast.Add}
_BOOL_SWAP = {ast.And: ast.Or, ast.Or: ast.And}

_OP_SYMBOL = {
    ast.Lt: "<", ast.GtE: ">=", ast.Gt: ">", ast.LtE: "<=",
    ast.Eq: "==", ast.NotEq: "!=", ast.Add: "+", ast.Sub: "-",
    ast.And: "and", ast.Or: "or",
}


@dataclass
class Mutant:
    """One single-change variant of the source."""
    index: int
    operator: str
    lineno: int
    before: str
    after: str
    source: str


def _mutable_sites(tree: ast.AST) -> list[tuple]:
    """Every mutable site in deterministic walk order: (node, kind, sub).

    `kind` selects the operator family; `sub` is the comparator index for
    Compare nodes (which can chain, e.g. `a < b < c`) and None otherwise.
    """
    sites: list[tuple] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for i, op in enumerate(node.ops):
                if type(op) in _CMP_SWAP:
                    sites.append((node, "cmp", i))
        elif isinstance(node, ast.BinOp) and type(node.op) in _ARITH_SWAP:
            sites.append((node, "arith", None))
        elif isinstance(node, ast.AugAssign) and type(node.op) in _ARITH_SWAP:
            sites.append((node, "augarith", None))
        elif isinstance(node, ast.BoolOp) and type(node.op) in _BOOL_SWAP:
            sites.append((node, "bool", None))
        elif isinstance(node, ast.Constant):
            if isinstance(node.value, bool):
                sites.append((node, "constbool", None))
            elif isinstance(node.value, int):  # bool already handled above
                sites.append((node, "constint", None))
    return sites


def _apply(site: tuple) -> tuple[str, str, str, int]:
    """Mutate one site in place. Returns (operator, before, after, lineno)."""
    node, kind, sub = site
    if kind == "cmp":
        old = type(node.ops[sub])
        new = _CMP_SWAP[old]
        node.ops[sub] = new()
        return "comparison", _OP_SYMBOL[old], _OP_SYMBOL[new], node.lineno
    if kind in ("arith", "augarith"):
        old = type(node.op)
        new = _ARITH_SWAP[old]
        node.op = new()
        label = "aug-assign" if kind == "augarith" else "arithmetic"
        return label, _OP_SYMBOL[old], _OP_SYMBOL[new], node.lineno
    if kind == "bool":
        old = type(node.op)
        new = _BOOL_SWAP[old]
        node.op = new()
        return "boolean", _OP_SYMBOL[old], _OP_SYMBOL[new], node.lineno
    if kind == "constbool":
        before = str(node.value)
        node.value = not node.value
        return "bool-constant", before, str(node.value), node.lineno
    if kind == "constint":
        before = str(node.value)
        node.value = node.value + 1
        return "int-constant", before, str(node.value), node.lineno
    raise ValueError(f"unknown mutation kind: {kind}")  # pragma: no cover


def discover_mutants(source: str) -> list[Mutant]:
    """Return every single-change mutant of `source`, deterministically ordered.

    Re-parses for each mutant so each carries exactly one change; the k-th mutant
    alters the k-th site in walk order.
    """
    n = len(_mutable_sites(ast.parse(source)))
    mutants: list[Mutant] = []
    for k in range(n):
        tree = ast.parse(source)
        site = _mutable_sites(tree)[k]
        operator, before, after, lineno = _apply(site)
        ast.fix_missing_locations(tree)
        mutants.append(Mutant(
            index=k, operator=operator, lineno=lineno,
            before=before, after=after, source=ast.unparse(tree),
        ))
    return mutants


@dataclass
class MutationResult:
    total: int
    killed: int
    survived: int
    timed_out: int
    errored: int
    survivors: list[dict] = field(default_factory=list)

    @property
    def score(self) -> float:
        """Fraction of mutants not surviving (1.0 = every mutant caught).

        Vacuously 1.0 when there are no mutable sites. Timeouts and errors count
        as not-surviving (the change was detectable, even if as a hang/crash);
        only a clean pass is a survivor.
        """
        return 1.0 if self.total == 0 else (self.total - self.survived) / self.total

    def summary(self) -> dict:
        return {
            "total": self.total,
            "killed": self.killed,
            "survived": self.survived,
            "timed_out": self.timed_out,
            "errored": self.errored,
            "score": round(self.score, 4),
            "survivors": self.survivors,
        }


def run_mutation(
    src_path: str | Path,
    test_command: str | list[str],
    *,
    timeout: float = 15.0,
    cwd: str | Path | None = None,
) -> MutationResult:
    """Mutate `src_path` one change at a time; run `test_command` against each.

    The source file is restored from its original text in a `finally`, so an
    interrupted run never leaves a mutated file behind. A non-zero exit means some
    test failed (mutant killed); exit 0 means every test still passed (survivor —
    a weak spot); a timeout is recorded separately and never hangs the caller.
    """
    src_path = Path(src_path)
    original = src_path.read_text(encoding="utf-8")
    mutants = discover_mutants(original)
    cmd = shlex.split(test_command) if isinstance(test_command, str) else list(test_command)

    # Each mutant overwrites the same file in quick succession. Python invalidates
    # cached bytecode by (mtime, size), so a later mutant of the same byte length
    # written within the same mtime tick can silently run against an EARLIER
    # mutant's .pyc — corrupting every result. Forbid bytecode writes in the child
    # and clear any pre-existing cache so each run recompiles from source.
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    shutil.rmtree(src_path.parent / "__pycache__", ignore_errors=True)

    killed = survived = timed_out = errored = 0
    survivors: list[dict] = []
    try:
        for m in mutants:
            src_path.write_text(m.source, encoding="utf-8")
            try:
                proc = subprocess.run(
                    cmd, cwd=str(cwd) if cwd else None, env=env,
                    capture_output=True, timeout=timeout,
                )
            except subprocess.TimeoutExpired:
                timed_out += 1
                continue
            except OSError:
                errored += 1
                continue
            if proc.returncode == 0:
                survived += 1
                survivors.append({
                    "operator": m.operator, "lineno": m.lineno,
                    "before": m.before, "after": m.after,
                })
            else:
                killed += 1
    finally:
        src_path.write_text(original, encoding="utf-8")

    return MutationResult(
        total=len(mutants), killed=killed, survived=survived,
        timed_out=timed_out, errored=errored, survivors=survivors,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="blind_tdd.mutate",
        description="Advisory mutation pass: surface weak (mutant-surviving) tests.",
    )
    ap.add_argument("src", help="source file to mutate")
    ap.add_argument("--test-cmd", required=True,
                    help="command that runs the tests (non-zero exit = a test failed)")
    ap.add_argument("--timeout", type=float, default=15.0,
                    help="per-mutant wall-clock bound in seconds (default: 15)")
    ap.add_argument("--cwd", default=None, help="working dir for the test command")
    ns = ap.parse_args(argv)

    result = run_mutation(ns.src, ns.test_cmd, timeout=ns.timeout, cwd=ns.cwd)
    s = result.summary()
    print(f"mutants: {s['total']}  killed: {s['killed']}  survived: {s['survived']}  "
          f"timed_out: {s['timed_out']}  errored: {s['errored']}  score: {s['score']}")
    for sv in s["survivors"]:
        print(f"  SURVIVOR line {sv['lineno']}: {sv['operator']} "
              f"{sv['before']!r} -> {sv['after']!r} (no test failed)")
    # Advisory only: a surviving mutant is a warning, not a hard failure.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
