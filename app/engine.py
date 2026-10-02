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
import logging

from rung.budget import SessionBudget
from rung.config import RETAIN_TRANSCRIPTS, get_mode
from rung.grader import grade_session
from rung.models import (
    Problem, RubricScore, Session, SessionFeedback, Student, Turn, database,
)
from rung.problems import PROBLEMS
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

# How long one turn may hold the session before another request can take it.
# Longer than the proctor's 60 second API timeout, so a slow reply is never
# overlapped, and short enough that a crashed request frees the session soon.
TURN_LEASE_SECONDS = 90
logger = logging.getLogger(__name__)


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
            problem = Problem.get(
                (Problem.slug == problem_slug) & (Problem.active == True))  # noqa: E712
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

    def _claim_turn(self) -> bool:
        """Take the session's single turn slot, or report that it is taken.

        The turn cap and token budget are checked before each model call, from
        the turns stored so far. Twenty requests fired at once would all see the
        same count, all pass, and all be billed, so the cap only holds if calls
        happen one at a time. A single conditional UPDATE is atomic in SQLite
        and Postgres alike, so exactly one request wins. The lease expires on
        its own, so a request that dies mid-call cannot lock the session.
        """
        now = datetime.datetime.now()
        claimed = (Session
                   .update(turn_lease_until=now + datetime.timedelta(seconds=TURN_LEASE_SECONDS))
                   .where((Session.id == self.session.id)
                          & Session.ended_at.is_null()
                          & (Session.turn_lease_until.is_null()
                             | (Session.turn_lease_until < now)))
                   .execute())
        return claimed == 1

    def _release_turn(self) -> None:
        Session.update(turn_lease_until=None).where(Session.id == self.session.id).execute()

    def send(self, student_message: str) -> dict:
        """One exchange. Returns what the browser needs to render."""
        if self.is_over:
            return {"ended": True, "reply": "This session has already finished."}
        if not self._claim_turn():
            if Session.get_by_id(self.session.id).ended_at is not None:
                return {"ended": True, "reply": "This session has already finished."}
            return {"busy": True}
        try:
            # Reread now that this request owns the session: the copy loaded at
            # the start of the request may predate the turn that just released it.
            self.session = Session.get_by_id(self.session.id)
            return self._exchange(student_message)
        finally:
            self._release_turn()

    def _exchange(self, student_message: str) -> dict:
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
        input_used = (
            usage.get("input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0)
            + usage.get("cache_read_input_tokens", 0)
        )

        with database.atomic():
            Turn.create(
                session=self.session,
                ordinal=self.turns().count() + 1,
                phase=phase_before,
                rung_used=max(0, min(turn["rung_used"], turn["rung_ceiling"])),
                rung_ceiling=turn["rung_ceiling"],
                advanced_phase=advanced,
                # Always stored during the session. Purged in finish() unless
                # RETAIN_TRANSCRIPTS, so the grade outlives the transcript.
                student_text=student_message,
                proctor_text=turn["reply"],
                input_tokens=input_used,
                output_tokens=usage.get("output_tokens", 0),
            )
            self.session.input_tokens += input_used
            self.session.output_tokens += usage.get("output_tokens", 0)
            if advanced:
                self.session.phase_reached = self.phase
            # Only the fields this method changed. A full save would write back
            # this request's copy of ended_at, reopening a session that the
            # browser's finish call closed while the model was answering.
            self.session.save(only=[Session.input_tokens, Session.output_tokens,
                                    Session.phase_reached])

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

    # --- checks --------------------------------------------------------------
    def check_tests(self) -> list:
        """The bank's [input, expected] pairs for this problem, possibly empty.

        Read from the bank rather than the database, like `challenge` and
        `topic`, so editing a problem's tests needs no reseed.
        """
        return list(PROBLEMS.get(self.session.problem.slug, {}).get("tests") or [])

    def record_checks(self, results) -> bool:
        """Store the browser's check results for the final code, once.

        The browser is the only place student code runs, so this is a report
        rather than a verdict, and it is validated as one: one boolean per bank
        check, or nothing is stored. The first report wins, so re-posting after
        the debrief has shown the failing inputs cannot rewrite the record.
        """
        tests = self.check_tests()
        if (not tests or not isinstance(results, list) or len(results) != len(tests)
                or not all(isinstance(r, bool) for r in results)
                or self.session.check_results is not None):
            return False
        with database.atomic():
            self.session.check_results = "".join("1" if r else "0" for r in results)
            self.session.solved = all(results)
            self.session.save()
        return True

    # --- ending --------------------------------------------------------------
    def finish(self, reason: str = "completed", code: str = "",
               checks: list | None = None) -> dict | None:
        """End the session, grade it, then purge the transcript.

        Order matters. The grader needs the text, so it runs first; the purge
        runs after and is unconditional unless retention is on. A grading
        failure must not leave a session open forever, so the session is closed
        either way and the debrief renders with self-scores alone.

        Check results are recorded even when the session already ended: a turn
        or token limit closes it server-side before the browser has sent its
        final code, and the browser's finish call arrives just after.
        """
        if checks is not None:
            self.record_checks(checks)
        if self.is_over:
            return None

        # Claim the ending with a conditional UPDATE rather than a check then a
        # save: two finish calls arriving together (a double click, the timer
        # and the End button) would otherwise both pass is_over and both pay
        # for a grading call. Only the request that actually closes the session
        # grades it.
        ended_at = datetime.datetime.now()
        closed = (Session
                  .update(ended_at=ended_at, stopped_because=reason)
                  .where((Session.id == self.session.id) & Session.ended_at.is_null())
                  .execute())
        if closed != 1:
            return None
        self.session.ended_at = ended_at
        self.session.stopped_because = reason

        grades = None
        try:
            grades = grade_session(self.session, list(self.turns()), code=code)
            self.record_rubric(
                {d: g["score"] for d, g in grades.items()
                 if d != "overall" and g.get("score") is not None},
                notes={d: g.get("note") for d, g in grades.items() if d != "overall"},
                source="proctor",
            )
            if grades.get("overall"):
                SessionFeedback.insert(
                    session=self.session, summary=grades["overall"]
                ).on_conflict(
                    conflict_target=[SessionFeedback.session],
                    update={SessionFeedback.summary: grades["overall"]},
                ).execute()
        except Exception:  # noqa: BLE001 - a missing grade is not a broken session
            logger.exception("grading failed for session %s", self.session.id)

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
