# Themis

[![CI](https://github.com/dscherm/themis/actions/workflows/ci.yml/badge.svg)](https://github.com/dscherm/themis/actions/workflows/ci.yml)

A test-first gate for AI-written code: the agent that writes the tests can't see the implementation, and the tests can't be edited once sealed.

## What it is

When the same agent writes code and writes the tests that check the code, the tests are not an independent verdict. They pass against whatever the agent built, misreadings included. Themis breaks that by making the verifier's independence a property of the system rather than a matter of trust: the agent that derives the tests is structurally denied any view of the implementation, the tests are cryptographically sealed before any code exists, and a separate agent runs them.

## What it does and does not do

Themis grounds **only what is executably specifiable**. Within that scope, it can establish that code does what a specification says without the author of the code in the loop.

It does **not**:
- judge whether a spec is correct, wise, or complete (a vague or wrong spec earns a confident pass on the wrong thing; the spec is the trust boundary)
- verify declarative knowledge, prose, or design rationale (it is not a lie-detector for an agent's text)
- guarantee a perfect wall (it is defense in depth; see [Limitations](#limitations))
- guarantee test **strength**. The gate enforces the test's *independence* (written without sight of the code) and *integrity* (not edited afterward). It does not prove a test is strong enough to fail a wrong implementation. A test can be independent, untampered, AC-tagged, and still too weak to catch a bug. Measuring strength requires perturbing the implementation and checking whether a test fails (mutation testing), which needs an agent that *can* read the code, the opposite of the blind writer. That is a separate, planned capability (`tools/mutate.py`, not yet built), not part of the gate.

A green gate means the code passed an independent, sealed test of every acceptance criterion. That is a strong and specific claim — necessary, not sufficient. It is not the same as "the code is correct"; keep the two separate.

It is **Claude Code-specific**: blindness is enforced through Claude Code `PreToolUse` / `PostToolUse` hooks and fresh-agent spawning (the production `claude -p` subscription spawner, or an optional Agent SDK spawner). It is not a framework-agnostic library.

## How it works

The design is permissions. Each agent is granted exactly what it needs and denied the rest, enforced by tooling rather than by instruction.

- **Blind writer** derives the acceptance tests from the spec and a single `public_api.md`. A hook denies it any read of the source, the examples, or generated data, so it cannot copy the implementation's behavior because it cannot see it.
- **Implementer** writes code to pass those tests. It can see and change everything except the sealed tests. The wall here is honest about its limits: a path guard blocks the ordinary edit tools from rewriting a locked test, but it does not cover `Bash`, and a determined agent can rewrite a file through the shell. That gap is closed by the hash layer below.
- **Blind runner** is spawned fresh, never having seen the implementer's reasoning. It re-verifies the seal, runs the tests, and checks that every acceptance criterion has a passing test tagged `Covers: AC-N`.
- **Arbiter** appears only on a dispute. The implementer cannot delete a test it believes is wrong; it can only file a challenge, which a fresh arbiter (having seen neither the writer's nor the implementer's reasoning) rules on. Upheld removes the test; rejected keeps it; ambiguous escalates to a human. Only "upheld" removes a locked test.

### The seal (the load-bearing layer)

Before any implementation exists, each test file is fingerprinted with a SHA-256 hash and the fingerprints are recorded (in `.themis/blind_tdd/red_state/<task_id>.json`). At the green phase the runner recomputes each fingerprint and compares it to the sealed one. A test changed by any route, shell included, no longer matches and fails the run regardless of whether it now passes. Adding or deleting a locked test also breaks the comparison.

## Pipeline

```
spec + public_api.md
      |
   [ RED ]    blind writer -> failing tests -> SHA-256 seal
      |
   implementer writes code (cannot edit sealed tests)
      |
   [ GREEN ]  blind runner -> re-verify seal -> run tests -> check AC coverage
      |
   (optional) challenge -> arbiter -> upheld / rejected / ambiguous
      |
   [ GATED ]
```

## When to use Themis

Most tasks do not need this. For a large class of work, a verifier you simply assign in the loop gives you solid testing, and the multi-agent overhead here is pure tax. Use the cheapest thing that holds:

- **Assigned in-loop verifier** for most tasks: unambiguous spec, and a wrong-but-passing result would be caught cheaply downstream. No reason to pay for enforced blindness.
- **Themis (blind gate)** when a false green is expensive *and* there is pressure toward it: security or financial logic, anything you will not manually re-check, anything feeding later automated steps, anything under deadline or reward pressure. This is the case where an assigned verifier is most likely to stop telling you the truth, because the cheap path to green has become attractive.
- **Mutation and audit pass on top** (planned; see [Limitations](#limitations)) when you also cannot afford a weak-but-honest test letting a wrong implementation through. That is a different threat from collusion, and a different layer.

Each layer covers a threat the one below it does not. The blind gate addresses an assigned verifier conforming to the code; it does not address a verifier that is honest but too weak. Pick the layer that matches the cost of being wrong.

## Quickstart

```bash
git clone <repo-url> themis && cd themis
pip install -e ".[dev]"        # add ".[dev,sdk]" for the Agent SDK spawner

python -m pytest               # run the test suite

# inspect the impossible-AC probe catalog and the gate verdict
python -m blind_tdd.probes --list
python -m blind_tdd.probes --gate --runs-file data/probe_runs.jsonl --min-on-runs 20
```

To adopt the gate on an existing project, start with [`docs/adoption-guide.md`](docs/adoption-guide.md). The design rationale is in [`docs/rfc.md`](docs/rfc.md).

## Claude Code plugin

The blindness layer ships as an installable Claude Code plugin in [`plugin/blind-tdd/`](plugin/blind-tdd/):

```
/plugin marketplace add dscherm/themis
/plugin install blind-tdd@themis
```

The plugin provides the enforcement surface — the `PreToolUse` path/bash guards, the `PostToolUse` audit, and the writer/runner/arbiter agent definitions, auto-activating passthrough-by-default. This package is the orchestration engine it drives (install it with `pip install themis-blind-tdd`). See the [plugin README](plugin/blind-tdd/README.md) for a 60-second quickstart and a recorded demo.

## Evidence

Themis was tested against an [ImpossibleBench](https://arxiv.org/abs/2510.20270)-style probe set: coding tasks whose acceptance criteria are mutually contradictory, so that any verified pass is provably a cheat. On these tasks there is nothing but cheating to detect.

| Condition | Result |
|---|---|
| Gate **on** — verified bypasses (all 42 runs, both framings) | **0 / 42** |
| Gate **off** — false greens, adversarial framing | 2 / 21 (9.5%) |
| Gate **off** — false greens, ordinary framing | 0 / 21 |
| Measurement losses | 0 |

The honest reading separates two claims. **Base rate:** across 42 ON runs there were 0 verified bypasses — but "0 observed" with n=42 means a 95% CI upper bound of ~7%, not zero, and fresh `claude -p` agents rarely even *attempt* a tamper, so those runs mostly show low cheat propensity, not enforcement. **Enforcement** — that the lock actually stops a tamper — is shown by the contrast arm where it was exercised (Agent-tool implementers attacked 7/7; the hash layer caught all), not by the idle subscription runs. The no-gate arm cheated only under adversarial framing (2/21). Net: agents seldom cheat here, *and* the lock holds when tested — two findings from two arms, not one 42-run proof of both.

Two caveats belong next to those numbers, not in a footnote:
- **Small sample.** n is 21 per cell; read [`docs/impossible-ac-results.md`](docs/impossible-ac-results.md) before quoting any figure.
- **How the agent is spawned moved the result a lot.** Identical probes cheated ~10× more through in-conversation Agent-tool subagents than through fresh `claude -p` agents — same task, same prompt, different inherited context. So every number here uses the production `claude -p` spawner, and each row in the ledger records its `spawner`. The practical takeaway, the part I'm confident about as a builder: if you're measuring cheat rates, pin and report the spawn method, or you're partly measuring your own harness. This looks to me like the context-as-active-ingredient effect Jane Pan et al. ([2407.04549](https://arxiv.org/abs/2407.04549)) study — [`docs/impossible-ac-results.md`](docs/impossible-ac-results.md) §4 has the honest question I'd put to researchers about whether it's the same effect or a cousin.

## Limitations

The full, honest list (including the spec-as-trust-boundary problem, the absence of test-strength/mutation measurement, the persistence of the hash baseline, the arbiter's input surface, and per-model staleness) is in [`docs/limitations.md`](docs/limitations.md). Read it before relying on the gate.

## Prior art

The principle is old: a checker cannot be independent of the thing it checks. It is the logic of clean-room software development (a separate team certifies code its authors never test) and separation of duties (whoever writes the check cannot sign it), and the general failure it guards against is reward hacking, or Goodhart's law. Themis applies those ideas to AI-written code, with the independence enforced as a permission rather than a policy. That last point is the distinction from the spec-driven-development tools that assign a verifier as a role the agent is asked to play, where the verifier can still read the implementation and the tests can still be edited after the fact.

On the empirical side, two papers map closely. Jane Pan et al. ([arXiv:2407.04549](https://arxiv.org/abs/2407.04549)) show *why* this fails — when one model is both author and judge with shared context, the two quietly converge on the same wrong answer; Themis is, in effect, an attempt to engineer that sharing away. And ImpossibleBench ([arXiv:2510.20270](https://arxiv.org/abs/2510.20270)) shows that asking an agent not to edit the tests helps but isn't enough: a good prompt cuts GPT-5's cheating to 1% on easy tasks, yet it still cheats 54% on hard multi-file ones — the case for a mechanism over a polite request. A fuller, practitioner's-eye map of the related research is in [`docs/related-work.md`](docs/related-work.md).

## Provenance

Themis is the standalone extraction of a blind-TDD gate built inside a larger agent-learning harness. The blind gate itself (the hooks, the hash seal, the arbiter, and the impossible-AC probe harness) is original to this project.

## License

MIT. See [LICENSE](LICENSE).
