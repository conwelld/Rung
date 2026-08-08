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

43 cases: 38 adversarial across ten categories, 5 controls.

| Metric | interview | drill |
|---|---|---|
| Answer leakage | _run it_ | |
| Over-refusal (controls) | _run it_ | |
| Rung ceiling violations | _run it_ | |
| Phase gate violations | _run it_ | |
| Spend per run | _run it_ | |

Leak rate alone is not the score. A proctor that stonewalls every question leaks
nothing and teaches nothing, so over-refusal sits directly underneath it.

**Attack categories:** direct request, authority claim, false completion, debug
vector, incremental extraction, phase skip, emotional pressure, format reframe,
hypothetical, test fishing.

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
