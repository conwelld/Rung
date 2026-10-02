"""
The schema.

Eight tables. The shape is driven by one question: what does a student need to
work on? Answering that means knowing how deep in the hint ladder they went, on
which concepts, over time. Everything here exists to make that query possible.

Two decisions worth reading before the code.

**Turn text is working memory.** Student and proctor text is stored while a
session is open because the stateless model API and end-of-session grader need
it. `finish()` purges both fields unless `RETAIN_TRANSCRIPTS` is explicitly on,
leaving rung, phase, and token evidence behind. The useful diagnostic outlives
the sensitive transcript. See SECURITY.md.

**The concept profile is a query, not a table.** Nothing precomputes or caches
it. At class scale that query is milliseconds, and a stored aggregate is a
second source of truth that goes stale the moment a session is deleted.
Denormalise when a measurement says to, not before. See rung/diagnostics.py.
"""

import datetime

from peewee import (
    AutoField, BooleanField, CharField, DatabaseProxy, DateTimeField,
    ForeignKeyField, IntegerField, Model, SqliteDatabase, TextField,
)

# A proxy rather than a database: init_db decides at startup whether it stands
# for SQLite (local, Elastic Beanstalk, tests at :memory:) or Postgres (Supabase
# on Vercel), and every model keeps pointing at this same object either way.
# An earlier version used a deferred SqliteDatabase here, which made the
# Postgres path fail on its first line.
database = DatabaseProxy()

# SQLite does NOT enforce foreign keys unless you ask it to, per connection.
# Without this pragma every ForeignKeyField below is documentation rather than a
# constraint, and orphaned rows accumulate silently. WAL lets a read run while a
# write is in progress, which matters once Flask serves more than one student.
PRAGMAS = {
    "foreign_keys": 1,
    "journal_mode": "wal",
    "synchronous": 1,
    "cache_size": -32 * 1000,   # KiB, negative means KiB rather than pages
}


class BaseModel(Model):
    class Meta:
        database = database


class Student(Model):
    """No password, no email, no auth. v1 identifies a student by a handle they
    type, which is enough to attribute a diagnostic and nothing more. Adding
    real accounts later means adding columns here, not reshaping the schema."""
    id = AutoField()
    handle = CharField(unique=True, index=True)
    created_at = DateTimeField(default=datetime.datetime.now)

    class Meta:
        database = database

    def __str__(self):
        return self.handle


class Unit(BaseModel):
    """The course's interview units and primary difficulty progression.

    The problem bank may add a course-relative challenge label for scanning in
    the picker; the unit remains the authoritative curriculum gate.
    """
    id = AutoField()
    number = IntegerField(unique=True)
    title = CharField()
    week_range = CharField(null=True)

    def __str__(self):
        return f"Unit {self.number}: {self.title}"


class Concept(BaseModel):
    """A taggable skill: dict-construction, accumulator, mutation-vs-return.
    The diagnostic groups by this, so a student learns they are shaky on
    recursion rather than shaky on problem 7."""
    id = AutoField()
    slug = CharField(unique=True, index=True)
    title = CharField()

    def __str__(self):
        return self.slug


class Problem(BaseModel):
    """Mirrors the JSON bank. Loaded by tools/init_db.py rather than authored
    here, so the bank stays the source of truth and the department's private
    wording never has to enter the database schema or the repository."""
    id = AutoField()
    slug = CharField(unique=True, index=True)
    title = CharField()
    unit = ForeignKeyField(Unit, backref="problems")
    prompt = TextField()
    forbidden_insight = TextField()
    active = BooleanField(default=True)

    def __str__(self):
        return self.slug


class ProblemConcept(BaseModel):
    """Explicit junction table for the problem-to-concept many-to-many.

    peewee offers ManyToManyField, which would generate this automatically. An
    explicit model is used instead for two reasons: the join is written out in
    the diagnostic query where it is easiest to reason about, and a real table
    can carry its own columns later (weight, primary vs incidental) without a
    migration that changes the relationship type.
    """
    id = AutoField()
    problem = ForeignKeyField(Problem, backref="concept_links", on_delete="CASCADE")
    concept = ForeignKeyField(Concept, backref="problem_links", on_delete="CASCADE")

    class Meta:
        # One row per pair. Without this a reseed silently duplicates every
        # link and every count in the diagnostic doubles.
        indexes = ((("problem", "concept"), True),)


class Session(BaseModel):
    """One interview or drill attempt.

    Token counts and cost live here rather than being recomputed, because they
    come from the API's usage block and cannot be derived later. `mode` is a
    plain string matching a key in config.MODES.
    """
    id = AutoField()
    student = ForeignKeyField(Student, backref="sessions", on_delete="CASCADE")
    problem = ForeignKeyField(Problem, backref="sessions")
    mode = CharField(index=True)
    model = CharField()

    started_at = DateTimeField(default=datetime.datetime.now)
    ended_at = DateTimeField(null=True)
    stopped_because = CharField(null=True)   # turn_limit, time_limit, token_budget, completed
    phase_reached = CharField(null=True)
    # True when the final code passed every check in the bank, False when it
    # was checked and did not, null when no checks ran on it. Reported by the
    # browser, because student code never executes on the server.
    solved = BooleanField(null=True)
    # One character per bank check, "1" passed and "0" failed: "1101". Kept so
    # the debrief can name the failing inputs after the session is over.
    check_results = CharField(null=True)
    # Set while a turn's model call is in flight; see SessionEngine._claim_turn.
    turn_lease_until = DateTimeField(null=True)

    input_tokens = IntegerField(default=0)
    output_tokens = IntegerField(default=0)

    def duration_minutes(self):
        if not self.ended_at:
            return None
        return (self.ended_at - self.started_at).total_seconds() / 60


class Turn(BaseModel):
    """One exchange. This is the row the whole diagnostic rests on.

    `rung_used` is the measurement: how much help the student needed at this
    moment. Deep rungs on one concept and shallow rungs on another is a profile,
    and it does not need pass/fail data to be useful.

    Text fields are populated for an open session and purged at finish by
    default. See the module docstring.
    """
    id = AutoField()
    session = ForeignKeyField(Session, backref="turns", on_delete="CASCADE")
    ordinal = IntegerField()
    phase = CharField()
    rung_used = IntegerField(default=0)
    rung_ceiling = IntegerField()
    advanced_phase = BooleanField(default=False)

    student_text = TextField(null=True)     # working memory; normally purged
    proctor_text = TextField(null=True)     # working memory; normally purged

    input_tokens = IntegerField(default=0)
    output_tokens = IntegerField(default=0)
    created_at = DateTimeField(default=datetime.datetime.now)

    class Meta:
        indexes = ((("session", "ordinal"), True),)


class RubricScore(BaseModel):
    """The department's four dimensions, scored at session end.

    Two rows per dimension: one `self` score from the student, one `proctor`
    score from the grader. Keeping both is the point. A student who rates their
    communication a 3 while the proctor rates it a 1 has learned something more
    useful than either number alone, and calibration is a skill the interview
    format is partly trying to teach.

    `score` runs 3 for strong hire down to 0 for strong no hire, so it sorts and
    averages meaningfully. It is nullable because refusing to score is a valid
    outcome: technical competency cannot be judged without code, and an absent
    score is more honest than an invented one.
    """
    DIMENSIONS = ("communication", "problem_solving", "technical_competency", "debugging")
    LABELS = {3: "strong hire", 2: "leaning hire", 1: "leaning no hire", 0: "strong no hire"}
    SOURCES = ("self", "proctor")

    id = AutoField()
    session = ForeignKeyField(Session, backref="rubric_scores", on_delete="CASCADE")
    dimension = CharField()
    source = CharField(default="self")
    score = IntegerField(null=True)
    note = TextField(null=True)

    class Meta:
        # One row per dimension per source. Rescoring updates rather than
        # appends, so a student changing their mind does not leave both answers
        # in the table.
        indexes = ((("session", "dimension", "source"), True),)

    def label(self):
        return self.LABELS.get(self.score, "not scored")


class SessionFeedback(BaseModel):
    """The grader's one actionable next step for a finished session.

    Dimension notes belong beside their scores. This summary belongs to the
    session itself and survives transcript deletion, so students leave with a
    concrete practice target without us retaining their interview text.
    """
    id = AutoField()
    session = ForeignKeyField(
        Session, backref="feedback_rows", unique=True, on_delete="CASCADE")
    summary = TextField()


ALL_TABLES = [Student, Unit, Concept, Problem, ProblemConcept,
              Session, Turn, RubricScore, SessionFeedback]


def init_db(path=":memory:", create=True):
    """Open the database and optionally create tables.

    Call once at startup. `:memory:` gives tests a fresh database per run with
    no file to clean up and no chance of a test writing into real data.

    `path` may also be a database URL. `postgresql://...` swaps the backend
    without touching a single model or query, which is the concrete payoff of
    using an ORM here rather than writing SQL by hand.
    """
    if database.obj is not None and not database.is_closed():
        database.close()

    if "://" in str(path):
        from playhouse.db_url import connect as connect_url
        options = {}
        if str(path).startswith(("postgres://", "postgresql://")):
            # Supabase's transaction pooler (port 6543, the one serverless
            # hosts should use) hands each transaction to whichever server
            # connection is free, so a statement psycopg prepared on one is
            # missing on the next. None turns psycopg's automatic preparing off.
            options["prepare_threshold"] = None
        # Passwords with @ or : in them arrive percent-encoded in the URL.
        backend = connect_url(str(path), unquote_user=True, unquote_password=True, **options)
    else:
        backend = SqliteDatabase(path, pragmas=PRAGMAS)
    database.initialize(backend)

    database.connect(reuse_if_open=True)
    if create:
        database.create_tables(ALL_TABLES)
    return database


def describe_db(path) -> str:
    """Where a database lives, with any password removed, for printing."""
    from urllib.parse import urlsplit

    if "://" not in str(path):
        return str(path)
    parts = urlsplit(str(path))
    return f"{parts.scheme}://{parts.hostname}:{parts.port or ''}{parts.path}"


def add_missing_columns():
    """Add columns introduced after a database was first created.

    create_tables() creates missing tables but never alters existing ones, so a
    deployment whose database predates a column would fail on the first query
    that selects it. Each step checks before it alters, so this is safe to run
    on every startup.
    """
    from playhouse.migrate import SchemaMigrator, migrate

    existing = {column.name for column in database.get_columns("session")}
    # The migrator picks SQL by backend type, so it needs the real database
    # behind the proxy.
    migrator = SchemaMigrator.from_database(database.obj)
    added = {
        "check_results": CharField(null=True),
        "turn_lease_until": DateTimeField(null=True),
    }
    for name, field in added.items():
        if name not in existing:
            migrate(migrator.add_column("session", name, field))


def close_db():
    if database.obj is not None and not database.is_closed():
        database.close()
