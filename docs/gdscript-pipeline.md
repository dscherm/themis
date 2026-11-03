# Themis for GDScript

2026-11-03. Started from `godot-prompt-bridge/docs/themis-gd-heading-bug.md`.

## Decision: one gate with per-language parts, not a second pipeline

Most of Themis was already language-neutral and worked on Godot: the blindness seal, the red/green
hash seal, `Covers:` tag coverage, and running the configured test command. A copy for Godot would
have duplicated the seal and HMAC code, which is the trust boundary, so every future fix would have
had to land twice. The stages that read source code each hard-coded `.py`; they now ask
`blind_tdd/lang.py` instead, and GDScript gets its own module only where Python's `ast` cannot help
(quality scan, mutation, GUT measurement).

## Stage by stage

| Stage | Before | Now | Where |
|---|---|---|---|
| Blindness seal, red/green hash seal, `Covers:` tags | Worked | Unchanged | `roots.py`, `orchestrator.py`, `coverage.py` |
| `public_api.md` module headings | Every `.gd` module read as undocumented | `.gd` (and every source extension in `lang`) recognised | `public_api_index.py` |
| Harness spec audit (`ralph-universal/tools/spec_audit.py`) | Same false misses, through the shared matcher | Fixed by the line above; already read `.gd` symbols since GD-9 | no change there |
| `Bead: <id>` on a test file | Python docstring only | Also `## Bead: <id>` in a GDScript header | `lang.bead_of` |
| Quality review collector | `test_*.py` only; `.gd` dropped silently, then auto-PASS | Every known language collected; one with no scanner is listed as **not scanned** and blocks the auto-PASS | `gate_integration._task_own_test_files` |
| Quality review scanner | Python AST only | GUT scanner: empty, pending, trivial, doubling the script under test; GDScript test-mode branches in source | `quality_gd.py`, `quality_review.scan` |
| Suppression scan | `.gd` not scanned | `.gd` scanned, `@warning_ignore` is a marker. A red state records which file types its baseline covered (signed); older red states are rescanned with the old set, so existing markers are not reported as new | `suppression.py`, `gate_integration` |
| Measured green report | pytest only; a GDScript green report was written by hand | `record_runner_handshake(..., runner="gut")` builds it from GUT's JUnit XML; a test GUT never reported counts as failed | `gut_runner.py` |
| Mutation testing | Python only | `python -m blind_tdd.mutate_gd`: token mutator, GUT per mutant, parse breaks scored `invalid` | `mutate_gd.py` |
| Impossible-AC probes | pytest sandboxes | Unchanged. Research tooling, not part of using the gate | `probes.py` |

## Measured on real projects

**Quality scanner, false positives.** Run read-only over two GUT suites:

- godot-prompt-bridge, 116 tests: one candidate, `test_sanity_gut.gd`'s `assert_true(true)`, which is
  real.
- Soapbox-2D, 3,967 tests: 69 candidates on the first version, 20 after fixes (17 pending, 3 trivial).
  The fixes: a helper in the same file that asserts counts as an assertion; `assert_null(x)` is a
  real claim; a string or bracket spanning lines no longer ends a test body; `assert_true(flag)` on
  a bare name and `assert_eq(f(), f())` (a determinism check) are not flagged. Each fix has a test
  quoting the Soapbox-2D line it came from.

**Mutation, godot-prompt-bridge `harness_reporter.gd` lines 16–43**, against
`test_pb2_harness_reporter.gd`, on a scratch copy: 16 mutants in 2 minutes (about 7 s per Godot run).
8 killed, each naming the GUT test that caught it; 3 invalid (string `+` turned into `-`, rejected
by Godot's type checker); 5 survived, score 0.62. The survivors are at lines 27–32: the fallback that
reads the task id from `bridge-state.json` when `.themis/current_task.json` is missing. No PB-2
test reaches that path.

**Measured green report.** `record_runner_handshake("PB-2", ..., runner="gut")` on the same copy:
14 passed, exit 0, 6 s. With the script under test removed so the test file cannot load, the report
reads 14 failed, all 14 listed under `not_run`.

## Things to know when using it

- **GUT's own addon prints parse errors on every run** under GUT 9.6 + Godot 4.6
  (`godot_singletons.gd`, `stub_params.gd`). The mutation runner only treats a parse error as
  `invalid` when it names the mutated file's `res://` path.
- **`bash` from Python on Windows can be WSL.** Windows searches System32 before `PATH`, and
  `System32\bash.exe` runs the script inside Linux, where `C:/...` Godot paths do not exist. The
  mutation runner and the GUT recorder skip it (`mutate_gd.resolve_shell`).
- **Mutation edits the source file in place** and restores it afterwards. Run it on a checkout no one
  else is editing. Each run gets its own `user://` folder through APPDATA / XDG_DATA_HOME.
- **A mixed task** whose tests are partly `.py` and partly `.gd` needs both runners; the recorder
  takes one runner per call.
- **GUT hook scripts** (`pre_run_script`, `post_run_script` in `.gutconfig.json`) can change what
  every test sees, like a `conftest.py`. The seal treats a new `conftest.py` as in scope; it does not
  yet do the same for GUT hooks.

## Usage

```bash
# measured green report
python -c "from blind_tdd.orchestrator import record_runner_handshake as r; r('PB-2', test_dirs=['tests/contracts/'], runner='gut')"

# mutation, only the lines a task changed
python -m blind_tdd.mutate_gd addons/x/panel.gd \
    --test-cmd "bash scripts/tools/run_tests.sh tests/contracts/test_x.gd" \
    --changed-since HEAD~1 --json .themis/mutation/panel.json
```
