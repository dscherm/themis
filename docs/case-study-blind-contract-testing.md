# Case study: substantive blind contract testing (and the trust gap it motivates)

A worked example of what independent, blind contract testing looks like in
practice — and an honest account of the one thing this example *didn't* have,
which is the thing Themis makes structural.

## Context

A ~1,000-line feature was added to a sibling project (`ralph-universal`): an
optional [beads](https://github.com/gastownhall/beads) task backend behind the
project's task ledger. It had been built with a normal test suite (unit +
integration) and had already passed an adversarial code review that read the
implementation and found — among other things — one HIGH-severity latent bug
the green tests had missed.

The question that prompted this exercise: *the tests were written by the same
process that wrote the code — how much do they really tell us?* That is the
exact problem Themis is built around, so we ran a second pass in the Themis
**method**, by hand.

## What was done

1. **Retroactive acceptance criteria.** Ten Given/When/Then criteria (`AC-1`…
   `AC-10`) were written from the feature's design doc — id preservation,
   fine-grained status round-trip, done-closes-and-unblocks-dependents,
   dependency-aware "next", atomic-claim exclusivity, concurrency safety,
   fail-closed behavior, self-heal of a partial close, json-backend
   unchanged, and stat-recount semantics.

2. **A blind writer.** A fresh agent was given *only* those criteria and the
   public CLI surface, and instructed to write black-box contract tests
   without reading the implementation or the existing tests.

3. **Run against the live system.** Its tests drove only the public CLI and
   observed external state (the task DB's `export`/`ready`, exit codes, the
   on-disk mirror). Result: **10/10 criteria pass**, then independently
   re-run by a third party to confirm.

## What made it *substantive* (not a rubber stamp)

These are the properties worth copying — they're what separate a real
independent test from one that passes by construction:

- **Derived from the spec, blind to the code.** The writer never read the
  implementation or the author's tests, so its tests encode *the contract's*
  expectations, not the implementation's behavior. A test written after
  reading the code tends to assert whatever the code happens to do.

- **Black-box only.** Every test drove the documented CLI and asserted on
  externally observable state. No test reached into internals, so none could
  pass for a reason invisible to a user.

- **Two-sided assertions.** The dependency and self-heal criteria asserted the
  *negative before* and the *positive after* (`dependent NOT ready before the
  blocker is done`, `ready after`) — not just the happy path. One-sided
  "it eventually works" assertions are where weak tests hide.

- **Real concurrency.** The atomic-claim and no-clobber criteria used two
  actual concurrent processes/threads and asserted the exact contract
  (exactly one claimer wins with a distinct exit code; two workers on
  different tasks both survive) — rather than simulating it single-threaded.

- **Independent reconstruction of a failure mode.** For the self-heal
  criterion the writer *constructed the broken state itself* (a task left
  half-closed) from the spec's description and checked recovery — arriving at
  the same failure mode the code review had found, from the opposite
  direction.

- **It surfaced a real environment fact on its own.** The writer independently
  hit a platform-specific execution issue (a Windows `.cmd` shim) in its own
  test harness — corroborating a risk the implementation had already had to
  handle, with no knowledge that it had.

A green result with those properties is a strong, specific claim: *every
acceptance criterion is independently satisfied by the running system.*

## The gap this example had — and why it's the whole point of Themis

Read the properties above and notice what's holding them up: **an
instruction.** The writer was *told* not to read the implementation. Its
blindness was a matter of good-faith compliance, not a wall. Nothing stopped
it from opening the source; nothing sealed its tests against later edits; the
same conversation orchestrated both the author and the "independent" reviewer.

Worse, the writer was an **in-conversation Agent-tool subagent** — the exact
spawn path this project's own data flags as the leakiest: identical probes
cheated roughly 10× more through in-conversation subagents than through fresh
`claude -p` agents (see [`impossible-ac-results.md`](impossible-ac-results.md)
§4). So this exercise leaned on good faith *through the highest-leakage
channel Themis has measured.* It held — but "it held this time" is a sample of
one, which is the difference between an anecdote and a guarantee.

That is precisely the trust-based independence Themis rejects. Themis makes
each of these a property of the system instead of a promise:

| Property in this case study | How it was held up here | How Themis enforces it |
| --- | --- | --- |
| Writer can't see the implementation | Instruction ("don't read src") | `PreToolUse` hook **denies** any read of source/examples/generated data |
| Tests aren't quietly edited to pass | Nobody edited them | Each test is **SHA-256 sealed** before code exists; the runner rejects any change, shell included (HMAC-signable with `THEMIS_SEAL_KEY`) |
| The runner is truly fresh | Same session re-ran them | A **separately spawned** runner that never saw the writer's or implementer's reasoning |
| A disputed test can't just be deleted | N/A (no dispute) | Only a fresh **arbiter** can remove a locked test, on an explicit challenge |

So this exercise is best read two ways at once. As a **template**, it shows
the shape of a substantive blind contract pass — the AC discipline, the
black-box stance, the two-sided and concurrent assertions, the independent
reconstruction of failure modes. As a **motivation**, it shows why that shape
isn't enough on its own: everything good about it depended on an agent
choosing not to look, and Themis's contribution is to remove the choice.

## One caveat this example shares with Themis

Passing all ten criteria says the code is independently *consistent with the
spec* — it does **not** prove the tests are *strong* enough to fail a wrong
implementation, and it does not judge whether the spec itself is right or
complete. Test strength is a separate, advisory concern (mutation testing,
which needs an agent that *can* read the code — the opposite of the blind
writer). Keep "passed an independent, sealed test of every criterion" and
"the code is correct" as two different statements. This case study earns the
first; neither it nor Themis earns the second for free.
