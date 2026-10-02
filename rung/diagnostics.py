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

from peewee import JOIN, Case, fn

from rung.models import (
    Concept, Problem, ProblemConcept, Session, Student, Turn, Unit,
)


def _avg(value):
    """An AVG() result as a rounded float, or None.

    SQLite returns a float. Postgres returns a Decimal, which then raises the
    moment it meets float arithmetic (the recommendation score adds 0.35 to it).
    """
    return round(float(value), 2) if value is not None else None


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
        # and would otherwise be another round trip per concept. CASE rather
        # than IIF, which exists only in SQLite.
        Case(None, [(Turn.rung_used >= 3, 1)], 0)
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
            "avg_rung": _avg(row.avg_rung),
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
            "avg_rung": _avg(row.avg_rung),
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
        "avg_rung": _avg(row.avg_rung),
        "input_tokens": row.input_tokens or 0,
        "output_tokens": row.output_tokens or 0,
        "stopped_because": session.stopped_because,
        "duration_minutes": session.duration_minutes(),
    }


def problem_status(student):
    """Per-problem progress for one student, keyed by problem id.

    `practiced` means a session on the problem reached its end. `solved` means
    a finished session's code passed every check the bank defines. The two are
    kept apart on purpose: a finished rep is exposure, passing the checks is
    evidence the code works, and neither says how independently the student
    got there. `best_depth` does: the deepest hint needed in the student's
    least-helped solving session, so "solved at rung 1" and "solved at rung 4"
    stay different things. Problems without checks can be practiced but never
    show as solved, rather than being marked on weaker evidence.

    Three grouped queries regardless of how many problems exist.
    """
    status = {}
    if student is None:
        return status

    finished = fn.SUM(Case(None, [(Session.ended_at.is_null(False), 1)], 0))
    solved = fn.SUM(Case(None, [(Session.solved == True, 1)], 0))  # noqa: E712
    for problem_id, sessions, finished_count, solved_count in (
            Session
            .select(Session.problem, fn.COUNT(Session.id), finished, solved)
            .where(Session.student == student)
            .group_by(Session.problem)
            .tuples()):
        status[problem_id] = {
            "sessions": sessions,
            "practiced": bool(finished_count),
            "solved": bool(solved_count),
            "turns": 0,
            "rung_total": 0,
            "best_depth": None,
        }

    for problem_id, turns, rung_total in (
            Turn
            .select(Session.problem, fn.COUNT(Turn.id), fn.SUM(Turn.rung_used))
            .join(Session, on=(Turn.session == Session.id))
            .where(Session.student == student)
            .group_by(Session.problem)
            .tuples()):
        status[problem_id]["turns"] = turns
        status[problem_id]["rung_total"] = rung_total or 0

    for problem_id, depth in (
            Session
            .select(Session.problem, fn.MAX(Turn.rung_used))
            .join(Turn, JOIN.LEFT_OUTER, on=(Turn.session == Session.id))
            .where((Session.student == student) & (Session.solved == True))  # noqa: E712
            .group_by(Session.id)
            .tuples()):
        depth = depth or 0
        best = status[problem_id]["best_depth"]
        status[problem_id]["best_depth"] = depth if best is None else min(best, depth)

    return status


def recommended_problems(student, limit=3, max_unit=None):
    """Choose useful next drills from the student's measured weak concepts.

    This is intentionally deterministic. The LLM conducts an interview; it
    does not get to invent a learning path. Recommendations come from stored
    hint depth, curriculum tags, unit availability, attempt counts, and whether
    the problem is already solved.
    """
    profile = concept_profile(student)
    weakness = {row["slug"]: row["avg_rung"] for row in profile}
    status = problem_status(student)

    query = (
        Problem
        .select(
            Problem.id.alias("problem_id"),
            Problem.slug.alias("problem_slug"),
            Problem.title.alias("problem_title"),
            Unit.number.alias("unit_number"),
            Unit.title.alias("unit_title"),
            Concept.slug.alias("concept_slug"),
            Concept.title.alias("concept_title"),
        )
        .join(Unit, on=(Problem.unit == Unit.id))
        .switch(Problem)
        .join(ProblemConcept, on=(ProblemConcept.problem == Problem.id))
        .join(Concept, on=(ProblemConcept.concept == Concept.id))
        .where(Problem.active == True)  # noqa: E712
        .order_by(Unit.number, Problem.title)
        .dicts()
    )
    if max_unit is not None:
        query = query.where(Unit.number <= max_unit)

    problems = {}
    for row in query:
        item = problems.setdefault(row["problem_id"], {
            "id": row["problem_id"],
            "slug": row["problem_slug"],
            "title": row["problem_title"],
            "unit_number": row["unit_number"],
            "unit_title": row["unit_title"],
            "concepts": [],
        })
        item["concepts"].append({
            "slug": row["concept_slug"], "title": row["concept_title"]})

    ranked = []
    for problem_id, item in problems.items():
        item["attempts"] = status.get(problem_id, {}).get("sessions", 0)
        item["solved"] = status.get(problem_id, {}).get("solved", False)
        matches = [c for c in item["concepts"] if c["slug"] in weakness]
        if matches:
            focus = max(matches, key=lambda c: weakness[c["slug"]])
            affinity = weakness[focus["slug"]]
            item["reason"] = (
                f"Targets {focus['title']}, where your average hint depth is "
                f"{weakness[focus['slug']]:.2f}."
            )
        else:
            focus = item["concepts"][0]
            affinity = 0
            item["reason"] = f"Builds breadth in {focus['title']}."

        # Prefer a strong concept match, then an untried problem, and step away
        # from one already solved: the same concept is better practised on code
        # the student has not yet made work. The tiny unit term breaks ties
        # toward material nearest the student's current point.
        item["score"] = (
            affinity
            + (0.35 if item["attempts"] == 0 else 0)
            - min(item["attempts"], 5) * 0.08
            - (0.5 if item["solved"] else 0)
            + item["unit_number"] * 0.005
        )
        ranked.append(item)

    ranked.sort(key=lambda row: (-row["score"], row["attempts"], row["title"]))
    return ranked[:limit]


def class_metrics():
    """A few bounded class-wide facts for the instructor overview."""
    return {
        "students": Student.select().count(),
        "sessions": Session.select().count(),
        "turns": Turn.select().count(),
        "completed": Session.select().where(Session.ended_at.is_null(False)).count(),
    }


def student_overview():
    """One aggregate query for instructor-facing student activity."""
    # DISTINCT problem ids, not a count of solving sessions: solving the same
    # problem three times is one solved problem.
    solved = fn.COUNT(fn.DISTINCT(
        Case(None, [(Session.solved == True, Session.problem)])))  # noqa: E712
    query = (
        Student
        .select(
            Student.handle,
            fn.COUNT(fn.DISTINCT(Session.id)).alias("sessions"),
            solved.alias("solved"),
            fn.AVG(Turn.rung_used).alias("avg_rung"),
            fn.MAX(Session.started_at).alias("last_session"),
        )
        .join(Session, JOIN.LEFT_OUTER, on=(Session.student == Student.id))
        .join(Turn, JOIN.LEFT_OUTER, on=(Turn.session == Session.id))
        .group_by(Student.id)
        # NULLS LAST keeps students who never started a session at the bottom.
        # Postgres sorts NULL first in a descending order, SQLite last.
        .order_by(fn.MAX(Session.started_at).desc(nulls="LAST"))
    )
    return [{
        "handle": row.handle,
        "sessions": row.sessions or 0,
        "solved": row.solved or 0,
        "avg_rung": _avg(row.avg_rung),
        "last_session": row.last_session,
    } for row in query.objects()]


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
            Concept.title,
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
            "title": row.title,
            "students": row.students,
            "avg_rung": _avg(row.avg_rung),
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
