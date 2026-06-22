# blind-tdd demo

A deterministic, self-contained demo of the gate, driving the **real shipped
hook scripts** (no model calls, no network). It shows the four load-bearing
beats:

1. a blind writer's read of the implementation is **blocked** by the path-guard
2. the same writer may still read its spec surface (`public_api.md`)
3. **red → green**: a SHA-256-sealed failing test goes green once code is written
4. the **seal + lock** catch tampering — editing a hash-locked test is blocked,
   and a changed test no longer matches its sealed hash

## Run it

```bash
bash plugin/blind-tdd/demo/run.sh
```

It creates a scratch project in a temp dir, runs the beats, and cleans up after
itself. Nothing outside the temp dir is touched.

## Watch the recording

[`demo.cast`](./demo.cast) is an [asciinema](https://asciinema.org) v2 cast:

```bash
asciinema play plugin/blind-tdd/demo/demo.cast        # play in a terminal
# or convert to a GIF for embedding in docs:
agg plugin/blind-tdd/demo/demo.cast demo.gif          # needs `agg`
```

The cast's *content* is the real output of the shipped hooks; only the
inter-line timing is synthesized (see "Regenerate" below), so the recording is
reproducible rather than hand-captured.

## Regenerate the cast

```bash
# Portable (Linux/macOS): runs run.sh itself
python plugin/blind-tdd/demo/make_cast.py

# Windows / git-bash: a bash spawned from Python gets a stripped env, so pipe a
# real run in instead
DEMO_COLOR=1 bash plugin/blind-tdd/demo/run.sh 2>&1 | python plugin/blind-tdd/demo/make_cast.py
```

`asciinema rec` is not used directly: it needs a Unix pty (no `termios` on
Windows). `make_cast.py` produces an equivalent v2 cast from a real run.

## Transcript

```
== 1. The blind writer tries to read the implementation ==
# Claude Code sends this PreToolUse event to the path-guard hook:
[blind-tdd] BLOCKED: Read access to 'src/calculator.py' denied by blind-TDD policy
  (path matches blocked pattern 'src/**'). Session: demo-writer, role: test_writer.
-> hook exit 2 (2 = BLOCKED): the writer cannot see the code.

== 2. The same writer reads its allowed spec surface ==
-> hook exit 0 (0 = allowed): public_api.md is on the whitelist.

== 3a. RED — writer authors a failing test from the spec alone ==
-> tests FAIL (RED): no implementation yet, as required.

== 3b. SEAL — fingerprint the test with SHA-256 before code exists ==
sealed tests/test_calculator.py -> sha256:8d688ee5ad668cf4...

== 3c. GREEN — implementer writes code; the sealed test goes green ==
-> tests PASS (GREEN): the sealed AC-1 test is satisfied.

== 4a. The implementer tries to edit the hash-locked test — BLOCKED ==
[blind-tdd] BLOCKED: Edit access to 'tests/test_calculator.py' denied by blind-TDD policy
  (write to hash-locked test file (red_state)). Session: demo-runner, role: test_runner.
-> hook exit 2 (2 = BLOCKED): a sealed test cannot be edited in-band.

== 4b. Even a tamper through the shell breaks the seal ==
sealed:  8d688ee5ad668cf4...
current: 716713f51b4e4f6e...
-> SHA-256 mismatch: the runner fails the gate regardless of the result.
```
