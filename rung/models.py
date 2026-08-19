"""
The schema.

Seven tables. The shape is driven by one question: what does a student need to
work on? Answering that means knowing how deep in the hint ladder they went, on
which concepts, over time. Everything here exists to make that query possible.

Two decisions worth reading before the code.

**Turn does not store text.** No student message, no proctor reply. The
diagnostic needs `rung_used` and a concept tag, and nothing else. Storing
transcripts would mean holding a semester of student work, mistakes and all, in
a SQLite file on a class server. `RETAIN_TRANSCRIPTS` in rung/config.py can turn
it on for local debugging, and it defaults off. Collecting less is easier to
defend than securing more. See SECURITY.md.

**The concept profile is a query, not a table.** Nothing precomputes or caches
it. At class scale that query is milliseconds, and a stored aggregate is a
second source of truth that goes stale the moment a session is deleted.
Denormalise when a measurement says to, not before. See rung/diagnostics.py.
"""

import datetime

from peewee import (
    AutoField, BooleanField, CharField, DateTimeField, ForeignKeyField,
    IntegerField, Model, SqliteDatabase, TextField,
)

# Deferred: the path is supplied by init_db so tests can point at :memory:
# without touching the real file.
database = SqliteDatabase(None)

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
    """The course's interview units. This is the difficulty axis: the
    curriculum already orders these, so there is no separate difficulty rating
    to invent or maintain."""
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
    solved = BooleanField(null=True)

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

    Text fields are null by default. See the module docstring.
    """
    id = AutoField()
    session = ForeignKeyField(Session, backref="turns", on_delete="CASCADE")
    ordinal = IntegerField()
    phase = CharField()
    rung_used = IntegerField(default=0)
    rung_ceiling = IntegerField()
    advanced_phase = BooleanField(default=False)

    student_text = TextField(null=True)     # only when RETAIN_TRANSCRIPTS
    proctor_text = TextField(null=True)     # only when RETAIN_TRANSCRIPTS

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


ALL_TABLES = [Student, Unit, Concept, Problem, ProblemConcept,
              Session, Turn, RubricScore]


def init_db(path=":memory:", create=True):
    """Open the database and optionally create tables.

    Call once at startup. `:memory:` gives tests a fresh database per run with
    no file to clean up and no chance of a test writing into real data.
    """
    if not database.is_closed():
        database.close()
    database.init(path, pragmas=PRAGMAS)
    database.connect()
    if create:
        database.create_tables(ALL_TABLES)
    return database


def close_db():
    if not database.is_closed():
        database.close()
