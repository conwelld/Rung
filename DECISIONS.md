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
the square of turn count. At current Sonnet 5 pricing: 5 turns is about $0.021,
40 turns is about $0.452.
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
with a small generic evaluation bank, and `rung/problems.py` as a validating
loader.
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

### Production defaults to safe, development opts in

**Chose:** `RUNG_ENV` defaults to production. In production a missing
`RUNG_SECRET_KEY` is fatal and cookies are Secure; development relaxes both.
**Rejected:** Defaulting to development, or falling back to a random key
everywhere.
**Why:** The dangerous configuration must not be the one you get by forgetting a
variable. Flask's debug mode serves an interactive console that runs arbitrary
Python from the browser, so a debug server on a public URL is a remote shell.
A random secret key is subtler: it works perfectly on one worker and silently
logs everyone out on a restart or a second worker, which is a miserable bug to
diagnose. Refusing to boot is louder and kinder.
**Cost:** One more environment variable to set, and tests have to declare
themselves as development.

### The database accepts a URL, not just a path

**Chose:** `init_db` takes either a file path or a connection URL, so
`DATABASE_URL=postgres://...` switches backends.
**Rejected:** Hardcoding SQLite.
**Why:** SQLite is right for a class-sized deployment and wrong for the free
tier's ephemeral disk, where the file is wiped on every deploy. Making the
backend a config value means the demo runs on SQLite and a real class runs on
Postgres with no model or query changing. That is the concrete payoff of the ORM
in this project, and it is the answer to why peewee is here rather than raw SQL.
**Cost:** A branch in `init_db` plus a real `psycopg` runtime dependency.
Postgres integration still needs exercising against a live disposable database
before a class relies on it; the offline suite uses SQLite.

### The database is seeded at import, not by a build step

**Chose:** `wsgi.py` calls `ensure_database()` before gunicorn serves anything.
**Rejected:** A one-off setup command after deploying.
**Why:** The free tier's filesystem is ephemeral, so there is no "once". Every
cold start gets an empty disk, and a manual step that has to run after every
wake is a step that will not run. The seeder is idempotent, so this is a no-op
on a warm container.
**Cost:** Slightly slower first request after a cold start, on top of the
tier's own 30 to 60 second wake.

---

## Round 4: complete product loop

### Course position gates access; authored labels frame challenge

**Chose:** `RUNG_CURRENT_UNIT` opens the current and earlier units. Later units
stay visible as a roadmap but their problems are disabled in the UI and rejected
again on the server. The problem bank may also mark a problem `warmup`, `core`,
or `stretch` for quick scanning inside its unit.
**Rejected:** Showing every problem, trusting a disabled radio button, and
model-generated or absolute "easy / medium / hard" ratings.
**Why:** The course supplies the honest difficulty axis: when material is taught.
Instructor-authored challenge labels add useful local context without telling a
student who is struggling that a problem is universally "easy." A client-only
gate is editable in devtools.
**Cost:** One deployment value to advance during the semester, plus an optional
bank field to review when the instructor changes a problem.

### Picker progress means practiced, not solved

**Chose:** A problem counts toward picker progress after any session reaches its
normal end, regardless of score or stop reason. The UI calls this "practiced"
and explicitly says it is not mastery.
**Rejected:** Solved checkmarks, streaks, rankings, and completion percentages
that imply competence.
**Why:** Rung measures how independently a student reasoned, not whether one
submission passed. A finished rep is objective and encouraging; mastery is a
larger claim the available evidence cannot support.
**Cost:** Progress measures exposure. Students still need the concept diagnostic
and debrief to understand the quality of that practice.

### A problem row starts the session

**Chose:** After entering a handle and choosing Interview or Drill, clicking a
problem row submits that problem and starts the session immediately.
**Rejected:** Selecting a radio row and then finding a separate Start button.
**Why:** The second click confirmed no destructive action and introduced an
avoidable split-attention step. A full-row submit button is also a larger touch
target and exposes its action directly to keyboard and assistive-technology
users.
**Cost:** There is no intermediate selected state. The row therefore includes a
directional cue and instructional copy so its navigation behavior is explicit.

### The homepage explains behavior before naming mechanisms

**Chose:** The header has ordinary links to Practice and How it works, while the
brand mark reads as a ladder rather than a hamburger menu. The hero states one
core idea and spells out what each hint level does.
**Rejected:** A nonfunctional drawer icon, a row of button-like product claims,
and unexplained language such as "help has a ceiling."
**Why:** A student should understand what will happen before starting a timed
session. Controls must look interactive only when they are interactive, and the
product should not require prior knowledge of its own vocabulary.
The hint ladder is the single explanation on the homepage; a second three-habit
section repeated the same behavior and was removed.
**Cost:** The hero contains a little more explanatory copy, balanced by removing
three competing badges, the abstract ladder graphic, and the redundant lower
explainer.

### Required fields use words, not color alone

**Chose:** The handle label always says "Required" and explains why. Invalid
submission adds a written alert, moves focus to the field, and sets
`aria-invalid`; the red border is supporting information only.
**Rejected:** Relying on the browser focus outline or color change after a
student clicks a problem.
**Why:** A prerequisite should be visible before the student encounters it, and
validation must remain understandable without color perception.
**Cost:** Two short lines beneath the field when invalid, one while empty.

### Recommendations are deterministic

**Chose:** Rank available problems from stored hint depth, concept tags, and
attempt counts.
**Rejected:** Asking the LLM what a student should practice next.
**Why:** The database already contains stronger evidence than another model
opinion. Keeping selection deterministic makes a recommendation explainable:
the page can name the concept and average rung that caused it.
**Cost:** The ranking is deliberately simple and will need classroom evidence
before its weights deserve sophistication.

### Python runs in a killable browser worker

**Chose:** Pyodide/WebAssembly in a module Web Worker, terminated after four
seconds and recreated after a timeout.
**Rejected:** Running student code on Flask. Also rejected running WebAssembly on
the UI thread.
**Why:** Server execution turns an interview tool into a remote-code-execution
service. Main-thread WebAssembly lets an infinite loop freeze the timer, chat,
and End button. A Worker gives the browser a process boundary it can terminate;
the final code crosses the network only once for grading and is not retained.
**Cost:** A first-use runtime download from jsDelivr, no turtle/tkinter, and a
browser dependency that needs cross-browser performance testing.

### Self-assessment is rendered before the proctor score

**Chose:** The debrief withholds proctor notes until all four self-scores are
saved, then reloads the comparison.
**Rejected:** Showing both immediately on the same page.
**Why:** A visible proctor score anchors the student's answer and destroys the
calibration signal the two-source schema exists to capture. The server does not
render the hidden content, so devtools do not turn this into a CSS convention.
**Cost:** One extra click before feedback appears.

### Instructor analytics are aggregate and separately protected

**Chose:** An environment-backed instructor code enables class-level concept and
participation aggregates. With no code, the route is disabled.
**Rejected:** A public dashboard, and a transcript browser.
**Why:** `class_overview()` already identified lecture-level patterns but had no
product surface. A separate gate makes the role boundary explicit while keeping
the demo simple. Transcript deletion still applies; the dashboard cannot show
data the system no longer retains.
**Cost:** A shared instructor code is appropriate for a small deployment, not a
replacement for campus SSO. Real classroom rollout should put the app behind the
institution's identity layer.

### The actionable next step survives transcript deletion

**Chose:** Store the grader's overall recommendation in `SessionFeedback` while
purging turn text.
**Rejected:** Discarding the overall line, or keeping the transcript so it could
be regenerated later.
**Why:** The previous code paid to generate the most actionable part of the
grade and then threw it away. A short next step has low privacy cost and high
student value; retaining the source transcript reverses that trade.
**Cost:** An eighth table and a feedback statement that cannot be regenerated
after model changes.

### A permanent cookie for the handle, not accounts

**Chose:** Mark the session cookie `permanent` (90 days) when a handle is set
in `/start`, so a returning student is recognized without retyping it.
**Rejected:** Real accounts with passwords or email verification. Leaving the
cookie session-only, so it clears every time the browser closes.
**Why:** The diagnostic only needs to attribute a session to a consistent
label, not to a verified identity, so the identity layer should cost the
student nothing. A password is friction with no payoff at this stakes level: a
mistyped handle already fragments a student's own history exactly as much as
a forgotten password would lock them out of it. A longer-lived cookie is pure
convenience layered on the same non-authentication -- it does not make the
handle any more verified, it just stops asking a returning student to retype
something the browser already knew.
**Cost:** The instructor login route explicitly resets `cookie.permanent` to
`False` after signing in, so a shared browser that already carries a 90-day
student cookie can never inherit that lifetime for the elevated instructor
cookie. Skipping that line would be a real privilege-duration bug, not a
cosmetic one.

### Elastic Beanstalk over App Runner or Lightsail for the AWS path

**Chose:** A single-instance Elastic Beanstalk environment, with CloudFront in
front for TLS, as the AWS equivalent of the Render deployment.
**Rejected:** App Runner (no free tier, and scale-to-zero reintroduces the
cold-start problem this project already has on Render's free tier). Lightsail
(a flat, predictable price, but a bare VM to patch and administer by hand with
no free tier at all).
**Why:** Elastic Beanstalk's single-instance shape reuses the existing
gunicorn/wsgi.py entry point almost unchanged, is free for 12 months on
`t3.micro`, and its instance disk survives ordinary deploys and reboots --
unlike Render's free tier, where `/tmp` is wiped on every deploy, restart, and
spin-down. An Elastic Beanstalk single instance serves plain HTTP with no
certificate of its own, and `SESSION_COOKIE_SECURE=True` means the handle
cookie is silently dropped by every browser over plain HTTP, so CloudFront in
front is load-bearing, not cosmetic polish.
**Cost:** SQLite at `/var/rung-data` does not survive an instance
*replacement* (health-based auto-replacement, a platform/AMI update, or
turning on load balancing or auto scaling later), only ordinary deploys and
reboots. Acceptable for a portfolio link; not a substitute for the Postgres
escape hatch DEPLOY.md already documents if this needs to survive real
classroom use. See AWS_DEPLOY.md.

### A per-IP cap on session starts, not just per-student

**Chose:** `STARTS_PER_HOUR_PER_IP` (5), checked in `/start` before a `Student`
row even exists.
**Rejected:** Trusting `DAILY_DRILL_LIMIT`/`DAILY_INTERVIEW_LIMIT` alone.
**Why:** SECURITY.md's abuse model was written for "a bored student with a
loop" inside one classroom roster. Once the link is public on a resume, the
per-student daily cap stops mattering: `Student.get_or_create` asks for
nothing but a string, so a script rotating through fresh handles pays for a
full interview session every time with no cap ever engaging. The IP address is
a weaker identity than a handle -- shared networks and VPNs sit behind one --
but it is an identity a script cannot type its way around the same way it
types a new handle, and it costs nothing to check before the expensive part
(the Anthropic call) happens.
**Cost:** A real classroom's shared NAT (one dorm, one lab) could hit this
before hitting the per-student caps on a busy day. The Anthropic console spend
cap with auto-reload off, already recommended in SECURITY.md, is still the
actual backstop; this only exists to keep that backstop from being the first
line of defense.

---

## Round 5: a class deployment, not a portfolio link

### Solved means every check passed, alongside practiced

**Chose:** A session is solved when its final code passes every `[input,
expected]` check in the bank. Practiced stays exactly as it was. The picker,
progress page and instructor view show both, and the progress page puts the
hint depth it took next to every solve.
**Rejected:** Keeping practiced only, as round 4 decided. An LLM judging
correctness from the code. Running the checks on the server.
**Why:** Round 4 rejected solved checkmarks because "solved" implied competence
the evidence could not support. That objection was to the claim, not the
measurement. Passing the bank's own checks is a narrow, objective fact that
students asked for and the course wants to see, and pairing it with hint depth
keeps the round 4 point intact: solved at rung 1 and solved at rung 4 are
different records. An LLM verdict on correctness is an opinion with variance.
Server execution is remote code execution.
**Cost:** The browser reports the result, so a student can forge their own
solved flag in devtools. Acceptable for a practice record; it must never feed a
grade. Problems without checks (the class-definition unit) can be practised but
never solved, which looks less finished and is more honest.

### The checks run the same code in the browser and in the tests

**Chose:** `rung/checks.py` is ordinary Python, handed to the Pyodide worker as
source and imported by the offline suite.
**Rejected:** A checker written in JavaScript, or Python embedded as a string in
the worker.
**Why:** Code that only ever runs in a browser only ever gets tested by hand.
Keeping it in a module means the suite exercises the exact file the browser
runs: argument unpacking, the JSON round trip that makes `(3, 4)` equal `[3,
4]` and `{1: ...}` equal `{"1": ...}`, `True` not passing for `1`.
**Cost:** The harness must stay standard-library-only and within what Pyodide's
Python accepts.

### Failing inputs are withheld until the debrief

**Chose:** During a session the student sees "2 of 4 checks pass" and the first
exception, never which input failed. The debrief names the failing calls after
self-assessment.
**Rejected:** Showing each failing case as it happens.
**Why:** The rubric's debugging dimension is about finding the case that breaks
your own code. A list of failing inputs does that work for the student, the
same way a proctor naming the edge case would. After the session it is the
most useful thing on the page, so it is shown then, behind self-assessment for
the same anchoring reason as the proctor scores.
**Cost:** A frustrated student with a failing check has to reason about which
edge case it is. That is the exercise.

### Topics are a second way to list problems, and the default

**Chose:** An optional `topic` per problem (Strings, Lists, Dictionaries...),
a picker that groups by topic by default with a toggle back to course units,
easiest-first ordering inside a topic, and a progress-by-topic table.
Difficulty stays the round 4 labels, shown under a "Difficulty" heading with a
three-step meter.
**Rejected:** Replacing units with topics. Easy/medium/hard labels.
**Why:** Students think "I'm weak on dictionaries", not "I'm weak on unit 3",
and a data-structures course will not map onto one intro course's units at all.
Units still gate what is open, so they stay. The round 4 reasoning against
"Easy" still holds; the labels just needed to be recognisable as difficulty.
**Cost:** Two groupings to keep coherent. A bank without topics falls back to
unit titles, so an old private bank still works unchanged.

### CloudFront is a template, not console instructions

**Chose:** `deploy/cloudfront.yaml`, deployed with one command, linted in CI.
**Rejected:** The step-by-step console instructions from round 4.
**Why:** Following those instructions exactly produced a site where nobody
stayed signed in, pages could be cached across students, and every POST
failed. Every one of those was a console default. Three settings (cache policy,
origin request policy, allowed methods) decide whether the app works, and a
template makes them reviewable and repeatable.
**Cost:** The AWS CLI or the CloudFormation console becomes part of the deploy.

### The app trusts exactly two proxies, and only CloudFront can reach it

**Chose:** `RUNG_PROXY_HOPS=2` on Elastic Beanstalk, and a secret header that
CloudFront adds and the app requires (`RUNG_ORIGIN_SECRET`).
**Rejected:** Trusting `X-Forwarded-For` wholesale. Leaving it at the default.
Restricting the security group to CloudFront's IP ranges.
**Why:** Behind two proxies every request came from 127.0.0.1, so the per-IP
start cap from round 4 was one cap for the whole class. Trusting the header
fixes that only if nothing can skip CloudFront and write the header itself,
which the environment's public `elasticbeanstalk.com` address allowed. The
secret header closes that at the application, where it is tested; a security
group edit is invisible to the test suite and easy to undo in the console.
**Cost:** Two secrets that must match, and a 403 on the EB address that looks
like an outage until you know why.

### Hourly SQLite backups to S3 instead of Postgres

**Chose:** A cron job that snapshots the database with SQLite's online backup
API to the account's Elastic Beanstalk bucket, and a restore on any deploy that
finds no database.
**Rejected:** RDS Postgres, which round 4 pointed to as the path for a real
class. Relying on the instance disk alone.
**Why:** For one class, one small instance is plenty, and losing an instance
should cost at most an hour of practice rather than a semester. Backups do
that for cents a month with no new infrastructure, no IAM changes, and no
second database to secure. Checking the Postgres path turned up that it has
never worked: `init_db` calls `initialize()` on a `SqliteDatabase`, and the
concept query uses SQLite's `IIF`.
**Cost:** Up to an hour of data lost on an instance failure. Still one
instance and one worker. Postgres needs fixing and a real test before it is an
option.

### The worker is a .js file, and the CSP allows Pyodide's helper worker

**Chose:** Rename `code-runner-worker.mjs` to `.js`; add jsDelivr to
`worker-src`.
**Why:** A module worker refuses to start unless its script is served with a
JavaScript MIME type, and `.js` is the extension every server gets right. The
pinned Pyodide release starts a helper worker from its own CDN URL, which
`worker-src 'self'` blocked, so the runner never loaded. `script-src` already
trusts that origin, so this adds no exposure.

### Daily caps reset at Berea's midnight

**Chose:** `TZ=America/New_York` on Elastic Beanstalk.
**Why:** Instances run in UTC, so "today" for the daily caps ended at 8pm
Eastern for half the year. A student's evening practice session should not
count against tomorrow.

### Vercel + Supabase as the fastest path to a live class

**Chose:** The Flask app as one Vercel function (`wsgi.py`), Postgres on
Supabase through its transaction pooler, deployed with the Vercel CLI from the
working folder.
**Rejected:** Finishing the AWS setup first. Connecting the GitHub repository
to Vercel.
**Why:** It needed to be live the same day, and Vercel removes the parts of the
AWS path that took longest: certificates, DNS validation records, CloudFront
settings, a server to back up. Supabase is plain Postgres, so the app needed no
new code beyond making the Postgres path real. The CLI deploys what is in the
folder, which is the only way the gitignored course bank reaches the function
without publishing it.
**Cost:** In-memory rate limits count per function instance. A free Supabase
project pauses after a week idle. Hobby-plan terms are non-commercial.

### The Postgres path is tested, not assumed

**Chose:** `database` is a `DatabaseProxy`; CASE replaces SQLite's `IIF`;
averages are converted to float; psycopg's prepared statements are off for
Postgres URLs; the app suite runs against Postgres in CI
(`RUNG_TEST_DATABASE_URL`).
**Why:** Round 3 advertised `DATABASE_URL=postgres://...` as a one-variable
switch. It failed on its first line, and two more Postgres-only failures sat
behind it: `IIF` does not exist, and Postgres returns AVG as a Decimal that
raises when the recommendation score adds a float. The SQLite suite could never
see any of it. Supabase's transaction pooler adds a fourth: it reuses server
connections between transactions, so prepared statements vanish mid-session.
**Cost:** A second CI job with a Postgres service container.

### Static files live in public/static

**Chose:** Move them out of `app/static`; Flask and nginx serve the same
folder, and `vercel.json` adds `public/**` to the function bundle with
`includeFiles`.
**Why:** Vercel's Python build treats `public/` as CDN files and leaves it out
of the function. The CLI configures the app as a Vercel "service", and routing
into a service is final, so the CDN never answers and Flask 404ed every
stylesheet in production while every local check passed. One folder, included
explicitly, works on every host.

### Cost limits that hold under attack, not just in normal use

**Chose:** An atomic per-session turn lease, an atomic claim on finishing, a
site-wide daily session ceiling counted in the database, and an optional class
code.
**Rejected:** Trusting the turn cap, the per-handle caps and the in-memory
per-IP limit, which is what round 4 did.
**Why:** Asked whether the API budget could be run up, the honest answer was
yes, two ways. Parallel requests all read the same turn count before any of
them stored a turn, so a session's cap could be passed twenty times over. And
handles are free, so the per-handle caps bound a student but not a script;
the per-IP cap counts per process and a VPN steps around it. The lease makes
calls for one session strictly sequential (20 simultaneous messages, 2 billed,
tested live). The ceiling turns the worst day into a number nobody can raise.
The class code removes the public internet from the threat model.
**Cost:** A session's second tab waits for the first tab's reply. A determined
student can make Rung "full for today" for the class; that is visible in the
logs and costs at most the ceiling.

### Daily caps use the course's timezone, not the server's

**Chose:** `RUNG_TIMEZONE` (default `America/New_York`) decides midnight.
**Why:** Vercel's clock is UTC and cannot be changed the way Elastic
Beanstalk's `TZ` can, which would end a student's day at 8pm.

---

## Still open

- Rung escalation policy. What earns a student the next rung: elapsed time,
  failed attempts, or asking?
- Whether the judge scores all four rubric dimensions per turn or only at
  session end. Per turn is richer and roughly four times the cost.
- Whether `DAILY_DRILL_LIMIT` of 12 is right. It sets the funding ceiling
  directly, and halving it halves the number in the proposal.
- How accommodations are handled, given that the proctor cannot verify a claim
  made mid-interview and refusing everything that sounds like one is the wrong
  answer. Probably account settings rather than conversation.
- Whether the grader should see the hint-depth evidence for dimensions other
  than problem solving. It currently sees all of it, which may be anchoring
  communication and debugging scores to how much help the student needed.
- Whether browser-reported checks are enough once solved counts matter to an
  instructor. Server-side verification means sandboxed execution, which is a
  project of its own.
- Whether class-definition problems should get checks, which needs a way to
  express "construct, call methods, compare" in the bank.
- Moving the per-minute and per-hour rate limits into the database, so they
  hold across Vercel's function instances.
- Whether 177 clean adversarial cases means the constraint is robust or the
  attacks are still too easy. Running the red-teamer repeatedly at higher counts
  is the cheapest way to keep testing that.
