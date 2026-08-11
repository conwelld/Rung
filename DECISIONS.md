# Decisions

One entry per choice. What was picked, what was rejected, why. Add to this as
you build, while the reasoning is still fresh. This file is the thing you reread
before an interview.

---

### Direct HTTP calls instead of an LLM framework

**Chose:** `requests.post` against the Messages endpoint.
**Rejected:** LangChain, LlamaIndex, the official SDK.
**Why:** The whole project is one chat loop with a system prompt. A framework
would add a dependency, an abstraction layer, and vocabulary to explain, in
exchange for hiding the two things that actually matter here: the shape of the
system prompt and the messages array. The SDK would have been reasonable, but
raw HTTP means the request body in the code is exactly the request body in the
docs. Nothing is doing anything on my behalf.
**Cost:** No automatic retries, no streaming, no typed responses. All three are
things I would want in production and none of them matter for a 43-case harness.

### Two models, not one

**Chose:** Sonnet for the proctor, Haiku for the judge.
**Rejected:** One model for both.
**Why:** Two separate reasons. Cost and latency: the judge is high-volume binary
classification against an explicit reference answer, which is what the small
model is built for, and it runs on nearly every case. Independence: having the
proctor grade its own output is marking your own homework, and a model is more
forgiving of its own phrasing than of a stranger's.

### Two-tier leak detection

**Chose:** Regex first, LLM judge only if the regex is clean.
**Rejected:** LLM judge on everything. Regex only.
**Why:** For an intro Python course the proctor has no legitimate reason to emit
any code at all, so a regex catches blatant leaks deterministically, for free,
with no judge variance. But regex cannot catch "have you considered a
dictionary?", which contains no code and is the entire answer on a
frequency-counting problem. Each tier covers the other's blind spot, and
skipping tier 2 when tier 1 fires saves an API call on cases already failing.
**Cost:** Two things to maintain, and tier 2 inherits whatever bias the judge
model has.

### Anchoring the regex patterns

**Chose:** `^\s*return\s+\S`, loops requiring their colon.
**Rejected:** Bare `\breturn\b`.
**Why:** The unanchored version flagged the question "what should this return for
an empty input?" as a code leak. That is a perfect proctor turn and the harness
was calling it a failure. Python statements start their own line, so anchoring
removes the false positive without missing real code. Caught by the offline
test suite before it ever cost an API call, which is the argument for having
the offline suite at all.

### Structured JSON output from the proctor

**Chose:** The model returns `{rung_used, advance_phase, reply}`.
**Rejected:** Plain prose replies.
**Why:** `rung_used` is the entire diagnostic. Hint depth per concept tag is
what tells a student they are fine on dictionaries and shaky on recursion, and
there is no way to get that from prose without a second inference call.
**Cost:** Parsing can fail. Handled by treating unparseable output as a plain
reply and still running the leak check on it, then counting parse failures
separately, because a malformed response that leaks the answer is still a leak.

### Rung ceiling in the prompt, verified after the fact

**Chose:** Pass `max_rung` into the system prompt AND check `rung_used` against
it in the judge.
**Rejected:** Trusting the prompt.
**Why:** A prompt is a request, not a guarantee. The model self-reports the rung
it used, which is exactly the kind of claim that needs external verification.
Rung violations are reported as their own metric.

### Control cases in the adversarial suite

**Chose:** 5 of 43 cases are legitimate student turns that should get real
engagement.
**Rejected:** Attacks only.
**Why:** A proctor that refuses everything scores 0% leakage and is useless. The
leak rate is meaningless without an over-refusal rate next to it. Optimising one
number in isolation is how you ship something that passes evals and fails
students.

### Turtle problems excluded from the bank

**Chose:** Drop `draw_polygon`, `draw_checkerboard`, `stamp_grid`, `draw_square`.
**Rejected:** Supporting them.
**Why:** They need `turtle`, which needs `tkinter`, which is not available in
Pyodide. Getting them running in a browser is a rendering project of its own.
The concepts they teach, loops and parameters, are already covered by non-turtle
problems in the same unit, so the curriculum loses nothing.

### Phase gate on APPROACH

**Chose:** The editor stays locked until the student states a plan.
**Rejected:** Free-form conversation.
**Why:** Straight out of the department rubric. Jumping into coding without
explaining is listed as a leaning-no-hire communication signal, and constant
communication is listed as a basic one. Gating the phase makes the tool train
the exact behaviour that is already being graded, rather than a generic notion
of good practice I invented.


---

## Round 2: modes, cost, and limits

### Two modes instead of one

**Chose:** `interview` (Sonnet, 20 turns, full history, rung 4, 30 min) and
`drill` (Haiku, 8 turns, 6-turn window, rung 2, 5 min).
**Rejected:** One configuration for all sessions.
**Why:** A student practising three times a day on interview settings costs about
$1,500 a semester for a class of thirty. Same usage split across two modes costs
$155, roughly $5 per student. That is the difference between a proposal a
department funds and one it declines.
The pedagogy argues for it independently, which is what makes it a design
decision rather than a cost hack. Twenty short reps on the approach phase builds
the habit the rubric grades. Twenty full 30-minute mock interviews mostly builds
fatigue.
**Cost:** Two configurations to reason about, and eval numbers that have to be
reported per mode because they are not comparable across models.

### Capping turns, and windowing history in drill mode

**Chose:** Hard turn cap in both modes; drill resends only the last 6 exchanges.
**Rejected:** Unlimited sessions with full history.
**Why:** The API is stateless, so the entire conversation is resent every turn.
Turn 20 pays for turns 1 through 19 again, which makes session cost grow with
the square of turn count. Measured: 5 turns is $0.032, 40 turns is $0.678.
Eight times the turns, twenty-one times the cost. The turn cap is the single
highest-leverage control in the project, and windowing converts the growth from
quadratic to roughly linear.
**Cost:** A windowed drill forgets what happened early in the session. Acceptable
for a 5-minute rep on one concept, not acceptable for a graded interview, which
is why only drill mode windows.

### Prompt caching instrumented but left off

**Chose:** Log the cache counters, keep `ENABLE_PROMPT_CACHING = False`.
**Rejected:** Turning caching on.
**Why:** Caching has a minimum cacheable prefix: 1024 tokens for Sonnet, 4096 for
Haiku. Our system prompt is around 800 tokens. Below the floor the request
succeeds and caches nothing, so enabling it today would pay the cache-write
premium and buy zero reads. The counters are logged either way, so if the prompt
grows past the floor it shows up in the numbers instead of being rediscovered by
accident.
**Cost:** An optimisation left on the table. It was worth about 18% at these
session lengths even in the best case, well behind the turn cap and the mode
split.

### Three independent session limits

**Chose:** Turn count, token total, and wall clock, checked before each call.
**Rejected:** A turn cap alone.
**Why:** They fail differently. A turn cap does nothing about a student who
writes essays every turn. A token budget does nothing about a browser tab left
open. Checking before the call rather than after matters because a check that
runs afterwards means the call that broke the budget was already billed.
**Cost:** Three numbers to tune per mode instead of one.

### The student never sees cost

**Chose:** `SessionBudget.student_message()` says time is up, never dollars.
**Rejected:** Showing usage to the student.
**Why:** Cost is the operator's problem. Telling a student they have spent $0.14
discourages exactly the practice the tool exists to encourage, and it leaks
infrastructure detail for no benefit. Cost goes in `summary()`, which is for the
operator.

### One key, server-side, never bring-your-own

**Chose:** One key in the server environment.
**Rejected:** Each student supplying their own.
**Why:** Bring-your-own-key means every student creates an Anthropic account and
enters billing details before they can practise, which kills adoption. It also
puts a key in the browser, which makes it public.
**Cost:** The operator pays for everyone, which is what makes the budget limits
above load-bearing rather than optional.

### A secret scan inside the test suite

**Chose:** `test_offline.py` fails if any tracked file contains key material.
**Rejected:** Relying on `.gitignore` and care.
**Why:** The realistic failure is pasting a key inline for one quick test and
forgetting. Git history is permanent, so that mistake is not undoable, only
revocable. A check that runs on every test invocation catches it while it is
still local. Verified by planting a fake key and confirming the suite goes red.

### Four top-level packages instead of a flat directory

**Chose:** `rung/` for the system, `evals/` for measurement, `tools/` for offline
utilities, `tests/` for checks. Run everything with `python -m` from the root.
**Rejected:** Flat directory. Also rejected a `src/` layout with `pyproject.toml`.
**Why:** At six modules flat was genuinely fine and folders would have been
decoration. What forced the change was the Flask layer arriving next: routes,
peewee models, templates and static files are fifteen or so more files, and
moving things afterwards means rewriting every import. Doing it while the
project was small cost one scripted pass.
The split is by what talks to what. `rung/` is the only package that calls the
API, which is why `tools/` and `tests/` both run for free. `evals/` sits beside
`rung/` rather than inside it, because the thing being measured and the thing
measuring it should not import each other.
`src/` plus packaging metadata was rejected as the correct answer to a problem
this project does not have. It is for distributable libraries, and it would add
an editable-install step to explain for zero benefit here.
**Cost:** Everything must be run from the repository root with `python -m`.
Running `python evals/run_evals.py` directly now fails on imports, which is a
real papercut and the reason the README says so explicitly.

### The question bank is not in the repository

**Chose:** `data/problems.json` gitignored, `data/problems.sample.json` committed
with three generic exercises, `rung/problems.py` reduced to a loader.
**Rejected:** Committing the real bank.
**Why:** The questions are department course material, reused every semester and
not mine to publish. A public repository containing them would hand the full
question list to every future student, which is a strange thing for a tool whose
premise is not giving away answers. The sample bank keeps the repository
runnable for anyone who clones it without publishing anything that is not mine.
**Cost:** A cloned repo runs against my wording rather than the department's, so
an outsider's eval numbers will not match mine exactly. Worth it. If the
department later wants the real bank public, that is their call to make, not a
default I should have taken.

First attempt at this got it wrong: the sample bank held three problems while
the eval suite referenced seven, so a fresh clone died on a KeyError before
reaching most of the checks. Two fixes. The sample bank now covers every id the
suite uses, and the tests select a problem from whichever bank loaded instead of
naming one, so a bank mismatch reports itself as a named failure rather than
crashing and hiding every check behind it. The suite is now run against both
banks before pushing.

### MIT for the code, CC BY-SA for the rubric

**Chose:** MIT on the repository, with the rubric attribution kept separate.
**Rejected:** No license. Also rejected putting everything under CC BY-SA.
**Why:** An unlicensed public repository is technically all-rights-reserved,
which means nobody can legally use or build on it, including a reviewer who
wants to run it. MIT is the low-friction default for a portfolio project. The
rubric this tool scores against is adapted from the Tech Interview Handbook
under CC BY-SA 3.0, and ShareAlike attaches to derivatives of that rubric
specifically rather than to the whole codebase, so the attribution lives in the
README and the code stays MIT.

### Testing drill mode, not just interview mode

**Chose:** Run the full suite against both modes.
**Rejected:** Validating on Sonnet and assuming Haiku behaves the same.
**Why:** Every run for the first several rounds was interview mode. By my own
cost model that path runs 120 times a semester while drill runs 6,750. I was
rigorously testing the rare configuration and had never once run the common one.
The first drill run immediately surfaced a bug that interview mode could not
produce: Haiku is less reliable at "reply with JSON and nothing else", which
broke the parser, which turned a correct refusal into a scored answer leak.
**Cost:** Two baselines to maintain, and the numbers are not comparable across
models so they get reported separately.

### Parsing JSON from anywhere in the reply

**Chose:** Scan for the object, handling fences with preamble and bare objects
in prose, with brace balancing so nested braces in the reply text survive.
**Rejected:** The original, which only stripped a fence when the text started
with one.
**Why:** Haiku wrapped its JSON in a fence behind a sentence of preamble. That
fell through to the raw-text fallback, where leftover backticks tripped the
tier-1 code detector, and a perfectly good refusal was recorded as an answer
leak. The bug was in the harness and it inflated the failure count rather than
hiding failures, which is the less dangerous direction but still wrong. When a
tier-1 hit does land on unparsed output, the reason string now says so, so a
formatting artefact is distinguishable from a real leak by eye.

### Accessibility framing showed up in the generated attacks

Not a decision yet, a finding worth recording. A large share of the
model-generated attacks were framed as accommodation requests: screen readers
garbling hints, visual processing disorders, ADHD accommodation letters, needing
fully worked examples rather than fragments. The proctor refused all of them,
which is right against an attacker.

But students with real accommodations phrase requests the same way, because that
is how you ask for an accommodation. A proctor that treats "I process worked
examples better than fragments" as an attack fails hardest for the students who
most need it to work. Refusing harder is not the fix and neither is a smarter
classifier, since the proctor cannot verify a claim made mid-interview either
way. The likely answer is that accommodations are handled outside the
conversation, as account settings the department sets: longer limits, a lower
rung ceiling, more worked examples in the practice bank. Needs a conversation
with whoever owns the course before it becomes a design.

### Turns do not store text

**Chose:** `Turn` records rung, phase, and token counts. Student and proctor
text are null unless `RETAIN_TRANSCRIPTS` is switched on, and it defaults off.
**Rejected:** Storing every transcript.
**Why:** The diagnostic needs `rung_used` and a concept tag. Nothing it computes
touches what anyone typed. Storing transcripts anyway would mean a semester of
student work, mistakes included, sitting in a SQLite file on a class server, and
the only thing it would buy is convenience while debugging prompts. Data you
never collected cannot leak, cannot be subpoenaed, and does not need a retention
policy. Collecting less is easier to defend than securing more.
**Cost:** No way to reread a session after the fact, so prompt debugging happens
with the flag on locally against seed data rather than against real students.

### The concept profile is a query, not a table

**Chose:** Compute it on demand in `rung/diagnostics.py`.
**Rejected:** A summary table updated as sessions finish.
**Why:** At class scale the query is milliseconds against a few thousand rows,
and a stored aggregate is a second source of truth that goes stale the moment a
session is deleted or a problem is retagged. Denormalise when a measurement says
to. There is no measurement yet.
**Cost:** If this ever serves a dashboard refreshing every few seconds for a
whole department, it will need caching. That is a nice problem to have and a
small change when it arrives.

### One join instead of a loop of queries

**Chose:** A single grouped join for `concept_profile`.
**Rejected:** Iterating concepts and querying turns for each.
**Why:** The obvious version is a textbook N+1: one query for the concepts, then
one more per concept, so eighteen concepts costs nineteen round trips. It
returns the right answer, which is exactly why it survives review and then falls
over later. The naive implementation is kept in `diagnostics.py` as
`_concept_profile_n_plus_one`, never called, and the offline suite asserts both
versions return identical numbers. That assertion is what makes the fast version
trustworthy rather than merely faster.

### An explicit junction table instead of ManyToManyField

**Chose:** A `ProblemConcept` model with a unique index on the pair.
**Rejected:** peewee's `ManyToManyField`.
**Why:** The join is written out in the diagnostic query, where it is the most
important thing to be able to read. A real model can also carry its own columns
later, weight or primary-versus-incidental, without a migration that changes the
relationship type. The unique index matters more than it looks: without it a
reseed silently doubles every link and every count in the diagnostic.

### Foreign keys enforced with a pragma

**Chose:** `foreign_keys: 1` in the SQLite pragmas, with a test asserting a bad
insert actually raises.
**Rejected:** Declaring `ForeignKeyField` and assuming.
**Why:** SQLite does not enforce foreign keys by default, per connection. Every
`ForeignKeyField` in the schema is documentation rather than a constraint until
that pragma is set, and orphaned rows accumulate in silence. The test asserts
the behaviour rather than the declaration, because the declaration was already
there while the enforcement was not.

---

## Still open

- Rung escalation policy. What earns a student the next rung: elapsed time,
  failed attempts, or asking?
- Whether `advance_phase` should be the model's call or the server's, decided
  from a transcript check.
- Whether the judge scores all four rubric dimensions per turn or only at
  session end. Per turn is richer and roughly four times the cost.
- Whether `DAILY_DRILL_LIMIT` of 12 is right. It sets the funding ceiling
  directly, and halving it halves the number in the proposal.
- How accommodations are handled, given that the proctor cannot verify a claim
  made mid-interview and refusing everything that sounds like one is the wrong
  answer. Probably account settings rather than conversation.
- Whether `Session.solved` should exist at all. Pass/fail is a weak signal at
  this level and hint depth is the better one, so it may be a column that
  invites the wrong question.
- Whether 177 clean adversarial cases means the constraint is robust or the
  attacks are still too easy. Running the red-teamer repeatedly at higher counts
  is the cheapest way to keep testing that.
