"""
Rubric grading.

The student self-scores at the debrief, which is weak evidence about them and
useful evidence about their self-assessment. This produces the other half: a
score from the proctor, on the department's four dimensions, so the two can sit
side by side. The gap between them is often the most useful thing on the page.

Two things make this more than a model being asked for an opinion.

**Behavioural evidence is supplied as fact.** Hint depth, turns per phase, how
far the student got, and how long it took are computed from stored turns and
handed to the grader. The department rubric names "did not require any major
hints" as an explicit problem-solving signal, so the tool already measures one
of the four dimensions directly and does not have to ask a model to guess at it.

**Refusing to score is allowed.** Technical competency cannot be judged without
code. When no code is submitted the grader returns null for that dimension and
says so, rather than inventing a number from how the student described their
plan. A confident score with nothing behind it is worse than an absent one.

Validate the grader before trusting it:  python -m tools.check_grader
"""

import json
import os

import requests

from rung.config import JUDGE_MODEL

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"

# The same model as the leak judge, and for the same reason: this is structured
# assessment against an explicit reference, not open-ended reasoning. Using the
# proctor's own model would also mean it grades a conversation it just had, and
# a model is a soft marker of its own work.
GRADER_MODEL = JUDGE_MODEL

SCORE_LABELS = {3: "strong hire", 2: "leaning hire", 1: "leaning no hire", 0: "strong no hire"}

GRADER_PROMPT = """You are grading a mock technical interview for an introductory \
computer science course, using the department's rubric. Grade the STUDENT. The \
proctor is not being assessed.

THE PROBLEM:
{problem}

MEASURED EVIDENCE (computed from the session, treat as fact):
{evidence}

THE TRANSCRIPT:
{transcript}

{code_section}

THE RUBRIC. Score each dimension 0 to 3:
  3 strong hire, 2 leaning hire, 1 leaning no hire, 0 strong no hire

communication
  Asks appropriate clarifying questions. Communicates approach, rationale and \
tradeoffs. Keeps talking while working. Organised and clear.
  3: thorough, organised and clear throughout; easy to follow their thinking.
  2: sufficient and clear, but needed follow-up questions to be understood.
  1: insufficient, disorganised, or jumped ahead without explaining.
  0: could not communicate with any clarity, or went silent when addressed.

problem_solving
  Understands the problem quickly. Systematic and logical. Reaches a working \
approach. Did not need major hints. Advanced: multiple solutions with tradeoffs.
  The hint depth in the evidence above is direct measurement of this dimension. \
Reaching rung 4 means the structural idea had to be named for them; staying at \
rung 0 or 1 means they found it themselves. Weight it accordingly.
  3: got there with room to spare, and compared approaches or discussed tradeoffs.
  2: got there, but without time or space for the advanced signals.
  1: some progress, but did not arrive at a working approach.
  0: could not solve it, or worked without explaining any of their thinking.

technical_competency
  Translates the approach into working code. Clean, correct, readable.
  Score this ONLY if code is present above. If no code was submitted, return \
null. Do not infer this from how well they described a plan.

debugging
  Verifies correctness systematically. Finds and fixes their own bugs. Thinks of \
typical cases and corner cases.
  If the session ended before any debugging happened, return null.

CALIBRATION. Be honest rather than encouraging. In a real intro course most \
sessions land at 1 or 2. A 3 means they did the advanced signals, not that they \
did fine. A 0 means the signal was essentially absent. A grader that gives \
everyone a 2 tells the student nothing and is worse than no grader. If the \
evidence for a dimension is thin, say so in the note and score low rather than \
splitting the difference.

Write each note to the student, in second person, one or two sentences. Point at \
something specific they did. No praise sandwiches.

Reply with only this JSON:
{{"communication": {{"score": <0-3 or null>, "note": "..."}},
  "problem_solving": {{"score": <0-3 or null>, "note": "..."}},
  "technical_competency": {{"score": <0-3 or null>, "note": "..."}},
  "debugging": {{"score": <0-3 or null>, "note": "..."}},
  "overall": "<two sentences: the single most useful thing to work on next>"}}"""


def build_evidence(session, turns) -> str:
    """Behavioural facts, computed rather than judged.

    This is what separates the grade from a model's impression of a chat log.
    Hint depth in particular is named in the rubric as a problem-solving signal,
    so it is measurement rather than opinion.
    """
    if not turns:
        return "No turns recorded."

    rungs = [t.rung_used for t in turns]
    by_phase = {}
    for turn in turns:
        by_phase.setdefault(turn.phase, []).append(turn.rung_used)

    lines = [
        f"- Mode: {session.mode} (hint ladder capped at rung "
        f"{max(t.rung_ceiling for t in turns)})",
        f"- Turns taken: {len(turns)}",
        f"- Deepest hint needed: rung {max(rungs)} of 4",
        f"- Average hint depth: {sum(rungs) / len(rungs):.2f}",
        f"- Turns needing a deep hint (rung 3 or 4): {sum(1 for r in rungs if r >= 3)}",
        f"- Furthest phase reached: {session.phase_reached}",
        f"- Session ended because: {session.stopped_because or 'still open'}",
    ]
    for phase, phase_rungs in by_phase.items():
        lines.append(f"- {phase}: {len(phase_rungs)} turn(s), "
                     f"deepest hint rung {max(phase_rungs)}")
    if session.duration_minutes():
        lines.append(f"- Duration: {session.duration_minutes():.1f} minutes")
    return "\n".join(lines)


def build_transcript(turns) -> str:
    """Render stored turns for the grader.

    Text is kept only for the life of the session, so this is called at the
    moment the session ends and before the text is purged. See app/engine.py.
    """
    parts = []
    for turn in turns:
        if not turn.student_text:
            continue
        parts.append(f"[{turn.phase}] Student: {turn.student_text}")
        if turn.proctor_text:
            parts.append(f"[{turn.phase}] Proctor: {turn.proctor_text}")
    return "\n\n".join(parts) if parts else "(no transcript available)"


def grade_session(session, turns, code: str = "", api_key: str | None = None) -> dict:
    """Return {dimension: {score, note}} plus an overall line.

    Raises RuntimeError on an API failure. The caller decides whether a session
    without a grade is still a valid session, and in this app it is: the debrief
    renders with self-scores alone.
    """
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("Set ANTHROPIC_API_KEY before grading.")

    code_section = (
        f"THE CODE THE STUDENT WROTE:\n{code.strip()}"
        if code.strip()
        else "NO CODE WAS SUBMITTED. Return null for technical_competency."
    )

    prompt = GRADER_PROMPT.format(
        problem=session.problem.prompt,
        evidence=build_evidence(session, turns),
        transcript=build_transcript(turns),
        code_section=code_section,
    )

    response = requests.post(
        API_URL,
        headers={"x-api-key": api_key, "anthropic-version": API_VERSION,
                 "content-type": "application/json"},
        json={"model": GRADER_MODEL, "max_tokens": 1200,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=90,
    )
    if response.status_code != 200:
        try:
            error = response.json().get("error", {})
            detail = f"{error.get('type')}: {error.get('message')}"
        except ValueError:
            detail = response.text[:300]
        raise RuntimeError(f"grader HTTP {response.status_code} -- {detail}")

    text = "".join(b.get("text", "") for b in response.json()["content"]
                   if b.get("type") == "text").strip()
    return _parse_grades(text)


def _parse_grades(text: str) -> dict:
    """Parse the grader's JSON, clamping anything out of range.

    A grader that returns 7 for communication is a bug that should surface as a
    clamped 3 rather than a crash on the debrief page or an out-of-range value
    written into the database.
    """
    cleaned = text.strip()
    if "```" in cleaned:
        for part in cleaned.split("```")[1:]:
            candidate = part.strip().removeprefix("json").strip()
            if candidate.startswith("{"):
                cleaned = candidate
                break
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1:
        raise RuntimeError("grader returned no JSON object")

    raw = json.loads(cleaned[start:end + 1])
    grades = {"overall": str(raw.get("overall", "")).strip()}

    for dimension in ("communication", "problem_solving",
                      "technical_competency", "debugging"):
        entry = raw.get(dimension) or {}
        score = entry.get("score")
        if score is not None:
            try:
                score = max(0, min(3, int(score)))
            except (TypeError, ValueError):
                score = None
        grades[dimension] = {"score": score, "note": str(entry.get("note", "")).strip()}

    return grades
