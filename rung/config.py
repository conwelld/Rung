"""
Every tunable number in the project, in one file.

Two modes exist because practice and assessment are different products with
different economics. A graded interview is the real thing and happens four
times a semester. A drill is a five-minute rep on one weak concept and happens
whenever the student wants. Running drills on the interview configuration is
what makes unlimited practice unaffordable. See DECISIONS.md.
"""

import os

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
    # Claude Sonnet 5 launched at $2/$10 per MTok. Keep this explicit because
    # the cost model is part of the product claim, not decorative telemetry.
    SONNET: {"input": 2.00, "output": 10.00},
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

# A public link has no classroom roster behind it, so nothing stops a script
# from rotating through fresh handles to dodge DAILY_DRILL_LIMIT one handle at
# a time -- Student.get_or_create asks for nothing but a string. This caps new
# session starts per IP address instead, independent of handle. Not foolproof
# (shared IPs, VPNs), but it turns "unlimited" into "unlimited divided by five
# per hour," which is the actual point: the Anthropic spend cap in the console
# is the backstop, this just keeps it from ever being tested.
STARTS_PER_HOUR_PER_IP = 5


def _count_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    if not raw.isdigit():
        raise RuntimeError(f"{name} must be a non-negative integer")
    return int(raw)


# Site-wide ceilings on sessions started per day, counted in the database.
# Every limit above can be stepped around by someone patient enough: the daily
# caps are per handle and handles are free, and the per-IP cap counts in memory,
# per server process, so a host running several copies (Vercel) or a VPN
# weakens it. These cannot be: however many names or addresses are used, the
# whole site starts at most this many sessions a day, which turns the worst
# case into a number. Defaults fit a class of 30 at the per-student caps;
# raise them for a bigger class. At current prices the defaults cap a day at
# roughly 60 x $0.15 + 400 x $0.02, under $20 even if all of it is abuse.
GLOBAL_DAILY_INTERVIEWS = _count_env("RUNG_GLOBAL_DAILY_INTERVIEWS", 60)
GLOBAL_DAILY_DRILLS = _count_env("RUNG_GLOBAL_DAILY_DRILLS", 400)

# A code the instructor gives the class. When set, starting a session needs it
# once per browser, so the public internet cannot spend the API budget at all.
# The value is compared server-side and never rendered into a page.
ACCESS_CODE = os.environ.get("RUNG_ACCESS_CODE", "").strip()

# The course calendar is the difficulty gate. Set this to the latest unit the
# class has reached; later questions remain visible as a roadmap but cannot be
# started. Leaving it unset makes every sample problem available for the public
# demo and keeps local setup frictionless.
_current_unit = os.environ.get("RUNG_CURRENT_UNIT", "").strip()
try:
    CURRENT_UNIT = int(_current_unit) if _current_unit else None
except ValueError as exc:
    raise RuntimeError("RUNG_CURRENT_UNIT must be a positive integer") from exc
if CURRENT_UNIT is not None and CURRENT_UNIT < 1:
    raise RuntimeError("RUNG_CURRENT_UNIT must be a positive integer")

# Aggregate class analytics are disabled unless the deployment supplies a
# separate instructor code. The value is never rendered into a page or logged.
INSTRUCTOR_CODE = os.environ.get("RUNG_INSTRUCTOR_CODE", "").strip()

# Used only for absolute social-preview metadata. Never derive this from
# forwarded request headers: a deployment supplies the origin it owns.
PUBLIC_ORIGIN = os.environ.get("RUNG_PUBLIC_ORIGIN", "").strip().rstrip("/")
if PUBLIC_ORIGIN and not PUBLIC_ORIGIN.startswith(("https://", "http://")):
    raise RuntimeError("RUNG_PUBLIC_ORIGIN must be an absolute http(s) origin")

# How many reverse proxies sit in front of the app, each appending to
# X-Forwarded-For. Behind CloudFront and Elastic Beanstalk's nginx that is 2,
# and without it request.remote_addr is nginx (127.0.0.1) for every student,
# which turns STARTS_PER_HOUR_PER_IP into one cap for the whole class. Too high
# a number is worse than zero: it would trust addresses the client wrote
# itself. Zero, the default, trusts nothing and is right for local runs.
_proxy_hops = os.environ.get("RUNG_PROXY_HOPS", "").strip() or "0"
if not _proxy_hops.isdigit():
    raise RuntimeError("RUNG_PROXY_HOPS must be a non-negative integer")
PROXY_HOPS = int(_proxy_hops)

# A value CloudFront adds to every request it forwards. When set, requests
# without it are refused, so the environment's own *.elasticbeanstalk.com
# address cannot be used to go around CloudFront: plain HTTP, and a place to
# forge X-Forwarded-For past the per-IP start cap. Unset, nothing is checked.
ORIGIN_SECRET = os.environ.get("RUNG_ORIGIN_SECRET", "").strip()
ORIGIN_HEADER = "X-Rung-Origin"

# Where "today" ends for the daily caps. Hosts disagree about their own clock
# (Vercel is UTC, which ends the day at 8pm in Berea for half the year), so the
# course says which midnight counts.
COURSE_TIMEZONE = os.environ.get("RUNG_TIMEZONE", "").strip() or "America/New_York"
try:
    from zoneinfo import ZoneInfo
    ZoneInfo(COURSE_TIMEZONE)
except Exception as exc:  # noqa: BLE001 - ZoneInfoNotFoundError, ValueError
    raise RuntimeError(f"RUNG_TIMEZONE {COURSE_TIMEZONE!r} is not a known timezone") from exc


def get_mode(name: str) -> dict:
    if name not in MODES:
        raise ValueError(f"Unknown mode {name!r}. Options: {list(MODES)}")
    return MODES[name]


def price(model: str, input_tokens: int, output_tokens: int) -> float:
    """Dollar cost of one call. Cache-read tokens are billed lower, but we do
    not use caching yet, so this stays deliberately simple."""
    rates = PRICING[model]
    return input_tokens / 1e6 * rates["input"] + output_tokens / 1e6 * rates["output"]
