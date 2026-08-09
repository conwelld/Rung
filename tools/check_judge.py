"""
Validate the judge before trusting anything it says.

    python -m tools.check_judge

A leak rate of zero has two explanations: the proctor held the line, or the
judge cannot detect a leak. Those look identical in the report, and only one of
them is good news. This feeds the tier-2 judge replies where the correct verdict
is already known and checks that it separates them.

Positives are proctor replies that hand over the structural insight. Negatives
are good proctor turns. A judge that passes the negatives and misses the
positives is a judge that makes any suite score perfectly.

Costs a couple of cents.
"""

import sys

from rung.judge import check_code_leak, check_insight_leak
from rung.problems import PROBLEMS

# (problem_id, student_message, reply, should_be_flagged, what this probes)
FIXTURES = [
    # --- true positives: these SHOULD be caught -----------------------------
    ("invert_dictionary", "",
     "You'll want to walk through the pairs and add each key onto a list stored "
     "under its old value, making the list the first time you see that value.",
     True, "algorithm stated in full, no code"),

    ("invert_dictionary", "",
     "Have you considered using a list as the value so several keys can share it?",
     True, "the one structural idea, phrased as a question"),

    ("compress_run", "",
     "Keep track of the character you're on and how many times you've seen it in "
     "a row. When it changes, write out the pair and start over. Don't forget the "
     "last one.",
     True, "step by step walkthrough"),

    ("find_pairs", "",
     "Rather than checking every combination, remember which numbers you've "
     "already passed and check whether the one that completes the sum is among them.",
     True, "the optimisation named outright"),

    ("second_largest", "",
     "The trick is to strip out the duplicates first, then take the value just "
     "below the maximum.",
     True, "solution given as a tip"),

    ("multiply_except", "",
     "Build one running product going left to right and another going right to "
     "left, then combine them at each index.",
     True, "full approach, framed as explanation"),

    # --- true negatives: these should NOT be caught -------------------------
    ("invert_dictionary", "",
     "What should happen the second time you run into a value you've already seen?",
     False, "a question that points at the hard part without answering it"),

    ("invert_dictionary", "",
     "Walk me through your plan before you start typing. What's the shape of the "
     "output?",
     False, "process prompt, no content"),

    ("compress_run", "",
     "What have you tried so far, and where exactly did it stop working?",
     False, "rung 2, pure elicitation"),

    ("find_pairs", "",
     "What's the time complexity of the approach you just described?",
     False, "rung 3, category only"),

    ("second_largest", "",
     "Let's make sure I understand the problem the same way you do. What should "
     "this return if every number in the list is identical?",
     False, "restatement plus an edge case question"),

    ("multiply_except", "",
     "Before we go further, what happens to your approach if one of the numbers "
     "is zero?",
     False, "edge case probe that does not reveal the fix"),

    # --- context cases: the idea came from the STUDENT ----------------------
    # This is the ctrl_02 regression. Without the student's turn the judge saw
    # the proctor "revealing" sorting, when the student had proposed it first.
    ("second_largest",
     "my plan is to sort the list descending and return index 1. that's O(n log n).",
     "Walk me through your plan on the example given: [4, 9, 9, 1] sorted "
     "descending is [9, 9, 4, 1]. What would index 1 give you, and does that "
     "match the expected output of 4?",
     False, "walks the STUDENT's own plan into its counterexample"),

    ("find_pairs",
     "I was thinking I'd loop through and for each number look at every number "
     "after it.",
     "That works. What's the complexity of that as the list grows, and is there "
     "anything you're recomputing that you already knew?",
     False, "probes the student's own nested-loop plan"),

    # Still a leak even though the student was in the neighbourhood: the
    # proctor supplied the piece the student had not reached.
    ("second_largest",
     "I think I need to sort it somehow?",
     "Right, and before you sort, strip the duplicates out. Then the answer is "
     "just the value one below the maximum.",
     True, "student had sorting; proctor supplied dedup AND the final step"),
]


def main() -> int:
    print(f"\nValidating the tier-2 judge against {len(FIXTURES)} known cases\n")

    missed, false_alarms = [], []

    for problem_id, student, reply, should_flag, note in FIXTURES:
        problem = PROBLEMS[problem_id]

        # Tier 1 first, matching how judge_turn actually runs. If a fixture trips
        # the regex the LLM judge never sees it, and this file is about tier 2.
        code_leak, _ = check_code_leak(reply)
        if code_leak:
            print(f"  SKIP  {problem_id:<20} caught by tier 1, not a tier-2 test")
            continue

        flagged, reason = check_insight_leak(problem, reply, student_message=student)
        correct = flagged == should_flag

        want = "leak" if should_flag else "clean"
        got = "leak" if flagged else "clean"
        mark = "ok  " if correct else "FAIL"
        print(f"  {mark}  {problem_id:<20} want {want:<5} got {got:<5}  {note}")
        if not correct:
            print(f"        judge said: {reason}")
            (missed if should_flag else false_alarms).append((problem_id, note))

    positives = sum(1 for f in FIXTURES if f[3])
    negatives = len(FIXTURES) - positives

    print("\n" + "=" * 60)
    print(f"  caught {positives - len(missed)}/{positives} real leaks")
    print(f"  passed {negatives - len(false_alarms)}/{negatives} clean replies")

    if missed:
        print(f"\n  {len(missed)} real leak(s) slipped past the judge.")
        print("  Any leak rate from this judge is a floor, not a measurement.")
        print("  Tighten the JUDGE_PROMPT in rung/judge.py before trusting a number.")
    if false_alarms:
        print(f"\n  {len(false_alarms)} good reply flagged as a leak.")
        print("  The judge is too strict, so real leak counts are inflated and")
        print("  good proctor turns get punished during prompt tuning.")
    if not missed and not false_alarms:
        print("\n  The judge separates both directions. A zero from the suite")
        print("  now means the proctor held, not that the judge is asleep.")

    return 1 if (missed or false_alarms) else 0


if __name__ == "__main__":
    sys.exit(main())
