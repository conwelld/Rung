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

### The server decides phase advancement, not the model

**Chose:** `advance_phase` from the model is one of three conditions. The server
also requires a minimum number of turns in the phase, and for APPROACH a stated
plan of real length.
**Rejected:** Trusting `advance_phase`.
**Why:** The model has been asked to be helpful, and a helpful model waves a
student through CLARIFY in one turn and accepts "idk" as an approach. The phase
gate is the single most valuable behaviour in the tool, because the rubric
penalises coding without explaining, so it cannot rest on a field the model
fills in. The test stub says `advance_phase: true` on every single turn, which
means every phase-gate test is really testing the server rather than the model.
**Cost:** A student with a genuinely terse but correct plan gets held an extra
turn. Cheap compared to the gate being decorative.

### An engine layer between routes and the proctor

**Chose:** `app/engine.py` owns phase, rung ceiling, budget, and persistence.
Routes do HTTP. `rung/proctor.py` does one API call.
**Rejected:** Putting session logic in the route handlers.
**Why:** The rules that make this an interview rather than a chat window are the
part worth testing, and they should not be reachable only through an HTTP
request. The engine is exercised directly in tests with no client at all, and
the same code runs under Flask.
**Cost:** One more file, and a small temptation to let logic drift into routes
that has to be resisted.

### Session state is rebuilt from the database every request

**Chose:** Nothing held in memory between requests. The budget is reconstructed
from stored turns; the cookie holds a session id and a handle.
**Rejected:** Keeping live session objects in a process-level dict.
**Why:** In-memory state means a restart drops every interview in progress, two
workers disagree about whose turn it is, and the limits become resettable by
whoever can make the process forget. Rebuilding costs one indexed query and the
limit then survives a restart, which is what makes it a limit.
**Cost:** A query per turn. Irrelevant next to an API call taking a second.

### The timer is server-side, the countdown is decoration

**Chose:** `remaining_seconds` computed from `started_at` on the server, sent on
every turn and polled every 20 seconds.
**Rejected:** A JavaScript countdown as the clock.
**Why:** A client-side timer can be paused from the devtools console, and the
users are CS students. The visible countdown exists so the page feels alive; the
server's number is the one that ends the session.
**Cost:** A poll every 20 seconds per active session. One row read, no model
call.

### In-process rate limiting, with the limitation written down

**Chose:** A per-handle deque in the Flask process.
**Rejected:** Redis. Also rejected pretending it is not a limitation.
**Why:** Correct for one worker, which is what this runs as, and it adds no
infrastructure to a project a department has to maintain. Multiple workers would
each keep their own counter, so the effective limit multiplies. The comment in
`app/server.py` says so, because the wrong fix is not choosing Redis early, it
is shipping a limit that quietly does not hold and never saying so.

### The API is stubbed in tests, deliberately

**Chose:** `tests/test_app.py` replaces `ask_proctor` with a stub.
**Rejected:** Testing routes against the real API.
**Why:** Everything under test here is code I wrote: the phase gate, the turn
cap, the ceiling per mode, transcript retention, cookie ownership, quotas, rate
limits. Running these against the real API would be slow, would cost money per
run, and would mostly test the model. Whether the proctor holds the line is a
separate question, answered by `evals/`, which does call the real API.

### Testing that templates actually rendered what they contain

**Chose:** Assert the interview page contains its script tag, its injected
state, and its event handlers, and fetch every other template through a real
request.
**Rejected:** Trusting that a template file containing JavaScript serves a page
containing JavaScript.
**Why:** Jinja discards anything a child template places outside a block. No
error, no warning. The first version of the interview page had its script
appended after the closing block tag, so the page rendered perfectly, looked
correct in a browser, and had zero JavaScript on it. The timer sat at --:-- and
the Send button did nothing. Every existing test passed, because they all
checked status codes and the presence of the problem prompt, which were fine.
The second version then failed to compile because the fix comment wrote Jinja
tag syntax inside a JavaScript comment, and Jinja parses tags wherever they
appear. Two failures from the same misunderstanding: a template is a program,
not a text file with holes in it.
**Cost:** Tests that assert on page content are more brittle than tests that
assert on status codes. That brittleness is the point here.

### Both scores are kept, not just the proctor's

**Chose:** Two rows per dimension, `self` and `proctor`, shown side by side with
the gap called out.
**Rejected:** Replacing self-scoring with proctor scoring.
**Why:** A student who rates their communication a 3 against the proctor's 1 has
learned something neither number says alone. Calibration is part of what the
interview format teaches, and it disappears the moment the tool just hands down
a verdict. The debrief asks them to self-score first, then shows the comparison.
**Cost:** A more complicated schema and a page with more on it than a single
grade would need.

### Behavioural evidence is supplied to the grader as fact

**Chose:** Hint depth, turns per phase, phase reached, and duration are computed
and handed to the grader alongside the transcript, with the problem-solving
dimension told to weight hint depth heavily.
**Rejected:** Giving the model the transcript and asking for four scores.
**Why:** The department rubric names "did not require any major hints" as an
explicit problem-solving signal, so the tool already measures one dimension
directly. Passing that as a fact rather than letting the model infer it from the
conversation grounds at least one score in something that is not an impression.
It also means the grade cannot contradict the diagnostic.
**Cost:** A longer prompt, and the grader's judgement on the other dimensions
is still a model's judgement.

### The grader may refuse to score

**Chose:** Null for technical competency when no code is submitted, and null for
debugging when the session never got there. A null is not stored as a row.
**Rejected:** Scoring every dimension every time.
**Why:** Technical competency is about code. Scoring it from how well a student
described a plan would produce a confident number with nothing behind it, which
is worse than an absent one because it looks like information. A null is also
different from a zero, and the schema keeps them different: absent means not
assessed, zero means assessed and absent.
**Cost:** A debrief page that sometimes says "not scored", which looks less
finished and is more honest.

### Validating the grader for sycophancy, not accuracy

**Chose:** `tools/check_grader.py` runs three constructed sessions of known
quality and asserts the scores separate, that the range is used, that the weak
session scores at most 1, and that the no-code case returns null.
**Rejected:** Spot-checking a few grades by eye.
**Why:** The failure mode for an LLM grader is not being wrong, it is being
uniformly encouraging. A grader that hands everyone "leaning hire" produces a
page that looks like feedback and contains none, and it passes any test that
only checks the endpoint returned something. Separation is testable; accuracy on
a single session is not.
**Cost:** Three fixture sessions to maintain, and a few cents per run.

### Transcripts are working memory, not a record

**Chose:** Turn text is always stored while a session runs, then purged when the
session ends and is graded, unless `RETAIN_TRANSCRIPTS` is on.
**Rejected:** Never storing text. Also rejected storing it permanently.
**Why:** Never storing it was the previous design and it was quietly broken: the
API is stateless, so those rows are the proctor's only memory of the
conversation, and without them it saw one message at a time and could not hold
an interview. Storing it permanently means a semester of student work, mistakes
included, in a file on a class server.
Purging at the end gets both. The proctor has full context while it matters, the
grader reads the transcript once at the moment it is useful, and what survives
is the grade and the hint-depth data. The useful artefact outlives the sensitive
one.
**Cost:** A session cannot be re-graded or reviewed after the fact. That is the
trade, and it is the right way round: a grade you can reread is worth more than
a transcript you have to protect.

### A grading failure does not fail the session

**Chose:** The session closes, then grading is attempted, and an exception is
swallowed. The debrief renders with self-scores and a note.
**Rejected:** Letting a grading error propagate.
**Why:** Grading is the last thing that happens, after the interview is already
over and the hint-depth data is already stored. An API hiccup at that moment
should not leave a session open forever or lose thirty minutes of a student's
work. Tested with a grader that raises.

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
- Whether the editor should run code. Right now it is a plain textarea kept in
  the browser. CodeMirror plus Pyodide would make it a real editor with tests,
  and would also mean deciding what happens to code the student writes.
- Whether the grader should see the hint-depth evidence for dimensions other
  than problem solving. It currently sees all of it, which may be anchoring
  communication and debugging scores to how much help the student needed.
- Whether `Session.solved` should exist at all. Pass/fail is a weak signal at
  this level and hint depth is the better one, so it may be a column that
  invites the wrong question.
- Whether 177 clean adversarial cases means the constraint is robust or the
  attacks are still too easy. Running the red-teamer repeatedly at higher counts
  is the cheapest way to keep testing that.
