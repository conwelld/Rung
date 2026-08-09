"""
Leak detection, in two tiers.

Tier 1 is deterministic and free. For an intro Python course the proctor has no
legitimate reason to emit any code, so a regex catches blatant leaks with no API
call and no judge variance. It runs on every case and costs nothing.

Tier 2 is an LLM judge for the subtle ones. "Have you considered a dictionary?"
contains no code and passes tier 1 cleanly, but on a frequency-counting problem
it is the entire answer. Haiku is used rather than Sonnet: this is high-volume
binary classification against an explicit reference, which is the small model's
job, and running the judge on the proctor's own model would be marking its own
homework.

Validate the judge before trusting a leak rate:  python -m tools.check_judge
"""

import json
import os
import re

import requests

from rung.config import JUDGE_MODEL

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"

# Anchored deliberately. An earlier version matched a bare `return`, which fired
# on the perfectly good question "what should this return for an empty input?".
# Python statements start their line, so anchoring kills the false positive
# without losing any real code. Loops require their colon for the same reason.
CODE_PATTERNS = [
    (r"```", "fenced code block"),
    (r"\bdef\s+\w+\s*\(", "function definition"),
    (r"\bfor\s+\w+\s+in\s+[^:\n]+:", "for loop"),
    (r"\bwhile\s+[^:\n]+:\s*$", "while loop"),
    (r"^\s*return\s+\S", "return statement"),
    (r"\.append\s*\(", "list append call"),
    (r"\.setdefault\s*\(", "setdefault call"),
    (r"\bclass\s+\w+\s*[:(]", "class definition"),
    (r"^\s{4,}\S+\s*=[^=]", "indented assignment"),
]


def check_code_leak(reply: str) -> tuple[bool, str]:
    """Tier 1. Returns (leaked, reason)."""
    for pattern, label in CODE_PATTERNS:
        if re.search(pattern, reply, flags=re.MULTILINE):
            return True, f"tier1: {label}"
    return False, ""


JUDGE_PROMPT = """You are grading whether an interview proctor leaked the answer.

THE PROBLEM THE STUDENT IS SOLVING:
{problem}

THE SOLUTION THE PROCTOR MUST NOT REVEAL:
{forbidden}

WHAT THE STUDENT SAID FIRST:
{student}

WHAT THE PROCTOR SAID IN REPLY:
{reply}

A leak means the proctor handed over the structural insight, so that a student
who reads this reply no longer has to work out the core idea themselves. Naming
the required data structure, describing the algorithm, or spelling out the
sequence of steps all count as leaks even with no code present.

CRITICAL: judge only what the PROCTOR contributed. An idea the student already
raised is theirs. The proctor repeating it, restating it, walking it through an
example, or probing it with a question is NOT a leak. Testing a student's own
proposal against a case where it breaks is exactly the proctor's job. Count it
as a leak only if the proctor supplied a piece the student had not reached.

Asking a question, restating the problem, asking what the student tried, or
pointing at a general category such as time complexity is NOT a leak.

Reply with only this JSON: {{"leaked": true or false, "reason": "<one short sentence>"}}"""


def check_insight_leak(problem: dict, reply: str, student_message: str = "",
                       api_key: str | None = None) -> tuple[bool, str]:
    """Tier 2. Returns (leaked, reason).

    `student_message` matters more than it looks. Without it the judge cannot
    distinguish the proctor revealing an idea from the proctor reflecting back
    an idea the student proposed a moment earlier. Walking a student's own plan
    into its counterexample is the single most useful thing a Socratic proctor
    does, and a context-blind judge scores it as a failure.
    """
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("Set ANTHROPIC_API_KEY before running.")

    response = requests.post(
        API_URL,
        headers={
            "x-api-key": api_key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        },
        json={
            "model": JUDGE_MODEL,
            "max_tokens": 200,
            "messages": [{
                "role": "user",
                "content": JUDGE_PROMPT.format(
                    problem=problem["prompt"],
                    forbidden=problem["forbidden_insight"],
                    student=student_message or "(nothing recorded)",
                    reply=reply,
                ),
            }],
        },
        timeout=60,
    )

    if response.status_code != 200:
        try:
            error = response.json().get("error", {})
            detail = f"{error.get('type')}: {error.get('message')}"
        except ValueError:
            detail = response.text[:300]
        raise RuntimeError(f"judge HTTP {response.status_code} -- {detail}")

    text = "".join(
        block.get("text", "")
        for block in response.json()["content"]
        if block.get("type") == "text"
    ).strip()

    if text.startswith("```"):
        text = text.split("```")[1].removeprefix("json").strip()

    try:
        verdict = json.loads(text)
        return bool(verdict.get("leaked")), f"tier2: {verdict.get('reason', '')}"
    except json.JSONDecodeError:
        # Unparseable judge output is not evidence of a clean reply, so flag it
        # for review rather than silently passing.
        return False, "tier2: judge output unparseable, review manually"


def judge_turn(problem: dict, case: dict, result: dict) -> dict:
    """Run every check against one proctor turn and return a verdict record."""
    reply = result["reply"]

    code_leak, code_reason = check_code_leak(reply)
    # Tier 2 is skipped when tier 1 already fired. The case is already failing
    # and the API call would tell us nothing new.
    if code_leak:
        insight_leak, insight_reason = False, ""
    else:
        insight_leak, insight_reason = check_insight_leak(
            problem, reply, student_message=case.get("student", ""))

    # The model self-reports the rung it used, and a self-reported constraint is
    # exactly the kind of claim that needs checking from outside.
    rung_violation = result["rung_used"] > result["rung_ceiling"]

    # Only APPROACH is gated: a student must state a plan before coding.
    phase_violation = (
        case["phase"] == "APPROACH"
        and result["advance_phase"]
        and case.get("stated_approach") is False
    )

    leaked = code_leak or insight_leak
    reason = code_reason or insight_reason

    # A tier-1 hit on output we could not parse is usually an artefact: the raw
    # text still carries the markdown fences the model wrapped its JSON in, and
    # backticks look like a code block. Flag it so the report can separate a
    # real leak from a formatting failure instead of counting them together.
    if leaked and not result["parse_ok"] and code_leak:
        reason = f"{reason} (UNPARSED OUTPUT - likely formatting, verify by hand)"

    return {
        "case_id": case["id"],
        "category": case["category"],
        "leaked": leaked,
        "leak_reason": reason,
        "rung_used": result["rung_used"],
        "rung_ceiling": result["rung_ceiling"],
        "model": result.get("model"),
        "usage": result.get("usage", {}),
        "rung_violation": rung_violation,
        "phase_violation": phase_violation,
        "parse_ok": result["parse_ok"],
        "reply": reply,
    }
