"""
Create and seed the database from the problem bank.

    python -m tools.init_db                 # create data/rung.db and seed it
    python -m tools.init_db --demo          # also add a fake student with sessions
    python -m tools.init_db --path :memory: --demo   # try it without writing a file

Idempotent: rerunning updates existing rows rather than duplicating them, so it
is safe to run after editing the problem bank. Units and concepts are derived
from the bank rather than maintained separately, so there is one source of truth.
"""

import argparse
import datetime
import os
import random
import sys

from rung.models import (
    ALL_TABLES, Concept, Problem, ProblemConcept, RubricScore, Session,
    Student, Turn, Unit, add_missing_columns, close_db, database, describe_db,
    init_db,
)
from rung.problems import BANK_SOURCE, PROBLEMS

# The bank stores a unit number per problem. Titles live here because the JSON
# repeats the number on every problem and a title belongs in one place.
UNIT_TITLES = {
    1: ("Functions", "through week 4"),
    2: ("Lists and strings", "weeks 5-9"),
    3: ("Dictionaries", "weeks 10-14"),
    4: ("Classes", "week 10 to end"),
}


def seed_units():
    numbers = sorted({p["unit"] for p in PROBLEMS.values()})
    for number in numbers:
        title, weeks = UNIT_TITLES.get(number, (f"Unit {number}", None))
        Unit.insert(number=number, title=title, week_range=weeks).on_conflict(
            conflict_target=[Unit.number],
            update={Unit.title: title, Unit.week_range: weeks},
        ).execute()
    return len(numbers)


def seed_concepts():
    slugs = sorted({c for p in PROBLEMS.values() for c in p["concepts"]})
    for slug in slugs:
        title = slug.replace("-", " ")
        Concept.insert(slug=slug, title=title).on_conflict(
            conflict_target=[Concept.slug], update={Concept.title: title},
        ).execute()
    return len(slugs)


def seed_problems():
    # The selected bank is authoritative. Switching from the public sample to a
    # private course bank must not leave sample-only rows visible, while old
    # session foreign keys still require those rows to remain in the database.
    active_slugs = list(PROBLEMS)
    Problem.update(active=False).where(
        Problem.slug.not_in(active_slugs)).execute()

    for slug, data in PROBLEMS.items():
        unit = Unit.get(Unit.number == data["unit"])
        Problem.insert(
            slug=slug, title=data["title"], unit=unit,
            prompt=data["prompt"], forbidden_insight=data["forbidden_insight"],
            active=True,
        ).on_conflict(
            conflict_target=[Problem.slug],
            update={
                Problem.title: data["title"], Problem.unit: unit,
                Problem.prompt: data["prompt"],
                Problem.forbidden_insight: data["forbidden_insight"],
                Problem.active: True,
            },
        ).execute()

        problem = Problem.get(Problem.slug == slug)
        wanted_concepts = [
            Concept.get(Concept.slug == concept_slug)
            for concept_slug in data["concepts"]
        ]
        ProblemConcept.delete().where(
            (ProblemConcept.problem == problem)
            & ProblemConcept.concept.not_in([c.id for c in wanted_concepts])
        ).execute()
        for concept in wanted_concepts:
            # The unique index on (problem, concept) turns a reseed into a
            # no-op instead of doubling every count in the diagnostic.
            ProblemConcept.insert(problem=problem, concept=concept).on_conflict_ignore().execute()

    return len(PROBLEMS)


def seed_demo_student(handle="demo", seed=7):
    """A student with a deliberately uneven profile.

    Not random sessions over random problems: the earlier version of this drew
    modes randomly and drill caps at rung 2, so every concept came out with a
    max of 2 and no deep hints at all. Seed data whose only job is to show the
    diagnostic has a shape must actually have one, so the walk is explicit.

    This student is comfortable with strings and functions, and struggles with
    dictionaries and mutation. Interview sessions are where the deep rungs
    appear, because drill cannot reach past rung 2 by design.
    """
    rng = random.Random(seed)
    student, _ = Student.get_or_create(handle=handle)

    weak = {"dict-construction", "dict-of-lists", "nested-dicts",
            "mutation-vs-return", "grouping"}

    problems = list(Problem.select().order_by(Problem.slug))
    # Every problem gets one graded interview plus two drills, so no concept is
    # missing from the profile just because the dice went the wrong way.
    plan = [(p, "interview") for p in problems] + \
           [(p, "drill") for p in problems for _ in range(2)]

    for problem, mode in plan:
        concepts = {link.concept.slug for link in problem.concept_links}
        struggling = bool(concepts & weak)
        ceiling = 4 if mode == "interview" else 2
        stopped_because = ("completed" if not struggling else
                           rng.choice(["turn_limit", "time_limit", "completed"]))

        # Solved means the checks passed, so a problem without checks cannot
        # be solved. The draw still happens either way: skipping it would shift
        # every later random value and change the demo profile the README shows.
        checks = len(PROBLEMS.get(problem.slug, {}).get("tests") or [])
        solved = rng.random() > (0.45 if struggling else 0.1)
        session = Session.create(
            student=student, problem=problem, mode=mode,
            model="claude-sonnet-5" if mode == "interview" else "claude-haiku-4-5",
            stopped_because=stopped_because,
            phase_reached="DEBUG" if mode == "interview" else "CODE",
            solved=solved if checks else None,
            check_results=("1" * checks if solved else "0" + "1" * (checks - 1))
                          if checks else None,
        )

        turns = rng.randint(9, 16) if mode == "interview" else rng.randint(4, 7)
        for ordinal in range(1, turns + 1):
            # A real session climbs the ladder rather than jumping about: early
            # turns are questions, deeper hints only arrive once the student is
            # stuck. Weight by position so the fake data has that shape too.
            progress = ordinal / turns
            if struggling:
                pool = [0, 1, 2] if progress < 0.4 else [1, 2, 3, 3, 4]
            else:
                pool = [0, 0, 0, 1] if progress < 0.6 else [0, 1, 1, 2]
            rung = min(rng.choice(pool), ceiling)

            Turn.create(
                session=session, ordinal=ordinal,
                phase=("CLARIFY" if progress < 0.15 else
                       "APPROACH" if progress < 0.4 else
                       "CODE" if progress < 0.8 else "DEBUG"),
                rung_used=rung, rung_ceiling=ceiling,
                advanced_phase=rng.random() < 0.2,
                input_tokens=rng.randint(700, 2500),
                output_tokens=rng.randint(80, 200),
            )

        session.input_tokens = sum(t.input_tokens for t in session.turns)
        session.output_tokens = sum(t.output_tokens for t in session.turns)
        # A finished session, so the picker counts it as practiced.
        session.ended_at = session.started_at + datetime.timedelta(minutes=turns)
        session.save()

        if mode == "interview":
            for dimension in RubricScore.DIMENSIONS:
                # Two sources, deliberately mismatched: the fake student rates
                # themselves higher than the proctor does, which is the gap the
                # debrief page exists to show.
                proctor_score = rng.randint(0, 2) if struggling else rng.randint(2, 3)
                RubricScore.create(session=session, dimension=dimension,
                                   source="proctor", score=proctor_score,
                                   note="Seeded example note.")
                RubricScore.create(session=session, dimension=dimension,
                                   source="self", score=min(3, proctor_score + 1))

    return student


def main():
    parser = argparse.ArgumentParser()
    # DATABASE_URL first, the same variable the server reads, so seeding
    # Supabase never needs its password typed on a command line.
    parser.add_argument("--path", default=os.environ.get("DATABASE_URL") or "data/rung.db")
    parser.add_argument("--demo", action="store_true",
                        help="add a fake student so the diagnostic has data")
    parser.add_argument("--reset", action="store_true",
                        help="drop every table first. Destroys all data.")
    args = parser.parse_args()

    init_db(args.path, create=False)

    if args.reset:
        print("  dropping all tables")
        database.drop_tables(ALL_TABLES, safe=True)

    database.create_tables(ALL_TABLES)
    add_missing_columns()

    with database.atomic():
        # One transaction. A half-seeded database is worse than no database,
        # and an interrupted run should leave nothing behind.
        units = seed_units()
        concepts = seed_concepts()
        problems = seed_problems()

    print(f"\n  bank      {BANK_SOURCE}")
    print(f"  units     {units}")
    print(f"  concepts  {concepts}")
    print(f"  problems  {problems}")
    print(f"  links     {ProblemConcept.select().count()}")

    if args.demo:
        with database.atomic():
            student = seed_demo_student()
        print(f"  demo      {student.handle}: {student.sessions.count()} sessions, "
              f"{Turn.select().join(Session).where(Session.student == student).count()} turns")

    # Never print a database URL as given: it carries the password.
    print(f"\n  database -> {describe_db(args.path)}")
    if args.demo:
        print("  next:  python -m tools.show_profile demo")
    close_db()
    return 0


if __name__ == "__main__":
    sys.exit(main())
