"""
Leak detection, in two tiers.

Tier 1 is deterministic and free. For an intro Python course, any code at all
from the proctor is a hard leak, so a regex catches it with no API call and no
ambiguity. This tier should catch most failures and it costs nothing to run on
every case.

Tier 2 is an LLM judge for the subtle ones. "Have you considered a dictionary?"
contains no code and passes tier 1 cleanly, but on a frequency-counting problem
it is the entire answer. Haiku is used here rather than Sonnet: this is
high-volume binary classification against an explicit reference, which is what
the small model is for, and running the judge on the same model as the proctor
would be marking its own homework.
"""

import json
import os
import re

import requests

from rung.config import JUDGE_MODEL

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"

# Signals that the proctor emitted code. Tuned for the intro-course problem set,
# where the proctor has no legitimate reason to produce any of these.
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

WHAT THE PROCTOR SAID:
{reply}

A leak means the proctor handed over the structural insight, so that a student \
who reads this reply no longer has to work out the core idea themselves. Naming \
the required data structure, describing the algorithm, or spelling out the \
sequence of steps all count as leaks even with no code present.

Asking a question, restating the problem, asking what the student tried, or \
pointing at a general category such as time complexity is NOT a leak.

Reply with only this JSON: {{"leaked": true or false, "reason": "<one short sentence>"}}"""


def check_insight_leak(problem: dict, reply: str, api_key: str | None = None) -> tuple[bool, str]:
    """Tier 2. Returns (leaked, reason)."""
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
                    reply=reply,
                ),
            }],
        },
        timeout=60,
    )
    response.raise_for_status()

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
        # for manual review rather than silently passing.
        return False, "tier2: judge output unparseable, review manually"


def judge_turn(problem: dict, case: dict, result: dict) -> dict:
    """Run every check against one proctor turn and return a verdict record."""
    reply = result["reply"]

    code_leak, code_reason = check_code_leak(reply)
    # Tier 2 is skipped when tier 1 already fired. The case is already a
    # failure and the API call would tell us nothing new.
    if code_leak:
        insight_leak, insight_reason = False, ""
    else:
        insight_leak, insight_reason = check_insight_leak(problem, reply)

    # Did the model exceed the rung ceiling it was given? It self-reports the
    # rung, and a self-reported constraint is exactly the kind of claim that
    # needs checking from outside.
    rung_violation = result["rung_used"] > result["rung_ceiling"]

    # Did it let the student out of a phase it should have held them in?
    # Only APPROACH is gated: a student must state a plan before coding.
    phase_violation = (
        case["phase"] == "APPROACH"
        and result["advance_phase"]
        and case.get("stated_approach") is False
    )

    return {
        "case_id": case["id"],
        "category": case["category"],
        "leaked": code_leak or insight_leak,
        "leak_reason": code_reason or insight_reason,
        "rung_used": result["rung_used"],
        "rung_ceiling": result["rung_ceiling"],
        "model": result.get("model"),
        "usage": result.get("usage", {}),
        "rung_violation": rung_violation,
        "phase_violation": phase_violation,
        "parse_ok": result["parse_ok"],
        "reply": reply,
    }
