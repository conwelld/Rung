"""
Cost model. No API key, no network, no spend.

    python cost_model.py

Every number here is an estimate from a token model, not a measurement. Run the
eval suite and read the real usage from `results/baseline.json` before quoting
any of it to a department. The point of this file is that the estimate is
reproducible and the assumptions are visible, not that it is exact.

The central fact: the API is stateless, so the whole conversation is resent on
every turn. Session cost therefore grows with the SQUARE of turn count. That is
why drill mode caps turns and windows history rather than just using a cheaper
model.
"""

from rung.config import DAILY_DRILL_LIMIT, MODES, price

# Assumptions, all in tokens. Change these and rerun rather than editing
# numbers further down.
SYSTEM_PROMPT = 800      # measured: len(build_system_prompt(...)) / 4
TURN_GROWTH = 200        # student message plus proctor reply, per exchange
REPLY = 150              # proctor output per turn


def session_tokens(turns: int, window: int | None) -> tuple[int, int]:
    """Input and output tokens for one session.

    With no window, turn N resends N exchanges, so input is a sum that grows
    quadratically. With a window of W, turn N resends at most W, so input is
    close to linear. This one line is the whole cost argument.
    """
    resent = [TURN_GROWTH * min(t, window if window else t) for t in range(1, turns + 1)]
    return sum(SYSTEM_PROMPT + r for r in resent), REPLY * turns


def session_cost(mode_name: str) -> float:
    mode = MODES[mode_name]
    inp, out = session_tokens(mode["max_turns"], mode["history_window"])
    return price(mode["model"], inp, out)


def why_turns_matter():
    print("Why the turn cap matters (interview settings, no window)")
    print(f"  {'turns':>6}  {'input tok':>10}  {'cost':>8}  {'per turn':>9}")
    for t in (5, 10, 20, 40):
        inp, out = session_tokens(t, None)
        c = price(MODES["interview"]["model"], inp, out)
        print(f"  {t:>6}  {inp:>10,}  {c:>8.3f}  {c/t:>9.4f}")
    print("  Eight times the turns costs twenty-one times as much, because")
    print("  every turn pays to resend all the turns before it.\n")


def compare_modes():
    print("What one session costs in each mode")
    for name in MODES:
        mode = MODES[name]
        inp, out = session_tokens(mode["max_turns"], mode["history_window"])
        c = price(mode["model"], inp, out)
        window = mode["history_window"] or "full"
        print(f"  {name:<10} {mode['model']:<20} {mode['max_turns']:>2} turns, "
              f"window {str(window):<4}  {inp:>7,} in  ${c:.4f}")
    ratio = session_cost("interview") / session_cost("drill")
    print(f"  A drill costs about 1/{ratio:.0f} of a graded interview.\n")


def semester(students: int, weeks: int = 15, drills_per_day: int = 3,
             days_per_week: int = 5, graded_interviews: int = 4):
    """The number a department actually needs to see."""
    drill = session_cost("drill")
    interview = session_cost("interview")

    n_drills = students * drills_per_day * days_per_week * weeks
    n_graded = students * graded_interviews
    total = n_drills * drill + n_graded * interview

    print(f"Semester estimate: {students} students, {weeks} weeks")
    print(f"  {n_drills:,} drills @ ${drill:.4f}      ${n_drills*drill:>9,.2f}")
    print(f"  {n_graded:,} graded @ ${interview:.4f}       ${n_graded*interview:>9,.2f}")
    print(f"  {'total':<32} ${total:>9,.2f}")
    print(f"  {'per student':<32} ${total/students:>9.2f}")

    # The counterfactual that justifies the two-mode design existing at all.
    naive = (n_drills + n_graded) * interview
    print(f"\n  If every drill ran on interview settings: ${naive:,.2f}")
    print(f"  The two-mode split saves ${naive-total:,.2f} ({100*(1-total/naive):.0f}%).")

    # Worst case with the per-student daily cap actually enforced.
    capped = students * DAILY_DRILL_LIMIT * days_per_week * weeks * drill \
        + n_graded * interview
    print(f"\n  Worst case, every student maxes the {DAILY_DRILL_LIMIT}/day cap: "
          f"${capped:,.2f}")
    print("  That is the number to put in a funding request: the ceiling,")
    print("  not the average, because the ceiling is what is enforced.\n")


if __name__ == "__main__":
    why_turns_matter()
    compare_modes()
    for size in (30, 60, 120):
        semester(size)
