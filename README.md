# Rung

A Socratic proctor for mock coding interviews. It runs a student through a timed
technical interview and never gives away the answer, working from a fixed
four-level hint ladder instead of open-ended help. How far down that ladder a
student needs to go, tagged by concept, becomes their diagnostic.

Built for the CSC coding interviews at Berea College, where interviews are
already graded on communication, problem solving, technical competency, and
debugging.

This repository is the proctor and the evaluation harness. There is no UI yet,
on purpose: the interesting claim this project makes is a measured one, so the
measurement came first.

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
| Cost per session | ~$0.22 | ~$0.02 |

A drill is a rep on one concept the diagnostic flagged. A graded interview is
the real thing, four times a semester. Running drills on interview settings is
what makes unlimited practice unaffordable, and short reps on the approach phase
are better practice anyway than twenty full-length mock interviews.

## Cost

```
python cost_model.py
```

No API key needed. Every assumption is at the top of the file, so the numbers
are re-derivable rather than asserted.

For a class of 30 over a 15-week semester, with three drills a day and four
graded interviews each:

| | Sessions | Cost |
|---|---|---|
| Drills | 6,750 | $128 |
| Graded interviews | 120 | $26 |
| **Total** | | **$155** (~$5.15 per student) |

Running every one of those drills on interview settings instead: **$1,505**.
The mode split is a 90% saving.

The number to take to a department is the enforced ceiling, not the average.
With every student maxing the daily cap, 30 students is $539 a semester.
`DAILY_DRILL_LIMIT` in `config.py` is the lever: halve it and the ceiling halves.

Why turn count dominates: the API is stateless, so the whole conversation is
resent every turn. Session cost grows with the square of turn count.

| Turns | Input tokens | Cost | Per turn |
|---|---|---|---|
| 5 | 7,000 | $0.032 | $0.0065 |
| 20 | 58,000 | $0.219 | $0.0110 |
| 40 | 196,000 | $0.678 | $0.0169 |

Eight times the turns, twenty-one times the cost. That is why the turn cap
exists and why drill mode windows history.

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

Every attack is text in a chat turn. Nothing here tests a real student over 30
minutes, a browser client, or code execution. The claim is bounded: across 177
adversarial turns on two models, the proctor did not hand over a solution.

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

evals/               how we know it works
  cases.py           the adversarial suite
  run_evals.py       harness, report, spend

tools/               offline utilities, nothing here calls the API
  cost_model.py      cost estimates and the funding ceiling

tests/
  test_offline.py    offline checks and secret scan, no API key required

app/                 (day 5) Flask routes, peewee models, templates, static
results/             (gitignored) transcripts, which are student work

DECISIONS.md         every choice made and what was rejected
SECURITY.md          key handling, student data, abuse limits
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
the loader prefers that automatically.

## License

MIT, see [LICENSE](LICENSE).

## Attribution

The interview rubric this tool scores against is adapted from the
[Tech Interview Handbook](https://www.techinterviewhandbook.org/coding-interview-rubrics/),
licensed CC BY-SA 3.0. Any derivative of that rubric in this repository carries
the same license.
