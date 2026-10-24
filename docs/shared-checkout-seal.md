# The green seal in a shared checkout: which new test files are this task's

**Status:** shipped 2026-10-24. Code: `orchestrator._classify_seal_diff`,
`orchestrator._new_file_scope`, the seal check in `run_green_phase`,
`coverage.verify_coverage(exclude_files=...)`, and
`gate_integration._all_task_ids_in_plan` (feeds `orch.known_task_ids`). Tests:
`blind_tdd/test_shared_checkout_seal.py`, plus the existing
`blind_tdd/test_hash_integrity.py`.

## The failure this closes

ralph-universal, task OS-1, 2026-10-24, `enforcement: strict`:

1. The red phase sealed every test file under `tests/contracts/` and
   `tests/integration/` — 76 files — in `.themis/blind_tdd/red_state/OS-1.json`.
2. During the implementation window, other sessions merged their own tasks into
   the same checkout. Three test files that were not OS-1's appeared (two from
   other tasks' merges, one from a staged change).
3. The green check compared the whole tree for equality and reported `test file
   integrity compromised`, wrote a `test_hash_break` to the tamper ledger, and
   under strict enforcement blocked the commit. OS-1's own blind test was
   byte-identical to its sealed version.

The ledger record was false, and it feeds routing, so the false break would
also have pushed later, similar tasks towards escalation.

## The decision

**Sealing stays whole-tree. Comparison splits differences by whose they are.**

| What differs since the red seal | Verdict |
|---|---|
| A sealed file was modified | **break** (unchanged) |
| A sealed file was deleted | **break** (unchanged) |
| A new `conftest.py` | **break** |
| A new file whose name names this task (`_task_id_pattern`) | **break** |
| A new file with a `Covers:` tag for one of this task's AC ids | **break**, unless its name names a *different* task from plan.md and not this one |
| Any other new file | **out of scope**: reported, not sealed, excluded from coverage |

Out-of-scope files show up in the gate result's message and in
`details["out_of_scope_files"]` (path → reason) on both pass and fail, so a
human can still see them.

### Why the seal stays whole-tree

The brief asked that "any pre-existing sealed test must still be flagged if
modified or deleted". Sealing only the files the writer produced would drop
that: an implementer could weaken an older test that happens to exercise the
same code, and nothing would notice. So the red phase still hashes everything,
and a change to *any* sealed file is still a break, even when another task made
it. That case has a manual fix already (re-seal that one hash at a human's
direction, as was done three times for OS-1 — see `manual_reseals` in its red
state and [`post-ruling-reseal.md`](post-ruling-reseal.md)). Only *new* files
were the noise, so only new files get a new rule.

### Why a `Covers:` tag alone can't decide, and plan.md is consulted

AC ids are numbered per task: every task has an `AC-1`. Another task's blind
test file tags `AC-1` exactly as a sneaked test for this task would. With the
tag as the only signal, every other task's blind test that merges mid-window
would still break the seal, and those are the most common new files in a shared
checkout. So the gate passes every task id it finds in plan.md / fix_plan.md as
`known_task_ids`, and a tagged file whose *name* names another known task (and
does not name this one) counts as that task's. ralph-universal names its blind
files this way (`test_os1_observation_tags.py`, `test_bl1_blocker_enforcement.py`).

With no known ids (the orchestrator used on its own, or no plan file), a new
file tagging this task's criteria always breaks. The rule fails closed.

### Why out-of-scope files are excluded from coverage

This is what makes the attribution rule safe even though it can be forged.
Before this change, `verify_coverage` counted every `Covers:` tag under the test
dirs. If out-of-scope new files were still counted, then naming a sneaked file
`test_cov2_x.py` would both get it past the seal *and* let its trivially passing
test satisfy this task's `AC-1`. With the exclusion, an out-of-scope file can
never satisfy one of this task's criteria. Mislabelling a sneaked test buys the
implementer nothing: the task still needs a passing test from a sealed file.
`test_another_tasks_blind_file_is_out_of_scope_and_never_covers` pins this.
That test has this task's AC-1 test failing and another task's AC-1-tagged test
passing, and the green check reports `AC-1` as not passing.

## Trust boundary: what this does and does not defend

**Still caught:**
- editing, weakening or deleting any test that existed at the red seal, this
  task's or anyone's;
- adding a test that claims one of this task's criteria under a neutral name
  (the sneak attack in `test_hash_integrity.py`);
- adding any file named for this task;
- adding a `conftest.py` in a sealed directory.

**Not caught: these are the holes, stated plainly:**
- **Side effects from a new out-of-scope file.** A new `test_*.py` that does
  not tag this task's criteria is excluded from coverage but is still
  *collected* by the test runner. Code at module import time could monkeypatch
  something a sealed test depends on. `conftest.py` is treated as in scope for
  that reason, but an ordinary module can do the same at import time. This
  vector was already open before this change, through files outside the test
  dirs: a root `conftest.py`, `tests/conftest.py`, `sitecustomize`, or the
  implementation itself. The seal never covered those. A runner that collects
  only sealed files would close it, and is the follow-up if this matters.
- **Name attribution can be forged.** An implementer can name a file after
  another task. That buys nothing for coverage, as shown above, so the only
  remaining effect is the side-effect hole in the previous point.
- **A pre-existing test's tags still count across tasks.** `verify_coverage`
  counts `Covers: AC-1` from any *sealed* file, including other tasks' older
  tests, toward this task's `AC-1`. That was true before this change and is not
  changed by it. It is a coverage-attribution weakness, not a seal weakness.
- **Foreign edits to existing files still break the seal.** This is
  deliberate (see above). In a busy shared checkout it still produces breaks
  that a human resolves by re-sealing one hash.

## What did not change

- `run_red_phase`, `resume_red_phase`, `seal-red-state` and the writer
  handshake hash exactly as before. Red-state files are unchanged, and so is
  their HMAC.
- Post-ruling re-seal works as before: it replaces one sealed hash, and the
  comparison above runs against the result.
- `hash_break` and the `test_hash_break` tamper record are produced only for
  the "break" rows.
