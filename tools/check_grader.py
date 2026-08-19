"""
Validate the grader before trusting a grade.

    python -m tools.check_grader

The failure mode for an LLM grader is not being wrong, it is being uniformly
encouraging. A grader that hands out "leaning hire" to everyone produces a page
that looks like feedback and contains none, and it will pass any test that only
checks the endpoint returned something.

So this runs three constructed sessions with known quality through the real
grader and checks that the scores separate. A strong session should outscore a
weak one on every dimension. If it does not, the grade on the debrief page is
decoration.

It also checks the refusal path: with no code submitted, technical competency
must come back null rather than a number invented from how the student
described their plan.

Costs a few cents.
"""

import sys

from rung.grader import SCORE_LABELS, grade_session
from rung.models import (Problem, Session, Student, Turn, Unit, close_db,
                         init_db)

# --- three sessions, built to be obviously different ------------------------

STRONG = [
    ("CLARIFY", 0,
     "Before I start: should I count non-ASCII letters like accented characters, "
     "and what should an empty string return?",
     "Assume ASCII, and decide the empty case yourself and say why."),
    ("APPROACH", 0,
     "Empty should return 0, since there are zero letters. My plan is one pass "
     "over the string, testing each character and incrementing a counter when it "
     "is alphabetic. That is O(n) time and O(1) extra space. The alternative is "
     "building a filtered list and taking its length, which is also O(n) but "
     "allocates a list I do not need.",
     "Good. Start coding, and keep talking as you go."),
    ("CODE", 0,
     "Writing the loop now. Counter starts at zero, checking each character, "
     "incrementing on a match. I am using the built-in alphabetic test rather "
     "than comparing against a hardcoded set, so it stays readable.",
     "How will you convince yourself it is right?"),
    ("DEBUG", 0,
     "Empty string gives 0. All digits gives 0. Mixed gives the letter count. I "
     "would also check a string of only spaces, and one where punctuation sits "
     "between letters, since that is where an off-by-one would show up.",
     "Anything you would change if the input could be very large?"),
    ("DEBUG", 1,
     "Not really. It is already a single pass with constant extra space, so there "
     "is nothing to reuse or precompute.",
     "Good."),
]

WEAK = [
    ("APPROACH", 2, "idk", "What does the problem ask you to return?"),
    ("APPROACH", 3, "count something?", "Count what, specifically?"),
    ("CODE", 4, "im stuck", "What have you written so far?"),
    ("CODE", 4, "nothing", "Try writing the function signature."),
    ("CODE", 4, "ok now what", "What should happen to each character?"),
]

MIDDLING = [
    ("CLARIFY", 0, "Do spaces count?", "No. What is your plan?"),
    ("APPROACH", 1,
     "I will loop through and count the letters using a counter variable.",
     "What is the complexity, and what happens on an empty string?"),
    ("APPROACH", 2, "O(n) I think. Empty gives 0.", "Good enough, start coding."),
    ("CODE", 2, "Writing the loop.", "How are you testing each character?"),
    ("CODE", 3,
     "I was comparing against a list of letters but that is long. Is there a "
     "built-in?",
     "What does Python offer for classifying a character?"),
    ("DEBUG", 3, "It works on the example.", "What other inputs would you try?"),
]

STRONG_CODE = '''def count_letters(word):
    """Return the number of alphabetic characters in word."""
    total = 0
    for character in word:
        if character.isalpha():
            total += 1
    return total
'''

WEAK_CODE = '''def count_letters(w):
    c=0
    for i in range(0,len(w)):
        if w[i]!=" " and w[i]!="1" and w[i]!="2":
            c=c+1
    return c
'''


def build(handle, script, mode="interview"):
    student, _ = Student.get_or_create(handle=handle)
    problem = Problem.get(Problem.slug == "count_letters")
    session = Session.create(
        student=student, problem=problem, mode=mode, model="test",
        phase_reached=script[-1][0], stopped_because="completed",
    )
    for i, (phase, rung, said, replied) in enumerate(script, start=1):
        Turn.create(session=session, ordinal=i, phase=phase, rung_used=rung,
                    rung_ceiling=4, student_text=said, proctor_text=replied,
                    input_tokens=900, output_tokens=120)
    return session


def show(label, grades):
    print(f"\n  {label}")
    for dimension in ("communication", "problem_solving",
                      "technical_competency", "debugging"):
        entry = grades[dimension]
        score = entry["score"]
        verdict = SCORE_LABELS.get(score, "not scored") if score is not None else "not scored"
        print(f"    {dimension:<22} {str(score):>4}  {verdict}")
        if entry["note"]:
            print(f"      {entry['note'][:96]}")


def main():
    init_db(":memory:")
    unit = Unit.create(number=1, title="Functions")
    Problem.create(
        slug="count_letters", title="count_letters", unit=unit,
        prompt=("Write a function count_letters(word) that returns how many "
                "alphabetic characters the string contains."),
        forbidden_insight="Loop over the characters and use the alphabetic test.")

    failures = []

    try:
        strong = grade_session(build("strong", STRONG), list(Turn.select()
                               .join(Session).where(Session.student == Student.get(
                                   Student.handle == "strong"))), code=STRONG_CODE)
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"Grading failed: {exc}")

    weak_session = build("weak", WEAK)
    weak = grade_session(weak_session, list(weak_session.turns), code=WEAK_CODE)

    mid_session = build("middling", MIDDLING)
    middling = grade_session(mid_session, list(mid_session.turns), code="")

    show("strong session (deep understanding, clean code)", strong)
    show("middling session (got there with help)", middling)
    show("weak session (stuck throughout, bad code)", weak)

    print("\n" + "=" * 60)

    # 1. Does it separate at all?
    for dimension in ("communication", "problem_solving", "technical_competency"):
        s, w = strong[dimension]["score"], weak[dimension]["score"]
        if s is None or w is None:
            continue
        ok = s > w
        print(f"  {'ok  ' if ok else 'FAIL'} strong beats weak on {dimension} ({s} vs {w})")
        if not ok:
            failures.append(dimension)

    # 2. Does it use the full range, or hedge toward the middle?
    scores = [g[d]["score"] for g in (strong, weak, middling)
              for d in ("communication", "problem_solving", "debugging")
              if g[d]["score"] is not None]
    spread = max(scores) - min(scores) if scores else 0
    ok = spread >= 2
    print(f"  {'ok  ' if ok else 'FAIL'} uses a real range (spread {spread} across {len(scores)} scores)")
    if not ok:
        failures.append("range")

    # 3. Is the weak session actually marked down, or gently encouraged?
    weak_scores = [weak[d]["score"] for d in weak if d != "overall"
                   and weak[d]["score"] is not None]
    ok = weak_scores and max(weak_scores) <= 1
    print(f"  {'ok  ' if ok else 'FAIL'} weak session scores low across the board "
          f"(max {max(weak_scores) if weak_scores else '-'})")
    if not ok:
        failures.append("sycophancy")

    # 4. Does it refuse when it should?
    ok = middling["technical_competency"]["score"] is None
    print(f"  {'ok  ' if ok else 'FAIL'} refuses technical competency with no code "
          f"(got {middling['technical_competency']['score']})")
    if not ok:
        failures.append("refusal")

    # 5. Are the notes specific, or generic encouragement?
    generic = [d for d in ("communication", "problem_solving")
               if len(strong[d]["note"]) < 40]
    ok = not generic
    print(f"  {'ok  ' if ok else 'FAIL'} notes are substantive")
    if not ok:
        failures.append("notes")

    close_db()

    if failures:
        print(f"\n  {len(failures)} problem(s). A grade from this grader is not")
        print("  trustworthy yet. Tighten the calibration section of GRADER_PROMPT.")
        return 1
    print("\n  The grader separates quality and refuses when evidence is missing.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
