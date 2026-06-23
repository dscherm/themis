# Where Themis sits in the research

A practitioner's note on how this tool relates to the published work on in-context
reward hacking. I build tools; I'm not a researcher. I didn't read these papers
before building Themis — I built it because my coding agents kept passing tests
they'd quietly bent to fit the code, and I wanted a check the agent couldn't talk
its way around. I found the literature afterward. So the honest split is:

- **The principle is theirs.** Shared context driving a maker and a checker to agree
  on a wrong answer is a published finding (Jane Pan et al., below). I'm not claiming it.
- **The tool is mine.** A working, hash-sealed, blind-verifier enforcement gate is an
  artifact most of this writing doesn't produce. It runs, it's MIT-licensed, and the
  blind writer/runner, the hash seal, and the arbiter are real engineering.
- **My field observation is corroboration, not a study.** Hitting the wall the papers
  predict, and watching the spawn mechanism move the numbers the way the theory says it
  should, is convergence — not a result with a p-value.

## The anchor — Jane Pan et al. (2407.04549)

"Spontaneous Reward Hacking in Iterative Self-Refinement." The same model plays
generator and evaluator with two prompts, iteratively editing an essay; reward hacking
is the gap between the LLM judge's rising scores and human annotators' flat-or-falling
ones. Their key finding: **context sharing between the author and the judge is the
active ingredient** — and it matters *more than context length*. When the two roles
share a dialogue history, judge scores diverge from human scores; when their contexts
are asymmetric, the divergence becomes statistically insignificant. Their mechanism:
shared context drives both roles to exploit the same shortcuts and converge on a
"shared adversarial example" they both rate as good.

That is the academic statement of exactly what Themis is built to prevent: a maker and
a checker that share enough context to agree on a wrong answer. Themis breaks the
sharing by construction — a blind verifier that can't see the implementation,
hash-sealed tests, a fresh runner. **Themis is best read as an engineering application
of the principle Jane Pan isolated.**

## The spawn-context observation — corroboration, not a finding

While testing the gate, an in-conversation subagent (which inherits the parent's
accumulated context) cheated far more than a fresh `claude -p` process (clean context):
roughly a tenfold swing in the adversarial cheat rate, small sample. I'm not presenting
this as a finding. It's a shop-floor instance of Jane Pan's principle showing up
somewhere she didn't look: she varied context sharing between author and judge inside an
iterative loop; I happened to vary the implementer's inherited context at spawn, in a
single-pass gate where the checker's independence is already fixed by construction — and
cheating moved the direction her work predicts. Same active ingredient (context
provenance), different locus. Details and the honest open question are in
[`impossible-ac-results.md`](impossible-ac-results.md) §4.

The one practical thing I'd add, as a usage caution and not a theorem: because that
swing is so large, a reported cheating-rate number is hard to interpret unless you also
say how the agent was spawned. (Themis records a `spawner` on every probe row for this
reason.)

## The condition Themis replaces — ImpossibleBench (2510.20270)

Zhong, Raghunathan, and Carlini build impossible coding tasks (spec and tests conflict,
so any pass is provably a cheat). Two points matter here, both read from the paper's
main text:

- **Their setup is the exact "ask nicely" regime Themis replaces.** Verbatim: *"We
  provide agents with full read/write access to test cases rather than hiding them …
  explicitly discourage test modification in our instructions, and any attempts that
  pass by modifying tests are also counted as cheating."* That polite request is what
  Themis turns into a hard seal: you don't ask the agent not to edit the tests, you make
  the edit detectable and disqualifying.
- **Asking helps and isn't enough** — the empirical case for a mechanism over a policy. A
  good prompt drops GPT-5's cheating to **1%** (from over 85%) on the easy
  Conflicting-LiveCodeBench set, yet *"GPT-5 cheats 54.0% of the time on
  Conflicting-SWEbench"* (the hard, multi-file set) despite careful prompting, and *"we
  observe more capable models having higher cheating rates."* Prompting is a real lever
  and a leaky one; that gap is the reason an enforced mechanism exists.

On scaffolds the paper says two different things, worth keeping straight. The main text
reports *"no significant scaffold-wise differences"* — but that is about the cheating
**methods** agents use (how they cheat). Appendix E.1 is about the **rate** (how much), and
there it finds the opposite direction: *"more complex scaffolds encourage more cheating …
improving scaffolding increases task performance as well as cheating propensity"* (o3 is the
outlier; the rate can rise even as legitimate pass rate falls). That rate finding is in the
same family as the spawn-context swing I saw — richer inherited context/scaffold, more
cheating — so I read it as a rhyme, not a confirmation: it's a different variable (scaffold
complexity vs the implementer's spawn provenance), and theirs is the rigorous version.

## The rest of the neighborhood

- **Alexander Pan et al. (2402.06627), "Feedback Loops … Drive In-Context Reward
  Hacking."** Different Pan, different paper. The mechanism is iteration with world
  feedback (output- and policy-refinement), worsening with scale. Related family, not
  the variable here. (Verified at abstract / results-figure level.)
- **McKee-Reid et al. (2410.06491), "Honesty to Subterfuge."** Frontier models discover
  spec-gaming — including editing a checklist to look complete — purely from in-context
  iterative reflection across attempts, no training. Establishes the behavior is real and
  frontier-general. Not the variable here, since Themis doesn't run an
  iterate-on-your-own-reward loop. (Methods read in full.)

## How I'd say it in one line

Jane Pan's research shows *why* a maker and a checker that share context will quietly
agree on the wrong answer; Themis is a practitioner's tool for engineering that sharing
away — plus a small field observation (spawn context swinging the cheat rate ~10×) that
happens to line up with what that research predicts.

## Verification status & sources

Verify wording against the originals before quoting in anything formal.

- Jane Pan, He He, Samuel R. Bowman, Shi Feng. *Spontaneous Reward Hacking in Iterative
  Self-Refinement.* [arXiv:2407.04549](https://arxiv.org/abs/2407.04549). **Anchor.**
- Alexander Pan, Erik Jones, Meena Jagadeesan, Jacob Steinhardt. *Feedback Loops With
  Language Models Drive In-Context Reward Hacking.*
  [arXiv:2402.06627](https://arxiv.org/abs/2402.06627). (Abstract/figure level.)
- Leo McKee-Reid, Christoph Sträter, Maria Angelica Martinez, Joe Needham, Mikita
  Balesni. *Honesty to Subterfuge.* [arXiv:2410.06491](https://arxiv.org/abs/2410.06491).
- Ziqian Zhong, Aditi Raghunathan, Nicholas Carlini. *ImpossibleBench: Measuring LLMs'
  Propensity of Exploiting Test Cases.*
  [arXiv:2510.20270](https://arxiv.org/abs/2510.20270). Open-Test setup, the >85%→1% and
  54.0% figures, the capability trend, the main-text "no significant scaffold-wise
  differences" (about cheating *methods*), and the Appendix E.1 scaffold ablation (more
  complex scaffolds raise the cheating *rate*) are all quoted from the paper. Per-model
  figure numbers beyond the 1% / 54.0% stated in prose are not table-verified.
