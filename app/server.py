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
import pathlib
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from zoneinfo import ZoneInfo

from flask import (
    Flask, jsonify, redirect, render_template, request, session as cookie, url_for,
)
from werkzeug.middleware.proxy_fix import ProxyFix

import rung.checks
from app.engine import SessionEngine
from rung.config import (
    ACCESS_CODE, COURSE_TIMEZONE, CURRENT_UNIT, DAILY_DRILL_LIMIT, DAILY_INTERVIEW_LIMIT,
    GLOBAL_DAILY_DRILLS, GLOBAL_DAILY_INTERVIEWS, INSTRUCTOR_CODE,
    MODES, ORIGIN_HEADER, ORIGIN_SECRET, PROXY_HOPS, PUBLIC_ORIGIN,
    REQUESTS_PER_MINUTE, STARTS_PER_HOUR_PER_IP,
)
from rung.diagnostics import (
    class_metrics, class_overview, concept_profile, problem_status,
    recommended_problems, session_summary, student_overview, weakest_concepts,
)
from rung.models import (
    Problem, RubricScore, Session, SessionFeedback, Student, Unit,
    add_missing_columns, close_db, database, init_db,
)
from rung.problems import PROBLEMS

# The check runner's source, handed to the browser's Python worker. The server
# never executes student code; see rung/checks.py.
CHECKS_SOURCE = pathlib.Path(rung.checks.__file__).read_text(encoding="utf-8")


class OriginGate:
    """Refuse requests that did not arrive through CloudFront.

    WSGI middleware rather than a before_request hook so it runs ahead of
    everything, including the per-request database open. /health stays open for
    a manual check and reveals nothing but "ok".
    """

    def __init__(self, wsgi_app, secret: str, header: str = ORIGIN_HEADER):
        self.wsgi_app = wsgi_app
        self.secret = secret.encode("utf-8")
        self.environ_key = "HTTP_" + header.upper().replace("-", "_")

    def __call__(self, environ, start_response):
        # WSGI decodes headers as latin-1, so this round-trips any byte a
        # client sends. compare_digest on str raises for non-ASCII input.
        supplied = environ.get(self.environ_key, "").encode("latin-1", "replace")
        if (environ.get("PATH_INFO") != "/health"
                and not secrets.compare_digest(supplied, self.secret)):
            start_response("403 Forbidden", [("Content-Type", "text/plain")])
            return [b"Forbidden\n"]
        return self.wsgi_app(environ, start_response)


# Static files live in public/static at the repository root, one copy for every
# host: Flask serves them locally and on Vercel (where vercel.json's
# includeFiles puts public/ into the function bundle, because in services mode
# every request reaches the app), and Elastic Beanstalk's nginx serves them
# directly.
PUBLIC_STATIC = pathlib.Path(__file__).resolve().parent.parent / "public" / "static"

app = Flask(__name__, static_folder=str(PUBLIC_STATIC), static_url_path="/static")
if PROXY_HOPS:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=PROXY_HOPS)
if ORIGIN_SECRET:
    app.wsgi_app = OriginGate(app.wsgi_app, ORIGIN_SECRET)

# True only when RUNG_ENV is explicitly "development". Defaulting the other way
# matters: Flask's debug mode ships an interactive console that executes
# arbitrary Python from the browser, so a debug server reachable from the
# internet is a remote shell. The default has to be the safe one.
# Running this module directly is an explicit development action. Importing it
# (as gunicorn and wsgi.py do) stays production-safe by default.
IS_DEV = os.environ.get(
    "RUNG_ENV", "development" if __name__ == "__main__" else "production"
) == "development"

# Signs the session cookie, which holds only a session id and a handle.
# In production a missing key is fatal rather than random: a random key would
# work fine on one worker and silently log everyone out on a restart or a second
# worker, which is a confusing bug to chase. Locally, random is convenient.
_secret = os.environ.get("RUNG_SECRET_KEY")
if not _secret:
    if not IS_DEV:
        raise RuntimeError(
            "RUNG_SECRET_KEY must be set in production. "
            "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\"")
    _secret = os.urandom(32)
app.secret_key = _secret

# Accepts a file path or a database URL. See rung/models.init_db.
DB_PATH = os.environ.get("DATABASE_URL") or os.environ.get("RUNG_DB", "data/rung.db")

# Cookies are not readable from JavaScript. Secure is on in production, where
# the host terminates TLS; leaving it on locally would stop the cookie being
# set over plain http and make the app look broken.
#
# The handle cookie set in /start is marked permanent so a returning student
# is recognized across visits instead of retyping their handle every time;
# PERMANENT_SESSION_LIFETIME caps how long that convenience lasts. The
# instructor cookie deliberately opts back out of this in instructor_login,
# since an elevated-access cookie should not outlive the browser session.
app.config.update(
    MAX_CONTENT_LENGTH=64 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=not IS_DEV,
    PERMANENT_SESSION_LIFETIME=datetime.timedelta(days=90),
)

# In-process rate limiting. Correct for one worker, which is what this runs as.
# Multiple workers would each keep their own counter, so a real deployment wants
# this in the database or a shared store. Noted rather than solved: the fix is
# easy and the wrong fix is pretending one process is enough forever.
_recent_requests = defaultdict(deque)


@app.after_request
def _security_headers(response):
    """Browser protections for a page that handles student work."""
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline'; "
        "connect-src 'self' https://cdn.jsdelivr.net; "
        # Pyodide starts a helper worker from its own CDN URL. script-src
        # already trusts that host, so allowing it here adds no new exposure;
        # without it the browser blocks the helper and the runner never loads.
        "worker-src 'self' https://cdn.jsdelivr.net; img-src 'self' data:; font-src 'self'; "
        "object-src 'none'; base-uri 'self'; frame-ancestors 'none'",
    )
    if not IS_DEV:
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    if request.path.startswith(("/interview", "/debrief", "/profile", "/instructor")):
        response.headers.setdefault("Cache-Control", "private, no-store")
    return response


def _rate_limited(key: str, limit: int = REQUESTS_PER_MINUTE, period: float = 60) -> bool:
    now = time.monotonic()
    window = _recent_requests[key]
    while window and now - window[0] > period:
        window.popleft()
    if len(window) >= limit:
        return True
    window.append(now)
    return False


def _course_midnight() -> datetime.datetime:
    """Midnight today in the course's timezone, as naive server-local time.

    started_at is stored as naive server-local time. Elastic Beanstalk sets the
    server to Eastern, Vercel runs in UTC, and a laptop is wherever it is, so
    "today" is worked out in COURSE_TIMEZONE and converted back.
    """
    now = datetime.datetime.now(ZoneInfo(COURSE_TIMEZONE))
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone().replace(tzinfo=None)


def _sessions_today(mode: str, student: Student | None = None) -> int:
    """Sessions of this mode started since midnight: one student's, or everyone's."""
    query = Session.select().where((Session.mode == mode)
                                   & (Session.started_at >= _course_midnight()))
    if student is not None:
        query = query.where(Session.student == student)
    return query.count()


# Wrong class codes allowed per address per hour before guessing is refused.
ACCESS_FAILURES_PER_HOUR = 30


def _access_required() -> bool:
    return bool(ACCESS_CODE) and cookie.get("class_ok") is not True


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


def _problem_is_open(problem: Problem) -> bool:
    return problem.active and (CURRENT_UNIT is None or problem.unit.number <= CURRENT_UNIT)


_CHALLENGE_LABELS = {
    "warmup": "Warm-up",
    "core": "Core",
    "stretch": "Stretch",
}


def _problem_challenge(slug: str, unit_number: int) -> dict:
    """Return an instructor-authored, course-relative challenge label.

    Older/private banks do not need an immediate migration: their unit supplies
    a conservative fallback until the instructor adds a per-problem value.
    """
    raw = str(PROBLEMS.get(slug, {}).get("challenge", "")).strip().lower()
    key = raw.replace("-", "").replace("_", "")
    if key not in _CHALLENGE_LABELS:
        key = "warmup" if unit_number <= 1 else "core" if unit_number <= 3 else "stretch"
    return {"key": key, "label": _CHALLENGE_LABELS[key]}


# Difficulty order for sorting and the three-step meter. The labels stay
# course-relative (see DECISIONS.md): "Warm-up" rather than "Easy".
_CHALLENGE_RANK = {"warmup": 1, "core": 2, "stretch": 3}


def _problem_topic(slug: str, unit_title: str) -> str:
    """The subject or data structure a problem is listed under.

    A bank without topics falls back to unit titles, which is how problems
    were grouped before topics existed, so an older private bank still works.
    """
    return str(PROBLEMS.get(slug, {}).get("topic") or unit_title).strip()


def _decorate_recommendations(items: list[dict]) -> list[dict]:
    for item in items:
        challenge = _problem_challenge(item["slug"], item["unit_number"])
        item["challenge"] = challenge["key"]
        item["challenge_label"] = challenge["label"]
        item["difficulty"] = _CHALLENGE_RANK[challenge["key"]]
        item["topic"] = _problem_topic(item["slug"], item["unit_title"])
    return items


def _catalog(student: Student | None) -> list[dict]:
    """Every active problem, where it sits in the course, and this student's
    progress on it. One query for the problems and three for the progress,
    however many problems the bank holds."""
    status = problem_status(student)
    items = []
    query = (Problem
             .select(Problem, Unit)
             .join(Unit)
             .where(Problem.active == True)  # noqa: E712
             .order_by(Unit.number, Problem.id))
    for problem in query:
        unit = problem.unit
        challenge = _problem_challenge(problem.slug, unit.number)
        progress = status.get(problem.id, {})
        items.append({
            "slug": problem.slug,
            "title": problem.title,
            "unit_number": unit.number,
            "unit_title": unit.title,
            "week_range": unit.week_range,
            "topic": _problem_topic(problem.slug, unit.title),
            "available": CURRENT_UNIT is None or unit.number <= CURRENT_UNIT,
            "challenge": challenge["key"],
            "challenge_label": challenge["label"],
            "difficulty": _CHALLENGE_RANK[challenge["key"]],
            "checks": len(PROBLEMS.get(problem.slug, {}).get("tests") or []),
            "practiced": progress.get("practiced", False),
            "solved": progress.get("solved", False),
            "best_depth": progress.get("best_depth"),
            "turns": progress.get("turns", 0),
            "rung_total": progress.get("rung_total", 0),
        })
    return items


def _group(items: list[dict], by: str) -> list[dict]:
    """Bucket catalog items by course unit or by topic, with progress per group.

    Unit groups keep the bank's authored order. Topic groups list the easiest
    problems first and are ordered by where their problems sit in the course,
    so Strings comes before Dictionaries without a hand-maintained list.
    """
    buckets = {}
    for item in items:
        buckets.setdefault(item["unit_number"] if by == "unit" else item["topic"], []).append(item)

    groups = []
    for key, problems in buckets.items():
        units = sorted({p["unit_number"] for p in problems})
        if by == "unit":
            title, tag = problems[0]["unit_title"], f"Unit {key}"
        else:
            problems.sort(key=lambda p: (p["difficulty"], p["unit_number"]))
            title = key
            tag = f"Unit {units[0]}" if len(units) == 1 else f"Units {units[0]}–{units[-1]}"
        turns = sum(p["turns"] for p in problems)
        practiced = sum(p["practiced"] for p in problems)
        groups.append({
            "title": title,
            "tag": tag,
            "week_range": problems[0]["week_range"] if by == "unit" else None,
            "available": any(p["available"] for p in problems),
            "is_current": by == "unit" and CURRENT_UNIT is not None and key == CURRENT_UNIT,
            "problems": problems,
            "problem_count": len(problems),
            "practiced_count": practiced,
            "solved_count": sum(p["solved"] for p in problems),
            "checked_count": sum(1 for p in problems if p["checks"]),
            "progress_pct": round(practiced / len(problems) * 100),
            "avg_rung": round(sum(p["rung_total"] for p in problems) / turns, 2) if turns else None,
            "position": sum(p["unit_number"] for p in problems) / len(problems),
        })

    if by == "topic":
        groups.sort(key=lambda g: (g["position"], g["title"]))
    first_open = next((i for i, g in enumerate(groups) if g["available"]), 0)
    for index, group in enumerate(groups):
        if by == "unit":
            group["open"] = group["is_current"] or (CURRENT_UNIT is None and index == 0)
        else:
            group["open"] = index == first_open
    return groups


def _call_repr(slug: str, prompt: str, given) -> str:
    """How a check's input reads as a call, for the debrief.

    The bank stores several arguments as one list, and so does a single list
    argument, so the parameter list in the prompt's own signature decides.
    """
    match = re.search(rf"\b{re.escape(slug)}\(([^)]*)\)", prompt)
    params = [p for p in match.group(1).split(",") if p.strip()] if match else []
    if len(params) > 1 and isinstance(given, list) and len(given) == len(params):
        return f"{slug}({', '.join(repr(arg) for arg in given)})"
    return f"{slug}({given!r})"


def _instructor_signed_in() -> bool:
    return bool(INSTRUCTOR_CODE and cookie.get("instructor") is True)


@app.context_processor
def _navigation_state():
    return {
        "instructor_enabled": bool(INSTRUCTOR_CODE),
        "instructor_signed_in": _instructor_signed_in(),
        "public_origin": PUBLIC_ORIGIN,
    }


_db_lock = threading.Lock()


@app.before_request
def _open_db():
    # Choose the backend once per process, then open a connection per request.
    # Rebuilding the backend on every request, as this used to, let two threads
    # swap it out from under each other's open connections.
    if database.obj is None:
        with _db_lock:
            if database.obj is None:
                init_db(DB_PATH, create=False)
    database.connect(reuse_if_open=True)


@app.teardown_request
def _close_db(exc):
    close_db()


# --- pages ------------------------------------------------------------------
@app.route("/")
def index():
    handle = cookie.get("handle", "")
    student = Student.get_or_none(Student.handle == handle) if handle else None
    view = "unit" if request.args.get("view") == "unit" else "topic"

    catalog = _catalog(student)
    available = [item for item in catalog if item["available"]]
    practiced = sum(item["practiced"] for item in available)
    course_progress = {
        "practiced": practiced,
        "solved": sum(item["solved"] for item in available),
        "total": len(available),
        "pct": round(practiced / len(available) * 100) if available else 0,
    }
    recommendations = (
        _decorate_recommendations(recommended_problems(
            student, max_unit=CURRENT_UNIT)) if student else []
    )
    return render_template(
        "index.html", groups=_group(catalog, view), view=view, handle=handle,
        modes=MODES, current_unit=CURRENT_UNIT, course_progress=course_progress,
        recommendations=recommendations,
        access_required=_access_required(),
        access_error=request.args.get("code") == "invalid",
    )


@app.route("/start", methods=["POST"])
def start():
    handle = (request.form.get("handle") or "").strip()
    problem_slug = request.form.get("problem")
    mode = request.form.get("mode", "drill")

    if not handle or not problem_slug or mode not in MODES:
        return redirect(url_for("index"))

    # The class code, once per browser. Only wrong guesses are counted, so a
    # lab full of students behind one address can all enter it at once, while
    # guessing is capped per address.
    if _access_required():
        supplied = (request.form.get("access_code") or "").strip()
        if not secrets.compare_digest(supplied.encode("utf-8"), ACCESS_CODE.encode("utf-8")):
            if _rate_limited(f"access:{request.remote_addr or 'unknown'}",
                             limit=ACCESS_FAILURES_PER_HOUR, period=3600):
                return "Too many wrong class codes from this address. Try again later.", 429
            return redirect(url_for("index", code="invalid") + "#practice")
        cookie.permanent = True
        cookie["class_ok"] = True

    # Per-student daily caps only bite once a handle exists; this bites before
    # one does, so rotating handles cannot be used to dodge them.
    start_key = f"start:{request.remote_addr or 'unknown'}"
    if _rate_limited(start_key, limit=STARTS_PER_HOUR_PER_IP, period=3600):
        return "Too many sessions started from this address recently. Try again later.", 429

    problem = Problem.get_or_none(Problem.slug == problem_slug)
    if problem is None or not _problem_is_open(problem):
        return redirect(url_for("index"))

    student, _ = Student.get_or_create(handle=handle.lower())
    limit = DAILY_INTERVIEW_LIMIT if mode == "interview" else DAILY_DRILL_LIMIT
    if _sessions_today(mode, student) >= limit:
        return render_template("limit.html", mode=mode, limit=limit)

    # The ceiling no handle, address or number of server copies gets around.
    site_limit = GLOBAL_DAILY_INTERVIEWS if mode == "interview" else GLOBAL_DAILY_DRILLS
    if _sessions_today(mode) >= site_limit:
        app.logger.warning("site-wide daily %s cap of %s reached", mode, site_limit)
        return render_template("limit.html", mode=mode, limit=site_limit, site_wide=True), 429

    engine = SessionEngine.start(handle, problem_slug, mode)
    cookie.permanent = True
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
    # The checks' expected outputs reach the browser because that is where the
    # code runs. They are examples of the specification, not the insight; the
    # forbidden insight still never leaves the server.
    return render_template(
        "interview.html", state=engine.state(),
        checks={"function": engine.session.problem.slug, "tests": engine.check_tests()},
        harness=CHECKS_SOURCE,
    )


@app.route("/debrief/<int:session_id>")
def debrief(session_id):
    session = Session.get_or_none(Session.id == session_id)
    if session is None or session.student.handle != cookie.get("handle"):
        return redirect(url_for("index"))

    self_scores = {
        r.dimension: r for r in session.rubric_scores.where(RubricScore.source == "self")}
    proctor_scores = {
        r.dimension: r for r in
        session.rubric_scores.where(RubricScore.source == "proctor")}
    feedback = SessionFeedback.get_or_none(SessionFeedback.session == session)

    # Which checks failed is withheld while the session runs, because finding
    # the breaking edge case is the student's job. Once it is over, name them.
    tests = PROBLEMS.get(session.problem.slug, {}).get("tests") or []
    checks = None
    if tests:
        results = session.check_results
        checks = {"total": len(tests), "ran": results is not None,
                  "passed": (results or "").count("1"), "failed": []}
        if results and len(results) == len(tests):
            checks["failed"] = [
                {"call": _call_repr(session.problem.slug, session.problem.prompt, given),
                 "expected": repr(expected)}
                for (given, expected), result in zip(tests, results) if result == "0"
            ]
    return render_template(
        "debrief.html", session=session, summary=session_summary(session),
        dimensions=RubricScore.DIMENSIONS, labels=RubricScore.LABELS,
        self_scores=self_scores, proctor=proctor_scores, feedback=feedback,
        has_self_assessment=len(self_scores) == len(RubricScore.DIMENSIONS),
        checks=checks,
    )


@app.route("/profile")
def profile():
    handle = cookie.get("handle")
    student = Student.get_or_none(Student.handle == handle) if handle else None
    if student is None:
        return redirect(url_for("index"))
    catalog = _catalog(student)
    solved = sorted((item for item in catalog if item["solved"]),
                    key=lambda item: (item["topic"], item["difficulty"], item["title"]))
    return render_template(
        "profile.html", student=student,
        profile=concept_profile(student), weakest=weakest_concepts(student),
        topics=_group(catalog, "topic"), solved=solved,
        practiced_count=sum(item["practiced"] for item in catalog),
        problem_count=len(catalog),
        recommendations=_decorate_recommendations(
            recommended_problems(student, max_unit=CURRENT_UNIT)),
    )


@app.route("/instructor/login", methods=["GET", "POST"])
def instructor_login():
    if not INSTRUCTOR_CODE:
        return render_template("instructor_login.html", disabled=True), 404
    error = None
    if request.method == "POST":
        key = f"instructor:{request.remote_addr or 'unknown'}"
        if _rate_limited(key):
            error = "Too many attempts. Wait a minute and try again."
        elif secrets.compare_digest(request.form.get("code", ""), INSTRUCTOR_CODE):
            # Opt back out of the 90-day handle cookie: elevated access should
            # not outlive the browser session even if this browser already
            # carries a permanent student handle cookie.
            cookie.permanent = False
            cookie["instructor"] = True
            return redirect(url_for("instructor"))
        else:
            error = "That instructor code is not valid."
    return render_template("instructor_login.html", disabled=False, error=error)


@app.route("/instructor")
def instructor():
    if not _instructor_signed_in():
        return redirect(url_for("instructor_login"))
    return render_template(
        "instructor.html", metrics=class_metrics(), concepts=class_overview(),
        students=student_overview(), current_unit=CURRENT_UNIT,
    )


@app.route("/instructor/logout", methods=["POST"])
def instructor_logout():
    cookie.pop("instructor", None)
    return redirect(url_for("index"))


@app.route("/health")
def health():
    database.execute_sql("SELECT 1")
    return jsonify({"status": "ok"})


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
        result = engine.send(message)
        if result.get("busy"):
            # Another turn for this session is still waiting on the model.
            # Refusing it is what keeps parallel requests from each being billed.
            return jsonify({"error": "Rung is still answering your last message."}), 429
        return jsonify(result)
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
    reason = payload.get("reason", "completed")
    if reason not in {"completed", "time_limit", "turn_limit", "token_budget"}:
        reason = "completed"
    # One boolean per bank check, from the browser's run on this final code.
    # The engine validates the shape and records it at most once.
    engine.finish(reason, code=code, checks=payload.get("checks"))
    return jsonify({"redirect": url_for("debrief", session_id=engine.session.id)})


@app.route("/api/rubric", methods=["POST"])
def api_rubric():
    engine = _current_engine()
    if engine is None:
        return jsonify({"error": "no active session"}), 400
    if not engine.is_over:
        return jsonify({"error": "finish the session before scoring it"}), 409
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


def ensure_database():
    """Create and seed the database if it is not there.

    Render's free tier has an ephemeral filesystem: the disk is wiped on every
    deploy, so a SQLite file cannot be created once and left alone. Seeding at
    startup makes a fresh container self-sufficient, and it is a no-op when the
    database already exists because the seeder is idempotent.

    The real consequence is that student data does not survive a deploy on the
    free tier. That is acceptable for a demo and unacceptable for a class, which
    is what DATABASE_URL is for: point it at Postgres and this stops mattering.
    """
    from rung.models import ALL_TABLES, close_db
    from tools.init_db import seed_concepts, seed_problems, seed_units

    db = init_db(DB_PATH, create=False)
    db.create_tables(ALL_TABLES)
    add_missing_columns()
    with db.atomic():
        seed_units()
        seed_concepts()
        seed_problems()
    close_db()


def ensure_schema():
    """Add any columns newer code expects, without seeding anything.

    One catalogue query when nothing is missing. Two cold starts can race to
    add the same column; the loser's ALTER fails because the column now exists,
    which is the outcome it wanted, so that failure is logged and ignored.
    """
    try:
        init_db(DB_PATH, create=False)
        add_missing_columns()
    except Exception:  # noqa: BLE001 - never stop the app booting over this
        app.logger.exception("schema check failed")
    finally:
        close_db()


def main():
    """Development server only. Production runs under gunicorn; see DEPLOY.md."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("Set ANTHROPIC_API_KEY first.")
    if not os.path.exists(DB_PATH) and "://" not in DB_PATH:
        raise SystemExit(f"No database at {DB_PATH}. Run: python -m tools.init_db")
    print(f"\n  Rung on http://127.0.0.1:5000   (database: {DB_PATH})\n")
    app.run(debug=True, port=5000)


if __name__ == "__main__":
    main()
