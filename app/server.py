"""
Flask routes.

Thin on purpose. Every rule that matters lives in app/engine.py or rung/, and
these functions do HTTP: parse a request, call the engine, render or return
JSON. If a route starts making decisions about phases or rungs, it belongs in
the engine instead.

    set ANTHROPIC_API_KEY=sk-ant-...
    python -m app.server

The API key never reaches the browser. The client posts here, this process
calls Anthropic, the reply comes back through the server. See SECURITY.md.
"""

import datetime
import os
import time
from collections import defaultdict, deque

from flask import (
    Flask, jsonify, redirect, render_template, request, session as cookie, url_for,
)

from app.engine import SessionEngine
from rung.config import (
    DAILY_DRILL_LIMIT, DAILY_INTERVIEW_LIMIT, MODES, REQUESTS_PER_MINUTE,
)
from rung.diagnostics import concept_profile, session_summary, unit_progress, weakest_concepts
from rung.models import Problem, RubricScore, Session, Student, Unit, close_db, init_db

app = Flask(__name__)

# Signs the session cookie, which holds only a session id and a handle. Falling
# back to a random value means restarting the server logs everyone out, which is
# the right failure: better than shipping a hardcoded default that ends up in
# production because nobody set the variable.
app.secret_key = os.environ.get("RUNG_SECRET_KEY") or os.urandom(32)

DB_PATH = os.environ.get("RUNG_DB", "data/rung.db")

# In-process rate limiting. Correct for one worker, which is what this runs as.
# Multiple workers would each keep their own counter, so a real deployment wants
# this in the database or a shared store. Noted rather than solved: the fix is
# easy and the wrong fix is pretending one process is enough forever.
_recent_requests = defaultdict(deque)


def _rate_limited(key: str) -> bool:
    now = time.monotonic()
    window = _recent_requests[key]
    while window and now - window[0] > 60:
        window.popleft()
    if len(window) >= REQUESTS_PER_MINUTE:
        return True
    window.append(now)
    return False


def _sessions_today(student: Student, mode: str) -> int:
    midnight = datetime.datetime.combine(datetime.date.today(), datetime.time.min)
    return (Session
            .select()
            .where((Session.student == student)
                   & (Session.mode == mode)
                   & (Session.started_at >= midnight))
            .count())


def _current_engine() -> SessionEngine | None:
    session_id = cookie.get("session_id")
    if not session_id:
        return None
    engine = SessionEngine.load(session_id)
    # A cookie from a different browser or a deleted session should not let
    # someone drive an interview that is not theirs.
    if engine and engine.session.student.handle != cookie.get("handle"):
        return None
    return engine


@app.before_request
def _open_db():
    init_db(DB_PATH, create=False)


@app.teardown_request
def _close_db(exc):
    close_db()


# --- pages ------------------------------------------------------------------
@app.route("/")
def index():
    units = []
    for unit in Unit.select().order_by(Unit.number):
        units.append({
            "number": unit.number, "title": unit.title,
            "week_range": unit.week_range,
            "problems": [{"slug": p.slug, "title": p.title}
                         for p in unit.problems.where(Problem.active == True)],  # noqa: E712
        })
    return render_template("index.html", units=units, handle=cookie.get("handle", ""),
                           modes=MODES)


@app.route("/start", methods=["POST"])
def start():
    handle = (request.form.get("handle") or "").strip()
    problem_slug = request.form.get("problem")
    mode = request.form.get("mode", "drill")

    if not handle or not problem_slug or mode not in MODES:
        return redirect(url_for("index"))

    student, _ = Student.get_or_create(handle=handle.lower())
    limit = DAILY_INTERVIEW_LIMIT if mode == "interview" else DAILY_DRILL_LIMIT
    if _sessions_today(student, mode) >= limit:
        return render_template("limit.html", mode=mode, limit=limit)

    engine = SessionEngine.start(handle, problem_slug, mode)
    cookie["session_id"] = engine.session.id
    cookie["handle"] = handle.lower()
    return redirect(url_for("interview"))


@app.route("/interview")
def interview():
    engine = _current_engine()
    if engine is None:
        return redirect(url_for("index"))
    if engine.is_over:
        return redirect(url_for("debrief", session_id=engine.session.id))
    return render_template("interview.html", state=engine.state())


@app.route("/debrief/<int:session_id>")
def debrief(session_id):
    session = Session.get_or_none(Session.id == session_id)
    if session is None or session.student.handle != cookie.get("handle"):
        return redirect(url_for("index"))

    scored = {r.dimension: r.score for r in session.rubric_scores}
    return render_template(
        "debrief.html", session=session, summary=session_summary(session),
        dimensions=RubricScore.DIMENSIONS, labels=RubricScore.LABELS, scored=scored,
    )


@app.route("/profile")
def profile():
    handle = cookie.get("handle")
    student = Student.get_or_none(Student.handle == handle) if handle else None
    if student is None:
        return redirect(url_for("index"))
    return render_template(
        "profile.html", student=student,
        profile=concept_profile(student), weakest=weakest_concepts(student),
        units=unit_progress(student),
    )


# --- json ---------------------------------------------------------------------
@app.route("/api/turn", methods=["POST"])
def api_turn():
    engine = _current_engine()
    if engine is None:
        return jsonify({"error": "no active session"}), 400

    if _rate_limited(f"turn:{cookie.get('handle')}"):
        return jsonify({"error": "Slow down a moment and try again."}), 429

    message = (request.json or {}).get("message", "").strip()
    if not message:
        return jsonify({"error": "empty message"}), 400
    if len(message) > 4000:
        return jsonify({"error": "That message is too long for one turn."}), 400

    try:
        return jsonify(engine.send(message))
    except RuntimeError as exc:
        # The proctor raises with the API's own message. Log it, do not leak it:
        # the body can carry account details the student has no business seeing.
        app.logger.error("proctor call failed: %s", exc)
        return jsonify({"error": "The proctor is unavailable. Try again shortly."}), 503


@app.route("/api/finish", methods=["POST"])
def api_finish():
    engine = _current_engine()
    if engine is None:
        return jsonify({"error": "no active session"}), 400
    payload = request.json or {}
    # Code arrives here rather than being stored turn by turn: it is only needed
    # at grading time, and technical competency cannot be scored without it.
    code = str(payload.get("code", ""))[:20000]
    engine.finish(payload.get("reason", "completed"), code=code)
    return jsonify({"redirect": url_for("debrief", session_id=engine.session.id)})


@app.route("/api/rubric", methods=["POST"])
def api_rubric():
    engine = _current_engine()
    if engine is None:
        return jsonify({"error": "no active session"}), 400
    payload = request.json or {}
    scores = {d: int(v) for d, v in payload.get("scores", {}).items()
              if d in RubricScore.DIMENSIONS and str(v).isdigit() and 0 <= int(v) <= 3}
    engine.record_rubric(scores)
    return jsonify({"saved": len(scores)})


@app.route("/api/state")
def api_state():
    """Polled by the timer. Deliberately cheap: no model call, one row read.

    The countdown in the browser is decoration. This is the clock that matters,
    because a client-side timer can be paused with devtools.
    """
    engine = _current_engine()
    if engine is None:
        return jsonify({"error": "no active session"}), 400
    return jsonify(engine.state())


def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("Set ANTHROPIC_API_KEY first.")
    if not os.path.exists(DB_PATH):
        raise SystemExit(f"No database at {DB_PATH}. Run: python -m tools.init_db")
    print(f"\n  Rung on http://127.0.0.1:5000   (database: {DB_PATH})\n")
    app.run(debug=True, port=5000)


if __name__ == "__main__":
    main()
