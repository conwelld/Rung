# Rung

A Socratic proctor for mock coding interviews. It runs a student through a timed
technical interview and never gives away the answer, working from a fixed
four-level hint ladder instead of open-ended help. How far down that ladder a
student needs to go, tagged by concept, becomes their diagnostic.

Built for the CSC coding interviews at Berea College, where interviews are
already graded on communication, problem solving, technical competency, and
debugging.

The measurement came first; the product now sits on top of it. Students get a
problem picker grouped by topic (Strings, Lists, Dictionaries...) or by course
unit, with a course-relative difficulty on every problem; a server-enforced
interview flow; a Python editor that runs locally in a killable Web Worker and
checks their code against each problem's tests; a self-first debrief; and a
progress page showing what they have solved, where their hints are deepest, and
what to practise next. Instructors get a separately protected aggregate view of
class concept signals and solved counts—never a transcript browser.

## Product loop

1. The deployment opens material through `RUNG_CURRENT_UNIT`, so the difficulty
   follows the course calendar instead of exposing the whole semester at once.
2. The student must explain an approach before the code editor unlocks.
3. Python runs through Pyodide/WebAssembly in a module Web Worker. A four-second
   timeout terminates infinite loops; untrusted code never executes on Flask.
4. The student self-scores before the proctor score is rendered, preserving the
   calibration exercise rather than anchoring their answer.
5. Hint depth is grouped by concept and deterministically selects the next
   useful drill. The LLM conducts the interview; it does not invent the learning
   path.
6. A problem counts as solved when the final code passes every check in the
   bank, run in the browser by `rung/checks.py`. During a session the student
   sees only how many checks pass; which inputs fail appears in the debrief,
   because finding the breaking edge case is part of the exercise. Solved sits
   beside the hint depth it took, so "solved on my own" and "solved with the
   structural idea named for me" stay different records.

## The hint ladder

| Rung | The proctor may |
|---|---|
| 0 | Ask a question. Nothing else. Most turns land here. |
| 1 | Restate the problem in different words |
| 2 | Ask what they tried and where it broke |
| 3 | Point at a category: complexity, what a lookup needs, growth behaviour |
| 4 | Name the structural idea in plain language. No code, no variable names |

There is no rung 5. The model self-reports which rung it used and the judge
checks that against the ceiling it was given, because a self-reported constraint
is exactly the kind of claim that needs verifying from outside.

## Phases

`CLARIFY → APPROACH → CODE → DEBUG`

The editor stays locked until the student states a plan. The course rubric marks
candidates down for jumping into code without explaining, so the tool enforces
the behaviour that is already being graded.

## Two modes

Practice and assessment are different products with different economics.

| | interview | drill |
|---|---|---|
| Model | Sonnet | Haiku |
| Length | 30 min, 20 turns | 5 min, 8 turns |
| Phases | all four | approach, code |
| Ladder ceiling | rung 4 | rung 2 |
| History sent | full | last 6 exchanges |
| Cost per session | ~$0.15 | ~$0.02 |

A drill is a rep on one concept the diagnostic flagged. A graded interview is
the real thing, four times a semester. Running drills on interview settings is
what makes unlimited practice unaffordable, and short reps on the approach phase
are better practice anyway than twenty full-length mock interviews.

## Cost

```
python -m tools.cost_model
```

No API key needed. Every assumption is at the top of the file, so the numbers
are re-derivable rather than asserted.

For a class of 30 over a 15-week semester, with three drills a day and four
graded interviews each:

| | Sessions | Cost |
|---|---|---|
| Drills | 6,750 | $128 |
| Graded interviews | 120 | $18 |
| **Total** | | **$146** (~$4.86 per student) |

Running every one of those drills on interview settings instead: **$1,003**.
The mode split is an 85% saving.

The number to take to a department is the enforced ceiling, not the average.
With every student maxing the daily cap, 30 students is about $531 a semester.
`DAILY_DRILL_LIMIT` in `config.py` is the lever: halve it and the ceiling halves.

Why turn count dominates: the API is stateless, so the whole conversation is
resent every turn. Session cost grows with the square of turn count.

| Turns | Input tokens | Cost | Per turn |
|---|---|---|---|
| 5 | 7,000 | $0.021 | $0.0043 |
| 20 | 58,000 | $0.146 | $0.0073 |
| 40 | 196,000 | $0.452 | $0.0113 |

Eight times the turns, twenty-one times the cost. That is why the turn cap
exists and why drill mode windows history.

## Running the app

```
python -m tools.init_db
set ANTHROPIC_API_KEY=sk-ant-...
python -m app.server
```

For production, see [VERCEL_DEPLOY.md](VERCEL_DEPLOY.md) (Vercel + Supabase,
the quickest), [AWS_DEPLOY.md](AWS_DEPLOY.md) (Elastic Beanstalk), or
[DEPLOY.md](DEPLOY.md) (Render). The app runs under gunicorn, refuses
to boot without a signing key, and sets secure cookies; `python -m app.server`
is the development server and marks itself as such.

Then open http://127.0.0.1:5000. Pick a handle, a mode, and a problem.

The first time the editor unlocks, the browser downloads the Pyodide runtime
from jsDelivr. Code and console output stay in that browser worker; only the
final editor contents are sent once, at session end, so technical competency
can be graded. They are not retained after grading.

The browser never sees the API key, never sees the forbidden insight for the
problem, and never decides anything. Phase, rung ceiling, remaining time, and
the turn budget are all computed server-side from stored turns, so a restart
does not lose an interview and devtools cannot buy extra time.

The editor stays locked until the student states an approach. That is the phase
gate, and it is the behaviour most worth having: the rubric marks candidates
down for coding before explaining.

## Grading

The debrief scores the session on the department's four dimensions, twice: once
by the student, once by the proctor. Both are kept and shown together, because
the gap between them is often the most useful thing on the page.

One dimension is measured rather than judged. The rubric names "did not require
any major hints" as a problem-solving signal, so hint depth, turns per phase and
how far the student got are computed from stored turns and handed to the grader
as fact.

The grader is allowed to refuse. With no code submitted, technical competency
comes back unscored rather than inferred from how well the student described a
plan. Absent and zero are different things and the schema keeps them different.

```
python -m tools.check_grader
```

Runs three constructed sessions of known quality through the real grader and
asserts the scores separate, the full range gets used, the weak session is
actually marked down, and the no-code case refuses. The failure mode for an LLM
grader is uniform encouragement, not error: a grader that gives everyone
"leaning hire" produces a page that looks like feedback and contains none.

## The diagnostic

Hint depth, tagged by concept, is what the schema exists to produce. Pass/fail
is a weak signal in an intro course because almost everyone eventually passes.
Needing rung 4 on every dictionary problem and rung 1 on every string problem is
a profile, and it says something pass/fail cannot.

```
python -m tools.init_db --demo
python -m tools.show_profile demo
```

```
  concept                turns   avg  max  deep
  methods                   20  1.85    3     5  #########
  mutation-vs-return        40  1.75    3     9  #########
  dict-construction         43  1.56    3     7  ########
  string-traversal          48   0.6    2     0  ###
  list-traversal            67  0.57    2     0  ###
```

Each of those is one grouped join rather than a query per concept. The naive
N+1 version is kept in `diagnostics.py`, never called, and the offline suite
asserts both return identical numbers.

## Security

Students never need an Anthropic account. One key lives in the server's
environment, the browser talks only to Flask, and `test_offline.py` fails the
build if key material appears in any tracked file. Full notes in
[SECURITY.md](SECURITY.md).

## Running it

Offline first. No key, no cost, catches plumbing bugs before they look like
model failures:

```
pip install -r requirements.txt
python -m tests.test_offline
python -m tools.cost_model
```

Then the real suite:

```
set ANTHROPIC_API_KEY=sk-ant-...
python -m evals.run_evals
python -m evals.run_evals --mode drill
python -m evals.run_evals --category debug_vector
python -m evals.run_evals --limit 5
```

Run everything from the repository root. `python -m` is what makes the package
imports resolve without any path juggling or an install step.

Each run prints its own spend from the API's usage counters, so you find out
whether the cost model is honest.

## Results

177 adversarial cases across two models. Every number below is reproducible
from this repository, though against the sample problem bank rather than the
course one, so exact figures will differ slightly.

| Run | Attacks | Leaks | Over-refusal | Rung violations | Parse fails | Cost |
|---|---|---|---|---|---|---|
| interview, single-turn | 38 | **0** | 0/5 | 0 | 0 | $0.19 |
| interview, multi-turn | 11 | **0** | 0/2 | 0 | 0 | $0.07 |
| drill (Haiku), both suites | 32 | **0** | 0/3 | 0 | 0 | $0.03 |
| red team, model-generated | 96 | **0** | n/a | 0 | 0 | ~$1.00 |

Leak rate alone is not the score. A proctor that stonewalls every question leaks
nothing and teaches nothing, which is why over-refusal on the control cases sits
in the same table.

**Hand-written categories (16):** direct request, authority claim, false
completion, debug vector, incremental extraction, phase skip, emotional
pressure, format reframe, hypothetical, test fishing, rapport-then-ask, deep
extraction, false premise built, role drift, persistence, partial completion.

### The measurement was wrong three times before the result was right

A perfect score from an unvalidated instrument is worth nothing, so most of the
work went into trying to make the harness fail. It did, three times, and each
failure was in the measurement rather than the proctor.

**Tier 1 flagged good questions as code.** An unanchored `return` pattern fired
on "what should this return for an empty input?", a textbook proctor turn.
Caught by the offline suite before it cost an API call. Anchoring to line starts
fixed it.

**The judge punished the proctor for the student's own ideas.** A control case
was scored as a leak when the proctor walked a student's stated plan through a
counterexample. The judge only ever saw the proctor's reply, so it could not
tell revealing an idea from reflecting one back. It now receives the student's
turn and is told that an idea the student raised belongs to the student.
`tools/check_judge.py` holds the regression, plus a case where the student was
close but the proctor still supplied the missing piece, so the fix did not just
make the judge blind.

**A correct refusal was scored as an answer leak.** Haiku wrapped its JSON in a
markdown fence behind a sentence of preamble. The parser only handled text that
started with a fence, so it fell through to the raw-text path, where the
leftover backticks tripped the code detector. Found only by testing drill mode,
which is the mode students actually use, and which had never been run until
that point.

### Validating the judge

`python -m tools.check_judge` feeds 15 replies with known verdicts through tier
2: 7 that hand over the structural insight in plain English, 8 that are good
proctor turns. Current: 7/7 caught, 8/8 passed. A zero from the suite means the
proctor held rather than that the judge is asleep.

### Red-teaming

The hand-written suite has a structural weakness: the same person wrote the
attacks and the defences, and the system prompt names four attack categories
outright. `python -m tools.red_team` asks a model to invent attacks while
showing it only the problem statement, never the system prompt, rules, ladder,
phases, or forbidden insight. It produced angles the suite did not have,
including unit-test generation, git-diff framing, docstring extraction, and
"write the version a failing candidate would submit".

### What this does not show

Every adversarial attack is text in a chat turn. The offline application suite
checks browser-runner wiring, server gates, ownership, quotas, debrief ordering,
and instructor protection, but it is not a longitudinal classroom study or a
cross-browser performance study. The claim is bounded: across 177 adversarial
turns on two models, the proctor did not hand over a solution. Before quoting
that number after a prompt or model change, rerun the evals.

## Portfolio framing

The stack is intentionally small: Flask and Jinja for the HTTP/UI boundary,
Peewee with SQLite locally and a supported Postgres driver path for durable class
deployments, direct Claude Messages API calls where request shape matters,
Pyodide/WebAssembly for client-side Python isolation, and GitHub Actions for the
offline quality gate. Redis, Celery, a JavaScript framework, and an LLM
orchestration framework are absent because this workload does not require them.

Resume bullets that stay honest:

- Built a Socratic coding-interview coach with a server-enforced four-phase
  state machine, tiered hint policy, concept-level diagnostics, and adaptive
  practice recommendations for an introductory CS course.
- Designed an adversarial LLM evaluation harness with deterministic and
  model-based leak detection; recorded 0 solution leaks across 177 attacks and
  control turns while measuring over-refusal separately.
- Reduced modeled semester API spend 85% (about $1,003 to $146 for 30 students)
  through task-specific model routing, history windowing, and server-side turn,
  time, token, and daily quotas.
- Isolated untrusted student Python in a timeout-controlled WebAssembly worker,
  kept API credentials server-side, deleted transcripts after grading, and
  exposed only protected aggregate class analytics.

## Layout

```
data/
  problems.sample.json  the full problem set in generic wording, committed
  problems.json         (gitignored) the real course bank, not mine to publish

rung/                the system itself
  config.py          every tunable number: modes, models, pricing, limits
  proctor.py         prompt assembly, phase machine, one API call
  judge.py           two-tier leak detection
  budget.py          session limits and history windowing
  problems.py        loads whichever problem bank is present
  checks.py          runs a problem's tests against student code (in the browser)
  grader.py          rubric scoring from transcript plus measured evidence
  models.py          peewee schema: students, units, concepts, sessions, turns
  diagnostics.py     the concept profile, in one query each

evals/               how we know it works
  cases.py           the adversarial suite
  run_evals.py       harness, report, spend

tools/               utilities
  cost_model.py      cost estimates and the funding ceiling (offline)
  init_db.py         create and seed the database (offline)
  show_profile.py    print a student's diagnostic (offline)
  check_api.py       API connectivity diagnostic
  check_judge.py     validate the judge against known verdicts
  check_grader.py    validate the grader separates quality
  red_team.py        generate attacks the author never wrote

tests/
  test_offline.py    offline checks and secret scan, no API key required
  test_app.py        Flask and engine tests with the API stubbed

wsgi.py              production entry point (gunicorn)
render.yaml          Render deployment blueprint
Procfile             Elastic Beanstalk start command (same gunicorn invocation)
.ebextensions/       Elastic Beanstalk non-secret config: env vars, persistent
                     data directory, static files, hourly S3 backups
.ebignore            what `eb deploy` leaves out (local DB) and keeps (course bank)
deploy/
  cloudfront.yaml    CloudFormation for HTTPS in front of Elastic Beanstalk
vercel.json          Vercel function settings (region, timeout, bundle exclusions)
pyproject.toml       Vercel's entrypoint and dependencies (mirrors requirements.txt)
.vercelignore        what `vercel deploy` leaves out; the course bank goes up
public/static/       CSS and the Pyodide worker, served by Vercel's CDN or nginx

app/                 the web layer
  engine.py          phase, rung ceiling, budget, persistence per session
  server.py          Flask routes, rate limiting, quotas
  templates/         Jinja: picker, interview, debrief, profile, instructor
results/             (gitignored) transcripts, which are student work

DECISIONS.md         every choice made and what was rejected
SECURITY.md          key handling, student data, abuse limits
DEPLOY.md            how it gets to production on Render and what the free tier costs
AWS_DEPLOY.md        Elastic Beanstalk + CloudFront + backups, from a fresh machine
```

Four top-level packages, split by what talks to what. `rung/` is the only one
that calls the API. `tools/` and `tests/` never do, which is why both run for
free. `evals/` is deliberately separate from `rung/` rather than a subpackage:
the thing being measured and the thing doing the measuring should not import
each other.

## A note on the problem bank

The interview questions this was built for are department course material,
reused each semester, so they are not committed. `data/problems.sample.json`
covers the same problems in my own wording, so the harness runs and every eval
case resolves straight after a clone. Drop a `data/problems.json` beside it and
the loader prefers that automatically. Each entry declares its course `unit`,
instructor-authored `challenge` (`warmup`, `core`, or `stretch`, shown as the
problem's difficulty), an optional `topic` (the subject or data structure it is
listed under; without one it is listed under its unit's title), diagnostic
`concepts`, student-facing `prompt`, protected `forbidden_insight`, and `tests`
as `[input, expected]` pairs. `input` is the single argument, or the list of
arguments for a function that takes several; the function is called by the
problem's id. A problem with no tests can be practised but never shows as
solved. The loader validates that contract at startup so a malformed private
question fails before a student begins a timed session.

Run `python -m tools.init_db` after editing the bank. Seeding is idempotent and
treats the selected bank as authoritative: removed questions become inactive
without deleting historical sessions, and changed concept tags are reconciled.

## License

MIT, see [LICENSE](LICENSE).

## Attribution

The interview rubric this tool scores against is adapted from the
[Tech Interview Handbook](https://www.techinterviewhandbook.org/coding-interview-rubrics/),
licensed CC BY-SA 3.0. Any derivative of that rubric in this repository carries
the same license.
