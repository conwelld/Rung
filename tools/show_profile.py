"""
Print a student's concept diagnostic.

    python -m tools.show_profile demo
    python -m tools.show_profile demo --mode drill

This is the report the web UI will render in day 8. Building it as a CLI first
means the query is finished and correct before any HTML exists, and it stays
useful afterwards for checking what a student actually sees.
"""

import argparse
import sys

from rung.diagnostics import (
    class_overview, concept_profile, unit_progress, weakest_concepts,
)
from rung.models import Student, close_db, init_db


def bar(value, scale=4, width=20):
    if value is None:
        return ""
    filled = int(round((value / scale) * width))
    return "#" * filled


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("handle")
    parser.add_argument("--path", default="data/rung.db")
    parser.add_argument("--mode", choices=["interview", "drill"])
    args = parser.parse_args()

    init_db(args.path, create=False)

    student = Student.get_or_none(Student.handle == args.handle)
    if student is None:
        handles = [s.handle for s in Student.select()]
        sys.exit(f"No student {args.handle!r}. Known: {handles or 'none'}")

    print(f"\n  {student.handle}  ({student.sessions.count()} sessions)")

    profile = concept_profile(student, mode=args.mode)
    if not profile:
        sys.exit("\n  No turns recorded yet. Seed with: python -m tools.init_db --demo")

    print(f"\n  concept depth{' (' + args.mode + ' only)' if args.mode else ''}")
    print(f"  {'concept':<22} {'turns':>5} {'avg':>5} {'max':>4} {'deep':>5}")
    for row in profile:
        print(f"  {row['slug']:<22} {row['turns']:>5} {row['avg_rung']:>5} "
              f"{row['max_rung']:>4} {row['deep_turns']:>5}  {bar(row['avg_rung'])}")

    weak = weakest_concepts(student)
    if weak:
        print("\n  drill these next")
        for row in weak:
            print(f"    {row['slug']:<22} avg rung {row['avg_rung']}, "
                  f"{row['deep_turns']} deep hint(s) across {row['sessions']} session(s)")

    print("\n  unit progress")
    for row in unit_progress(student):
        avg = row["avg_rung"] if row["avg_rung"] is not None else "-"
        state = "not started" if row["sessions"] == 0 else f"{row['sessions']} session(s)"
        print(f"    {row['number']}. {row['title']:<22} {state:<16} avg rung {avg}")

    overview = class_overview()
    if overview:
        print("\n  class-wide (instructor view)")
        for row in overview[:5]:
            print(f"    {row['slug']:<22} avg rung {row['avg_rung']} "
                  f"across {row['students']} student(s)")

    close_db()
    return 0


if __name__ == "__main__":
    sys.exit(main())
