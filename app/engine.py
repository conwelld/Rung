"""
The session engine.

Everything the browser must not be trusted with lives here: which phase the
student is in, how deep the hint ladder may go, whether the budget is spent,
and what gets written to the database. The browser holds a session id and
nothing else.

This sits between the Flask routes and rung/proctor.py deliberately. Routes
should be about HTTP, and the proctor should be about one API call. The rules
that make this an interview rather than a chat window belong in neither.
"""

import datetime

from rung.budget import SessionBudget
from rung.config import RETAIN_TRANSCRIPTS, get_mode
from rung.grader import grade_session
from rung.models import Problem, RubricScore, Session, Student, Turn, database
from rung.proctor import ask_proctor

# How many turns a student must spend in a phase before the proctor may advance
# them. Without a floor the model will happily move CLARIFY -> APPROACH -> CODE
# in three turns, which defeats the phase gate: the rubric penalises jumping
# into code without explaining, so the tool has to make explaining take a beat.
PHASE_MINIMUMS = {"CLARIFY": 1, "APPROACH": 2, "CODE": 2, "DEBUG": 1}

# What the student needs to say before they may leave APPROACH. Short of a real
# classifier, this asks whether they wrote enough to constitute a plan. The
# proctor also has to agree via advance_phase, so this is a floor, not the rule.
MIN_APPROACH_CHARS = 60


class SessionEngine:
    """Wraps one live interview or drill.

    Constructed per request from the session id. Nothing is held in memory
    between requests, so a restart loses no state and two workers cannot
    disagree about whose turn it is.
    """

    def __init__(self, session: Session):
        self.session = session
        self.mode = get_mode(session.mode)
        self.phases = list(self.mode["phases"])

    # --- construction --------------------------------------------------------
    @classmethod
    def start(cls, handle: str, problem_slug: str, mode: str) -> "SessionEngine":
        settings = get_mode(mode)
        with database.atomic():
            student, _ = Student.get_or_create(handle=handle.strip().lower())
            problem = Problem.get(Problem.slug == problem_slug)
            session = Session.create(
                student=student, problem=problem, mode=mode,
                model=settings["model"],
                phase_reached=settings["phases"][0],
            )
        return cls(session)

    @classmethod
    def load(cls, session_id: int) -> "SessionEngine | None":
        session = Session.get_or_none(Session.id == session_id)
        return cls(session) if session else None

    # --- state ---------------------------------------------------------------
    @property
    def phase(self) -> str:
        return self.session.phase_reached or self.phases[0]

    @property
    def is_over(self) -> bool:
        return self.session.ended_at is not None

    def turns(self):
        return self.session.turns.order_by(Turn.ordinal)

    def turns_in_phase(self) -> int:
        return self.turns().where(Turn.phase == self.phase).count()

    def editor_unlocked(self) -> bool:
        """The phase gate, expressed as a UI fact.

        The editor stays locked until the student has stated a plan. This is the
        single most valuable behaviour in the tool: the rubric marks candidates
        down for coding without explaining, so the tool refuses to let them.
        """
        return self.phase in ("CODE", "DEBUG")

    def remaining_seconds(self) -> int:
        limit = self.mode["duration_minutes"] * 60
        elapsed = (datetime.datetime.now() - self.session.started_at).total_seconds()
        return max(0, int(limit - elapsed))

    def _budget(self) -> SessionBudget:
        """Rebuild the budget from the database.

        The budget object is not held between requests. Recomputing it from the
        stored turns means the limit survives a restart and cannot be reset by
        the client, which is the whole point of enforcing it server-side.
        """
        budget = SessionBudget(self.session.mode)
        budget.started_at = 0.0
        budget._clock = lambda: (
            datetime.datetime.now() - self.session.started_at).total_seconds()
        for turn in self.turns():
            budget.record({"input_tokens": turn.input_tokens,
                           "output_tokens": turn.output_tokens})
        return budget

    # --- the turn loop -------------------------------------------------------
    def history(self) -> list:
        """Rebuild the message list for the API from stored turns.

        Turn text is always stored while the session runs, so this works
        regardless of the retention setting. It has to: the API is stateless, so
        these rows are the proctor's only memory of the conversation. An earlier
        version skipped storing text entirely, which meant the proctor saw one
        message at a time and could not hold an interview at all.
        """
        messages = []
        for turn in self.turns():
            if turn.student_text and turn.proctor_text:
                messages.append({"role": "user", "content": turn.student_text})
                messages.append({"role": "assistant", "content": turn.proctor_text})
        return messages

    def send(self, student_message: str) -> dict:
        """One exchange. Returns what the browser needs to render."""
        if self.is_over:
            return {"ended": True, "reply": "This session has already finished."}

        budget = self._budget()
        if not budget.can_continue():
            self.finish(budget.stopped_because)
            return {"ended": True, "reply": budget.student_message(),
                    "stopped_because": budget.stopped_because}

        phase_before = self.phase
        turn = ask_proctor(
            problem={"prompt": self.session.problem.prompt,
                     "forbidden_insight": self.session.problem.forbidden_insight},
            phase=phase_before,
            history=self.history(),
            student_message=student_message,
            mode=self.session.mode,
        )

        advanced = self._maybe_advance(turn, student_message, phase_before)
        usage = turn.get("usage", {})

        with database.atomic():
            Turn.create(
                session=self.session,
                ordinal=self.turns().count() + 1,
                phase=phase_before,
                rung_used=min(turn["rung_used"], turn["rung_ceiling"]),
                rung_ceiling=turn["rung_ceiling"],
                advanced_phase=advanced,
                # Always stored during the session. Purged in finish() unless
                # RETAIN_TRANSCRIPTS, so the grade outlives the transcript.
                student_text=student_message,
                proctor_text=turn["reply"],
                input_tokens=usage.get("input_tokens", 0)
                + usage.get("cache_read_input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
            )
            self.session.input_tokens += usage.get("input_tokens", 0)
            self.session.output_tokens += usage.get("output_tokens", 0)
            if advanced:
                self.session.phase_reached = self.phase
            self.session.save()

        return {
            "reply": turn["reply"],
            "phase": self.phase,
            "advanced": advanced,
            "editor_unlocked": self.editor_unlocked(),
            "remaining_seconds": self.remaining_seconds(),
            "turns": self.turns().count(),
            "ended": False,
        }

    def _maybe_advance(self, turn: dict, student_message: str, phase: str) -> bool:
        """Decide whether the student moves to the next phase.

        The model proposes; the server decides. `advance_phase` is a suggestion
        from a model that has been asked to be helpful, and a helpful model will
        wave a student through CLARIFY in one turn. Three conditions must all
        hold, and the extra two are the ones a prompt cannot enforce.
        """
        if not turn.get("advance_phase"):
            return False
        if self.turns_in_phase() + 1 < PHASE_MINIMUMS.get(phase, 1):
            return False
        if phase == "APPROACH" and len(student_message.strip()) < MIN_APPROACH_CHARS:
            return False

        index = self.phases.index(phase)
        if index + 1 >= len(self.phases):
            return False
        self.session.phase_reached = self.phases[index + 1]
        return True

    # --- ending --------------------------------------------------------------
    def finish(self, reason: str = "completed", code: str = "") -> dict | None:
        """End the session, grade it, then purge the transcript.

        Order matters. The grader needs the text, so it runs first; the purge
        runs after and is unconditional unless retention is on. A grading
        failure must not leave a session open forever, so the session is closed
        either way and the debrief renders with self-scores alone.
        """
        if self.is_over:
            return None

        with database.atomic():
            self.session.ended_at = datetime.datetime.now()
            self.session.stopped_because = reason
            self.session.save()

        grades = None
        try:
            grades = grade_session(self.session, list(self.turns()), code=code)
            self.record_rubric(
                {d: g["score"] for d, g in grades.items()
                 if d != "overall" and g.get("score") is not None},
                notes={d: g.get("note") for d, g in grades.items() if d != "overall"},
                source="proctor",
            )
        except Exception:  # noqa: BLE001 - a missing grade is not a broken session
            pass

        if not RETAIN_TRANSCRIPTS:
            self._purge_transcript()

        return grades

    def _purge_transcript(self) -> None:
        """Drop the text, keep the measurements.

        Rung depth, phase, and token counts stay, so the diagnostic is unharmed.
        What leaves is the record of exactly what a student typed and how wrong
        they were, which is the part nobody needs six months from now.
        """
        with database.atomic():
            Turn.update(student_text=None, proctor_text=None).where(
                Turn.session == self.session).execute()

    def record_rubric(self, scores: dict, notes: dict | None = None,
                      source: str = "self") -> None:
        """Store rubric scores for a finished session.

        `source` is 'self' or 'proctor'. Both are kept, because the gap between
        them is often the most useful thing on the debrief page: a student who
        rates their own communication a 3 against the proctor's 1 has learned
        something neither number says alone.
        """
        notes = notes or {}
        with database.atomic():
            for dimension in RubricScore.DIMENSIONS:
                if dimension not in scores:
                    continue
                score = int(scores[dimension])
                RubricScore.insert(
                    session=self.session, dimension=dimension, source=source,
                    score=score, note=notes.get(dimension),
                ).on_conflict(
                    conflict_target=[RubricScore.session, RubricScore.dimension,
                                     RubricScore.source],
                    update={RubricScore.score: score,
                            RubricScore.note: notes.get(dimension)},
                ).execute()

    def grades(self, source: str = "proctor") -> dict:
        return {r.dimension: r for r in
                self.session.rubric_scores.where(RubricScore.source == source)}

    def state(self) -> dict:
        """Everything the browser needs to render the page."""
        return {
            "session_id": self.session.id,
            "handle": self.session.student.handle,
            "mode": self.session.mode,
            "problem": {
                "slug": self.session.problem.slug,
                "title": self.session.problem.title,
                "prompt": self.session.problem.prompt,
            },
            "phase": self.phase,
            "phases": self.phases,
            "editor_unlocked": self.editor_unlocked(),
            "remaining_seconds": self.remaining_seconds(),
            "turns": self.turns().count(),
            "max_turns": self.mode["max_turns"],
            "ended": self.is_over,
        }
