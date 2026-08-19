"""
Every tunable number in the project, in one file.

Two modes exist because practice and assessment are different products with
different economics. A graded interview is the real thing and happens four
times a semester. A drill is a five-minute rep on one weak concept and happens
whenever the student wants. Running drills on the interview configuration is
what makes unlimited practice unaffordable. See DECISIONS.md.
"""

# --- models -----------------------------------------------------------------
# Sonnet holds a constraint under adversarial pressure, which is what the graded
# interview needs. Haiku is cheaper and faster, and a drill capped at rung 2
# asks much less of the model: it never has to name a structural idea.
SONNET = "claude-sonnet-5"
HAIKU = "claude-haiku-4-5"

# The judge runs on nearly every eval case and does binary classification
# against an explicit reference. That is Haiku's job. Using the proctor's own
# model would also mean it grades its own output.
JUDGE_MODEL = HAIKU

# --- pricing, USD per million tokens ----------------------------------------
# Kept here so the cost model and the eval report cannot disagree. Verify
# against the pricing page before quoting these to anyone.
PRICING = {
    SONNET: {"input": 3.00, "output": 15.00},
    HAIKU: {"input": 1.00, "output": 5.00},
}

# Prompt caching does nothing below these prefix lengths. The request still
# succeeds, it just silently caches nothing. Our system prompt is ~800 tokens,
# under the Sonnet floor and far under Haiku's, so caching is instrumented
# (we log the counters) but not enabled. Revisit if the prompt grows.
CACHE_MINIMUM_TOKENS = {SONNET: 1024, HAIKU: 4096}
ENABLE_PROMPT_CACHING = False

# --- modes ------------------------------------------------------------------
MODES = {
    "interview": {
        "model": SONNET,
        "phases": ("CLARIFY", "APPROACH", "CODE", "DEBUG"),
        "max_rung": 4,
        # Session cost grows with the SQUARE of turn count, because the whole
        # history is resent every turn. 40 turns costs 21x a 5-turn session,
        # not 8x. The turn cap is the single most effective cost control here.
        "max_turns": 20,
        "history_window": None,     # full history; the graded run needs it
        "token_budget": 80_000,
        "duration_minutes": 30,
    },
    "drill": {
        "model": HAIKU,
        # No CLARIFY: the student has already seen this concept and is doing
        # reps. No DEBUG: drills target the approach habit the rubric grades.
        "phases": ("APPROACH", "CODE"),
        "max_rung": 2,
        "max_turns": 8,
        # Only the last 6 turns are resent. Turns cost a flat amount instead of
        # a rising one, which is what makes unlimited drilling affordable.
        "history_window": 6,
        "token_budget": 12_000,
        "duration_minutes": 5,
    },
}

# --- data retention ---------------------------------------------------------
# Turn text is ALWAYS stored while a session is running. It has to be: the API
# is stateless, so the proctor's own memory of the conversation is these rows,
# and the grader reads them at the end to score the rubric.
#
# This flag controls what happens after. Off, the text is purged the moment the
# session ends and is graded, leaving rung, phase and token counts behind, which
# is everything the diagnostic needs. On, the transcripts persist, which means a
# semester of student work sitting in a file on a class server.
#
# Off is the default because the grade survives and the transcript does not, so
# the useful artefact outlives the sensitive one. Turning it on is a
# conversation with the department, not a config change.
RETAIN_TRANSCRIPTS = False

# --- fairness and abuse limits ----------------------------------------------
# Not primarily about cost at two cents a drill. Unlimited retries on one
# problem turns practice into brute forcing, which defeats the point.
DAILY_DRILL_LIMIT = 12
DAILY_INTERVIEW_LIMIT = 2

# Per-IP request ceiling for the Flask layer in phase 2.
REQUESTS_PER_MINUTE = 20


def get_mode(name: str) -> dict:
    if name not in MODES:
        raise ValueError(f"Unknown mode {name!r}. Options: {list(MODES)}")
    return MODES[name]


def price(model: str, input_tokens: int, output_tokens: int) -> float:
    """Dollar cost of one call. Cache-read tokens are billed lower, but we do
    not use caching yet, so this stays deliberately simple."""
    rates = PRICING[model]
    return input_tokens / 1e6 * rates["input"] + output_tokens / 1e6 * rates["output"]
