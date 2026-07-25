# Learnings

I built this project to automate personalized job-search outreach end to end:
discover companies, identify the right contact, research them, draft a
grounded email, fact-check it, and queue it in Gmail. It took about 40 hours
and $115 in API spend, with Claude Fable 5 (in Claude Code) doing the heavy
coding. It worked — and I retired it deliberately, because
what I learned building it (and a small real-world experiment with the
emails it produced) convinced me the problem is better solved with a human
in the loop. These are the lessons, technical and product, in roughly the
order they cost me something.

## Technical

**Cost management matters even if you aren't cost-conscious.** I didn't start
this project thinking about API spend, and it found me anyway: early runs
cost $10–15 because part of the workflow ran on Opus. Input caching and
switching that work to Sonnet 5 brought a run down to $4–5 — still not cheap
for ten emails, but manageable. Basic optimizations (model tiering per step,
prompt caching, lower reasoning effort on rule-bound steps, tighter search
budgets) weren't premature optimization; they were the difference between a
tool I'd run and one I'd hesitate to. Cost also quietly gated
experimentation: I never re-tested Opus to see if its quality justified the
price, because at $10+ per run the comparison itself wasn't worth buying.
And in the end, cost was half of what killed the product — each run's price
tag combined with the email-quality issues to push me back toward chat.
Measuring mattered too: per-step usage logging revealed that contact
*identification* — not drafting — was the most expensive step. The writing
everyone sees cost almost nothing; finding and verifying the human cost
everything.

**Concurrency has a ceiling, and it's lower than you think.** Running every
company's calls at once felt free until it wasn't: too many concurrent
requests ate into the rate limit, and a mid-stream 429 discards the whole
partial generation — so the "faster" configuration was slower *and* wasted
tokens. Semaphores that queue excess calls (with the timeout clock paused
while queued) beat stampeding the limiter. Relatedly: searches inside one
streamed request run serially, so two concurrent 3-search calls finish in
roughly half the wall-clock of one 6-search call.

**Prompt rules alone don't hold; enforcement lives in code.** Every rule I
cared about eventually leaked past the prompt: banned phrases shipped,
drafts opened mid-thought, a model interpreted its own exhausted search
budget as a tool outage and discarded good results. Each rule the product
relied on needed a detection or enforcement pass in code — regex backstops,
adoption gates, deterministic post-processing. And budgets must be *told* to
the model: an unexplained `max_uses_exceeded` rejection reads as a failure,
not a boundary, and models react to perceived failures in expensive ways.

**Models have real limits on open-ended tasks.** Contact identification — an
open-ended judgment call about org structures the model can only glimpse —
sometimes went wrong in ways no prompt fixed. Writing style drifted toward
AI-recognizable patterns despite explicit bans. The model faithfully
amplified an ambiguous claim in my own resume into something that read as
false. And open-ended web research has an irreducible mess to it:
conflicting sources, stale aggregator listings, undated pages, links that
point at the wrong person with the right name.

**Multiple independent passes beat one strong pass.** No single call — even
on the strongest model available — reliably caught its own errors, because
the call that wrote a claim already believes its misreading. The
architecture that worked was adversarial and multi-pass: draft, then an
independent fact-check that never sees the research notes, then a targeted
revision, then an independent recheck that gates whether the revision ships.
Accuracy came from structure, not model strength.

**The code was the easy part; the product's factual accuracy was the hard
part.** My workflow was to write a detailed plain-language `plan.md` and let
the AI assistant build from it — and the technical code came out largely
correct that way. Two things stayed stubbornly human. First, requirements:
when I had the AI review the plan itself, it never told me I'd need a "sent
list" (the never-double-contact ledger the whole product depends on) —
domain-critical requirements don't fall out of plan review. Second, output
correctness: making the *software* correct was straightforward; making what
it *produced* factually accurate consumed as much effort as all the coding
combined, and was never fully solved.

**A stronger model for the *coding assistant* pays for itself** (subjective).
I ran the two tiers in opposite directions: in Claude Code I defaulted to
Claude Fable 5 and dropped to Sonnet 5 only for genuinely easy tasks — I
wasn't hitting usage limits often, and the stronger model meant fewer
accumulated bugs and forced redos — while the product's own runtime calls
ran on the cheaper Sonnet 5. Bugs accumulate across a project even with a
thorough test suite, and the sessions where the stronger model found root
causes — instead of patching symptoms — prevented compounding cost later.
For the code that builds the product, model quality is leverage; for the
product's own bulk work, tiering down was the right call.

**How to work with an AI coding assistant, learned by doing it for 40
hours.** Paste full run logs — they're the difference between the assistant
guessing and the assistant diagnosing, which also means logging is worth
building *for the assistant's benefit*, not just yours: every hard bug here
was found by grepping step-labeled logs. Let it run the test suite after
every change; regressions surface immediately instead of three features
later. But human direction stays load-bearing: the assistant will happily
produce technically correct code that misses the actual objective. The
factual-accuracy machinery and the "stop early" timeout button (so a user
isn't stuck waiting on one hung company out of thirteen) exist because I
asked for them, not because the code was wrong without them.

## Product

**Personalized outreach anti-scales.** The value of a personalized email
comes from its scarcity — the evidence that a human spent attention on you.
Automation optimizes throughput, which is precisely the variable that
destroys that value. Recipients are increasingly wary of AI-written email,
and they're the audience most able to detect it. A job search needs tens of
excellent emails, not hundreds of good ones, and at that volume my own
judgment in a chat session is cheap, better, and free of per-run API cost.

**One factual error costs more than all the polish earns.** These emails go
to experts. A wrong claim about their company — or an inflated claim about
me — doesn't weaken an email; it ends it. Most of the engineering effort in
this project ultimately served accuracy, not eloquence, and it still
couldn't drive the error rate to zero without me reviewing every draft
anyway. Once I'm reviewing every draft, the automation is mostly overhead.

**The experiment that settled it.** I actually sent the output. Fifteen
emails went out fully automated — no editing for errors or missing details —
and got zero responses. The emails I edited before sending got responses.
Fifteen is far too small a sample to prove anything, but the direction
matched everything else I'd observed, and it crystallized where the line is:
AI does fine at discovering companies and doing initial research, and
AI-assisted drafting is workable — but the process has to be at least
human-directed (prompting for re-verification, reading every email, steering
the style, ideally checking the facts yourself), and it's best with heavy
human input. Full automation is the one configuration I won't rely on.

**The human review gates were the most valuable components.** The
company-approval gate and the flagged-claims report did more for outcome
quality than any generation improvement. That was the tell: the product's
best features were the ones that inserted *me*. The honest conclusion of
that trend is doing the work in chat with a checklist — which is where this
project ended up (see `OUTREACH_CHECKLIST.md`).

**Killing a project on evidence is a result, not a waste.** The system
works, the test suite is green, and I'm shutting it down because I measured
what it produces against what the problem actually rewards. What survives:
the verification architecture, the guardrail checklist, the sent-list data,
and everything above.
