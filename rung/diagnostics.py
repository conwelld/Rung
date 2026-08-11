"""
The concept diagnostic. This is the thing the whole schema exists for.

A student finishes six sessions. What should they work on? Pass/fail is a poor
signal at this level because almost everyone eventually passes. Hint depth is
better: needing rung 4 on every dictionary problem and rung 1 on every string
problem is a profile, and it says something pass/fail cannot.

Every function here answers that in a single query. The N+1 version is written
out at the bottom, wrong on purpose, because it is the shape this naturally
takes if you write it the obvious way and it is worth being able to point at.
"""

from peewee import JOIN, fn

from rung.models import (
    Concept, Problem, ProblemConcept, Session, Student, Turn, Unit,
)


def concept_profile(student, mode=None):
    """Return per-concept hint depth for one student, deepest need first.

    One query. The join runs Concept -> ProblemConcept -> Problem -> Session ->
    Turn, filtered to this student, grouped by concept. The database does the
    aggregation, which is the entire reason for using one.

    Returns dicts with: slug, title, turns, sessions, max_rung, avg_rung,
    deep_turns (rung 3 or 4).
    """
    deep = fn.SUM(
        # A CASE expression rather than a second query. Counting "how many turns
        # needed a deep hint" alongside the averages costs nothing extra here
        # and would otherwise be another round trip per concept.
        fn.IIF(Turn.rung_used >= 3, 1, 0)
    ).alias("deep_turns")

    query = (
        Concept
        .select(
            Concept.slug,
            Concept.title,
            fn.COUNT(Turn.id).alias("turns"),
            fn.COUNT(fn.DISTINCT(Session.id)).alias("sessions"),
            fn.MAX(Turn.rung_used).alias("max_rung"),
            fn.AVG(Turn.rung_used).alias("avg_rung"),
            deep,
        )
        .join(ProblemConcept, on=(ProblemConcept.concept == Concept.id))
        .join(Problem, on=(ProblemConcept.problem == Problem.id))
        .join(Session, on=(Session.problem == Problem.id))
        .join(Turn, on=(Turn.session == Session.id))
        .where(Session.student == student)
        .group_by(Concept.id)
        .order_by(fn.AVG(Turn.rung_used).desc())
    )
    if mode:
        query = query.where(Session.mode == mode)

    return [
        {
            "slug": row.slug,
            "title": row.title,
            "turns": row.turns,
            "sessions": row.sessions,
            "max_rung": row.max_rung,
            "avg_rung": round(row.avg_rung, 2) if row.avg_rung is not None else None,
            "deep_turns": row.deep_turns or 0,
        }
        for row in query.objects()
    ]


def weakest_concepts(student, limit=3, min_turns=3):
    """The concepts to drill next.

    `min_turns` guards against a single deep hint on a concept seen once
    outranking a genuine pattern. Two data points is not a weakness.
    """
    profile = concept_profile(student)
    eligible = [c for c in profile if c["turns"] >= min_turns]
    return eligible[:limit]


def unit_progress(student):
    """Sessions and average hint depth per unit, for the roadmap view.

    LEFT OUTER JOIN so units the student has not started still appear with
    zeros. An inner join would silently omit them, and "units you have not
    touched" is exactly what a roadmap needs to show.
    """
    query = (
        Unit
        .select(
            Unit.number,
            Unit.title,
            fn.COUNT(fn.DISTINCT(Session.id)).alias("sessions"),
            fn.AVG(Turn.rung_used).alias("avg_rung"),
        )
        .join(Problem, JOIN.LEFT_OUTER, on=(Problem.unit == Unit.id))
        .join(Session, JOIN.LEFT_OUTER,
              on=((Session.problem == Problem.id) & (Session.student == student)))
        .join(Turn, JOIN.LEFT_OUTER, on=(Turn.session == Session.id))
        .group_by(Unit.id)
        .order_by(Unit.number)
    )
    return [
        {
            "number": row.number,
            "title": row.title,
            "sessions": row.sessions or 0,
            "avg_rung": round(row.avg_rung, 2) if row.avg_rung is not None else None,
        }
        for row in query.objects()
    ]


def session_summary(session):
    """Aggregates for one finished session, for the post-interview debrief."""
    row = (
        Turn
        .select(
            fn.COUNT(Turn.id).alias("turns"),
            fn.MAX(Turn.rung_used).alias("deepest_rung"),
            fn.AVG(Turn.rung_used).alias("avg_rung"),
            fn.SUM(Turn.input_tokens).alias("input_tokens"),
            fn.SUM(Turn.output_tokens).alias("output_tokens"),
        )
        .where(Turn.session == session)
        .objects()
        .first()
    )
    return {
        "turns": row.turns or 0,
        "deepest_rung": row.deepest_rung,
        "avg_rung": round(row.avg_rung, 2) if row.avg_rung is not None else None,
        "input_tokens": row.input_tokens or 0,
        "output_tokens": row.output_tokens or 0,
        "stopped_because": session.stopped_because,
        "duration_minutes": session.duration_minutes(),
    }


def class_overview(min_students=1):
    """Which concepts the whole class struggles with.

    Aimed at the instructor rather than the student. If two thirds of a class
    hits rung 4 on mutation-vs-return, that is a lecture problem rather than a
    set of individual student problems, and it is worth someone knowing.
    """
    query = (
        Concept
        .select(
            Concept.slug,
            fn.COUNT(fn.DISTINCT(Session.student)).alias("students"),
            fn.AVG(Turn.rung_used).alias("avg_rung"),
        )
        .join(ProblemConcept, on=(ProblemConcept.concept == Concept.id))
        .join(Problem, on=(ProblemConcept.problem == Problem.id))
        .join(Session, on=(Session.problem == Problem.id))
        .join(Turn, on=(Turn.session == Session.id))
        .group_by(Concept.id)
        .having(fn.COUNT(fn.DISTINCT(Session.student)) >= min_students)
        .order_by(fn.AVG(Turn.rung_used).desc())
    )
    return [
        {
            "slug": row.slug,
            "students": row.students,
            "avg_rung": round(row.avg_rung, 2) if row.avg_rung is not None else None,
        }
        for row in query.objects()
    ]


# ---------------------------------------------------------------------------
# Kept deliberately, never called.
#
# This is what concept_profile looks like written the obvious way, and it is
# the classic N+1: one query for the concepts, then one more per concept, so
# fourteen concepts is fifteen round trips instead of one. It returns the right
# answer, which is why it survives code review and then falls over in
# production once the tables are large enough for the round trips to matter.
#
# The fix is not caching. It is letting the database do the join and the
# aggregation, which is what it is for.
# ---------------------------------------------------------------------------
def _concept_profile_n_plus_one(student):  # pragma: no cover
    results = []
    for concept in Concept.select():                      # query 1
        turns = (Turn                                     # query 2..N+1
                 .select()
                 .join(Session, on=(Turn.session == Session.id))
                 .join(Problem, on=(Session.problem == Problem.id))
                 .join(ProblemConcept, on=(ProblemConcept.problem == Problem.id))
                 .where((ProblemConcept.concept == concept)
                        & (Session.student == student)))
        rungs = [t.rung_used for t in turns]
        if rungs:
            results.append({
                "slug": concept.slug,
                "turns": len(rungs),
                "max_rung": max(rungs),
                "avg_rung": sum(rungs) / len(rungs),
            })
    return sorted(results, key=lambda r: -r["avg_rung"])
