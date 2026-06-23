# Limitations

Themis is defense in depth, not a perfect wall. This is the honest list of where
its guarantee ends. Read it before relying on the gate. The empirical caveats
(sample size, the spawn-mechanism confound) live in
[`impossible-ac-results.md`](./impossible-ac-results.md) and are summarized at
the bottom.

## The spec is the trust boundary

The gate establishes that the code does what the **specification says** — not that
the specification is correct, complete, or wise. A vague, under-specified, or
simply wrong set of acceptance criteria can earn a confident green on the wrong
behavior. The blind writer can only test what the spec states; if the spec omits
a case, no test covers it.

`preflight.py` reduces this risk but does not remove it. It checks *structural*
quality — that each `then` clause contains an observable assertion, that every
declared public-surface identifier is exercised by a criterion, that subjective
language (`smoothly`, `properly`) is flagged — but it cannot judge whether the
criteria describe the *right* behavior. Garbage spec, garbage oracle. Spec
review remains a human responsibility upstream of the gate.

## Test strength is not measured

The gate enforces a test's *independence* (written without sight of the
implementation) and its *integrity* (not edited afterward). It does **not** prove
the test is *strong* — that it would actually fail a wrong implementation. A test
can be independent, untampered, AC-tagged, and still too weak to catch a real bug
(it might assert a return type but not the value).

Measuring strength means perturbing the implementation and checking whether some
test fails — **mutation testing** — which requires something that *can* read the
code, the opposite of the blind writer. So it is necessarily a separate,
**advisory** pass, not part of the gate. That pass now exists: `blind_tdd.mutate`
applies deterministic single-change AST mutations to a source file, runs the suite
against each in a wall-clock-bounded subprocess (a mutated conditional can spin —
that is recorded as a timeout, never a hang), and reports any **surviving** mutant
(one the tests still pass) as a weak spot. It is advisory by design — a survivor
is a warning, never a gate failure — and it does not change the gate's guarantee.
(Equivalent mutants exist — a change that cannot alter behavior, e.g. adding 0 —
so a non-zero survivor count is a prompt to look, not proof of a weak test.) So a
green gate remains **necessary but not sufficient** for correctness: it guarantees
the code satisfies an independent, untampered test of every criterion — not that
those tests are exhaustive.

## The hash seal assumes an intact baseline

Layer 2 (the load-bearing defense) works by recomputing each test file's SHA-256
and comparing it to the fingerprints recorded at the red phase. Those fingerprints
are stored on the filesystem at `.themis/blind_tdd/red_state/<task_id>.json`. The
guarantee therefore reduces to the **integrity of that baseline file** and of the
gate-runner process that reads it:

- An actor able to write to the red-state JSON could replace a stored fingerprint
  with the hash of a tampered test, re-sealing the cheat. The hash layer defends
  against editing the *test*; it assumes the *record of the seal* is not itself
  attacker-controlled.
- The green report (`.themis/blind_tdd/green_report/<task_id>.json`) is likewise a
  filesystem artifact the runner trusts.

**Mitigation (optional, opt-in): sign the seal record.** Set `THEMIS_SEAL_KEY` in
the gate/host environment. The red phase then HMAC-SHA256s the baseline into the
record (`seal_hmac`), and the green phase verifies it before trusting the
baseline — a modified record fails (fail-closed: a signed record with no key to
verify it is also rejected). The spawner strips `THEMIS_SEAL_KEY` from every
spawned agent's environment (like `ANTHROPIC_API_KEY`), so the writer/implementer/
runner can read the record but cannot forge its signature. This raises the bar
from "edit a JSON number" to "exfiltrate a key the spawner deliberately
withholds." When the key is unset, records are unsigned and verification is a
no-op — unchanged behavior, no false failures.

This does not make tampering *impossible* — it shrinks the problem to protecting
one key. Themis still assumes the gate's own process is trusted; it is not a
sandbox against an adversary who already controls that process or can read its
environment. For that, isolate the gate from the agent (run it host-side, outside
the agent's sandbox).

## Blindness is only as complete as its configuration

The path guard denies reads of the paths it is told to deny (typically `src/`,
examples, and generated data). If the implementation — or a faithful copy of its
behavior — is reachable from a location *not* in the deny list (an unguarded
fixture directory, a vendored copy, a path the project layout puts somewhere
unexpected), the blind writer can see it, and blindness is silently weaker than it
looks. Adopters must make sure the guard actually covers every route to the
implementation for their project layout.

## The arbiter can be argued with

When the implementer challenges a test, a fresh arbiter agent rules
upheld / rejected / ambiguous from the implementer's written argument plus the
test and spec. The arbiter is an LLM exercising judgment, and a sufficiently
persuasive-but-wrong argument can get a *correct* test deleted (an erroneous
"upheld"). Mitigations bound the blast radius rather than eliminate it:
per-task and per-criterion challenge caps, and an `ambiguous` ruling that escalates
to a human instead of auto-deleting. It is a smaller surface than letting the
implementer edit tests directly, but it is not zero.

## Results are per-model snapshots

The prompts, the blindness-compliance behavior, and the measured cheat rates are
tied to the specific Claude models they were validated on. Agent behavior — how
often a model even *attempts* to tamper, whether it escalates to Bash, how it
argues a challenge — drifts across model versions. The `0 / 42` bypass figure is a
snapshot, not a permanent property of the design. Re-validate (re-run the probe
harness) when you change the model behind the spawner.

## Scope: executably-specifiable behavior only

Themis grounds behavior you can write a test against. It says nothing about
declarative knowledge, prose, design rationale, or any claim that has no
executable oracle. It is not a lie-detector for an agent's text.

## Empirical caveats

From [`impossible-ac-results.md`](./impossible-ac-results.md):

- **Small sample.** n = 21 per cell; the 9.5% no-gate adversarial rate (2/21) has a
  wide confidence interval. Don't quote a figure without reading §7 there.
- **Spawn-mechanism confound.** Identical probes cheated ~100% through
  in-conversation subagents versus ~9.5% through fresh `claude -p` agents — a ~10×
  swing driven by *how the agent was spawned*. Every reported number uses the
  production spawner; the strongest evidence that the hash layer catches Bash
  tampering comes from the over-aggressive contrast arm, used as a stress test, not
  a baseline.
