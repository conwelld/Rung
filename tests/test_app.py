"""
Flask and engine tests. No API key, no network, no cost.

    python -m tests.test_app

The proctor's API call is replaced with a stub, so every rule that makes this an
interview rather than a chat window gets exercised for free: the phase gate, the
turn cap, the rung ceiling, transcript retention, and the fact that a cookie
from one student cannot drive another student's session.

Testing these against the real API would be slow, expensive, and would test the
model rather than the code. The stub is the point.
"""

import os
import sys

# app/server.py defaults to production, where a missing secret key is fatal.
# Tests are development. Set this before importing anything that reads it.
os.environ.setdefault("RUNG_ENV", "development")

from rung.models import (Concept, Problem, ProblemConcept, RubricScore, Session,
                         SessionFeedback, Student, Turn, Unit, close_db, init_db)

failures = []


def check(label, condition):
    if condition:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}")
        failures.append(label)


# --- stub the model call before importing anything that uses it -------------
import rung.proctor as proctor_module

_calls = []


def fake_ask_proctor(problem, phase, history, student_message, mode="interview",
                     max_rung=None, api_key=None, timeout=60):
    from rung.config import get_mode
    settings = get_mode(mode)
    ceiling = settings["max_rung"] if max_rung is None else min(max_rung, settings["max_rung"])
    _calls.append({"phase": phase, "history_len": len(history), "message": student_message})
    return {
        "rung_used": 1,
        # Always asks to advance, so the server-side gate is what is under test.
        "advance_phase": True,
        "reply": f"[stub reply in {phase}]",
        "rung_ceiling": ceiling,
        "parse_ok": True,
        "raw": "",
        "usage": {"input_tokens": 900, "output_tokens": 120},
        "model": settings["model"],
    }


import app.engine as engine_module
engine_module.ask_proctor = fake_ask_proctor

_graded = []


def fake_grade_session(session, turns, code="", api_key=None):
    """Stub grader. Whether the real grader is calibrated is a separate
    question, answered by tools/check_grader.py against the live API. What is
    under test here is that grading runs at the right moment, writes the right
    rows, and that a failure does not break the session."""
    _graded.append({"session": session.id, "turns": len(turns), "code": code})
    return {
        "communication": {"score": 2, "note": "Clear enough, but terse."},
        "problem_solving": {"score": 1, "note": "Needed the structural hint."},
        "technical_competency": {"score": None if not code else 2, "note": "n"},
        "debugging": {"score": 0, "note": "No debugging happened."},
        "overall": "Work on stating your plan before coding.",
    }


engine_module.grade_session = fake_grade_session
from app.engine import MIN_APPROACH_CHARS, PHASE_MINIMUMS, SessionEngine
import app.server as server_module

# The same checks run against Postgres when RUNG_TEST_DATABASE_URL names a
# disposable database, which is how the Supabase path is tested. Every table is
# dropped first, so never point this at a database you want to keep.
TEST_DATABASE = os.environ.get("RUNG_TEST_DATABASE_URL")
if TEST_DATABASE:
    from rung.models import ALL_TABLES, database
    init_db(TEST_DATABASE, create=False)
    database.drop_tables(ALL_TABLES, safe=True, cascade=True)
    database.create_tables(ALL_TABLES)
else:
    init_db(":memory:")

# Flask registers before/teardown hooks at import, so reassigning the module
# attribute does nothing: the app already holds a reference to the original
# function. Clear the registrations instead. The hooks exist to open and close a
# file-backed database per request; an in-memory one lives for the process and
# reopening it would hand every request a fresh empty database.
server_module.app.before_request_funcs.clear()
server_module.app.teardown_request_funcs.clear()
server_module.DB_PATH = TEST_DATABASE or ":memory:"

unit = Unit.create(number=1, title="Functions")
concept = Concept.create(slug="loops", title="loops")
problem = Problem.create(slug="p1", title="P1", unit=unit,
                         prompt="Write a thing.", forbidden_insight="Use a loop.")
ProblemConcept.create(problem=problem, concept=concept)
later_unit = Unit.create(number=2, title="Collections")
later_problem = Problem.create(slug="p2", title="P2", unit=later_unit,
                               prompt="Write a later thing.",
                               forbidden_insight="Use a mapping.")
ProblemConcept.create(problem=later_problem, concept=concept)

print("\nphase gate")
e = SessionEngine.start("alice", "p1", "interview")
check("starts in the first phase", e.phase == "CLARIFY")
check("editor locked at the start", not e.editor_unlocked())

e.send("Is the input always a string?")
check("advances out of CLARIFY after its minimum", e.phase == "APPROACH")
check("editor still locked in APPROACH", not e.editor_unlocked())

# A model that always says advance_phase must not be able to wave a one-word
# "plan" through. This is the rule a prompt cannot enforce.
e.send("idk")
check("short non-answer does not leave APPROACH", e.phase == "APPROACH")

e.send("I will loop over the characters and count the ones that are letters, "
       "tracking a running total as I go.")
check("a real stated plan advances to CODE", e.phase == "CODE")
check("editor unlocks in CODE", e.editor_unlocked())

check("APPROACH minimum is more than one turn", PHASE_MINIMUMS["APPROACH"] >= 2)
check("approach length floor is meaningful", MIN_APPROACH_CHARS >= 40)

print("\npersistence")
turns = list(e.turns())
check("every exchange stored a turn", len(turns) == 3)
check("ordinals are sequential", [t.ordinal for t in turns] == [1, 2, 3])
check("rung recorded", all(t.rung_used == 1 for t in turns))
check("phase recorded per turn", turns[0].phase == "CLARIFY")
check("advance flag recorded", turns[0].advanced_phase is True)
# Text IS stored while a session runs: it is the proctor's only memory of the
# conversation, and the grader reads it at the end. The purge happens in
# finish(), which is asserted separately below.
check("transcript held during the session",
      all(t.student_text and t.proctor_text for t in turns))
check("tokens accumulated on the session", e.session.input_tokens == 3 * 900)

print("\nrung ceiling per mode")
d = SessionEngine.start("bob", "p1", "drill")
check("drill starts in APPROACH (no CLARIFY)", d.phase == "APPROACH")
result = d.send("I will iterate through and keep a counter of what I find as I go along.")
check("drill ceiling capped at 2", list(d.turns())[0].rung_ceiling == 2)
check("drill never offers rung 3+", list(d.turns())[0].rung_ceiling < 3)

print("\nbudget enforcement")
b = SessionEngine.start("carol", "p1", "drill")
for i in range(20):
    out = b.send(f"Message number {i} with enough length to be a real attempt at explaining.")
    if out.get("ended"):
        break
check("turn cap ends the session", b.is_over)
check("stopped for a recorded reason", b.session.stopped_because in
      ("turn_limit", "token_budget", "time_limit"))
check("no turns beyond the cap", b.turns().count() <= b.mode["max_turns"])
check("a finished session refuses more turns", b.send("hello").get("ended") is True)

# The budget is rebuilt from the database, not held in memory, so reloading the
# session in a new request must not reset it.
reloaded = SessionEngine.load(b.session.id)
check("limit survives a reload", reloaded.send("try again").get("ended") is True)

print("\nproctor grading")
# Earlier sessions in this file also call finish(), which grades. Clear the
# recorder so the assertions below are about this session only.
_graded.clear()
g = SessionEngine.start("grace", "p1", "interview")
g.send("Does the input ever contain digits?")
g.send("I will walk the string once and count the alphabetic characters as I go.")
g.send("Writing the loop now, tracking a counter as I move through it.")
check("transcript stored during the session",
      all(t.student_text for t in g.turns()))
check("proctor sees real history", len(g.history()) == 6)

grades = g.finish("completed", code="def f(w): return 1")
check("grading ran on finish", len(_graded) == 1)
check("code reached the grader", _graded[0]["code"].startswith("def f"))
check("grader saw every turn", _graded[0]["turns"] == 3)

proctor_rows = g.grades("proctor")
check("proctor scores written", len(proctor_rows) == 4)
check("proctor score recorded", proctor_rows["communication"].score == 2)
check("proctor note recorded", "terse" in proctor_rows["communication"].note)
feedback = SessionFeedback.get_or_none(SessionFeedback.session == g.session)
check("actionable session feedback recorded",
      feedback is not None and "stating your plan" in feedback.summary)
# With no code, the stub returns null for technical competency, and a null
# score must not become a row: an absent grade is different from a zero.
_graded.clear()
n = SessionEngine.start("nora", "p1", "drill")
n.send("I will step through the input and tally what I find as I go along here.")
n.finish("completed")
check("no code means no technical competency row",
      "technical_competency" not in n.grades("proctor"))
check("other dimensions still scored without code", len(n.grades("proctor")) == 3)

print("\ntranscript purge")
check("transcript purged after grading",
      all(t.student_text is None and t.proctor_text is None for t in g.turns()))
check("rung data survives the purge",
      all(t.rung_used is not None for t in g.turns()))
check("token counts survive the purge",
      all(t.input_tokens > 0 for t in g.turns()))
check("phase survives the purge", all(t.phase for t in g.turns()))

print("\nself scores sit alongside proctor scores")
g.record_rubric({"communication": 3, "problem_solving": 3}, source="self")
self_rows = g.grades("self")
check("self scores stored separately", len(self_rows) == 2)
check("both sources coexist for one dimension",
      g.grades("self")["communication"].score == 3
      and g.grades("proctor")["communication"].score == 2)
g.record_rubric({"communication": 1}, source="self")
check("rescoring updates rather than duplicating",
      g.grades("self")["communication"].score == 1
      and g.session.rubric_scores.where(
          (RubricScore.dimension == "communication")).count() == 2)

print("\ngrading failure does not break the session")
def exploding_grader(session, turns, code="", api_key=None):
    raise RuntimeError("grader unavailable")
engine_module.grade_session = exploding_grader
h = SessionEngine.start("heidi", "p1", "drill")
h.send("I will iterate over the input and keep a running count as I go along.")
h.finish("completed")
check("session still closes when grading fails", h.is_over)
check("transcript still purged when grading fails",
      all(t.student_text is None for t in h.turns()))
check("no proctor scores when grading fails", len(h.grades("proctor")) == 0)
engine_module.grade_session = fake_grade_session

print("\nhttp layer")
server_module.app.config["TESTING"] = True
client = server_module.app.test_client()

r = client.get("/")
check("problem picker renders", r.status_code == 200 and b"Pick a problem" in r.data)
check("picker uses full-width rows without a phantom grid cell",
      b'class="problem-list"' in r.data and b'class="problem-grid"' not in r.data)
check("clicking a problem is the start action",
      b'class="problem-choice" type="submit" name="problem"' in r.data
      and b'type="radio" name="problem"' not in r.data
      and b">Start session</button>" not in r.data)
check("large curriculum sections are collapsible",
      b'<details class="unit-block" open>' in r.data
      and b'<summary class="unit-head">' in r.data
      and b'class="when-closed">View' in r.data
      and b'class="when-open">Hide' in r.data
      and b'class="unit-chevron"' not in r.data)
check("picker frames challenge relative to the course",
      b"Warm-up" in r.data and b"Core" in r.data)
check("home navigation exposes real destinations instead of implying a drawer",
      b'href="#practice"' in r.data and b'href="#how-it-works"' in r.data)
check("handle requirement is conveyed with text, help, and an alert",
      b'class="required-text">Required' in r.data
      and b'id="handle-help"' in r.data
      and b'id="handle-error" role="alert" hidden' in r.data
      and b'aria-describedby="handle-help handle-error"' in r.data
      and b'addEventListener("invalid"' in r.data)
check("hero states one core idea without button-like claim pills",
      b"Core idea" in r.data and b"smallest useful hint" in r.data
      and b'class="hero-badges"' not in r.data)
check("hint ladder explains its levels to a first-time visitor",
      b"Hints, one step at a time." in r.data
      and b"Ask a question" in r.data
      and b"Name the idea, never the code" in r.data)
check("homepage uses one explanation instead of a redundant lower section",
      b'id="how-it-works"' in r.data
      and b"Three habits, practiced on purpose" not in r.data
      and b"Designed for the actual rubric" not in r.data)

r = client.get("/interview")
check("no session redirects home", r.status_code == 302)

r = client.post("/api/turn", json={"message": "hi"})
check("turn without a session is rejected", r.status_code == 400)

r = client.post("/start", data={"handle": "dave", "problem": "p1", "mode": "drill"})
check("start redirects into the interview", r.status_code == 302)

r = client.get("/interview")
check("interview page renders", r.status_code == 200)
check("problem prompt is on the page", b"Write a thing." in r.data)
check("forbidden insight is NOT sent to the browser", b"Use a loop." not in r.data)

r = client.post("/api/turn", json={"message": ""})
check("empty message rejected", r.status_code == 400)

r = client.post("/api/turn", json={"message": "x" * 5000})
check("oversized message rejected", r.status_code == 400)

r = client.post("/api/turn", json={"message": "What should this return when empty?"})
check("a real turn succeeds", r.status_code == 200)
check("reply returned to the browser", b"stub reply" in r.data)

r = client.get("/api/state")
check("state endpoint works", r.status_code == 200 and b"remaining_seconds" in r.data)

r = client.get("/health")
check("health endpoint checks the database", r.status_code == 200 and b'"ok"' in r.data)

print("\ndebrief contract")
r = client.post("/api/finish", json={"reason": "completed", "code": "def f(): return 1"})
check("finishing returns a debrief location", r.status_code == 200 and r.json.get("redirect"))
debrief_path = r.json["redirect"]
r = client.get(debrief_path)
check("self-assessment is shown before proctor feedback",
      b"Score yourself before Rung weighs in" in r.data and b"Clear enough" not in r.data)
r = client.post("/api/rubric", json={"scores": {
    "communication": 2, "problem_solving": 1,
    "technical_competency": 2, "debugging": 1,
}})
check("complete self-assessment is saved", r.status_code == 200 and r.json["saved"] == 4)
r = client.get(debrief_path)
check("proctor feedback appears after self-assessment",
      b"Your read and the proctor" in r.data and b"Clear enough" in r.data)
check("overall next step appears in the debrief", b"stating your plan" in r.data)

r = client.get("/")
check("finished reps update picker progress",
      b"1/2 available problems practiced" in r.data and b"1/1 practiced" in r.data)
check("picker explains that practice progress is not mastery",
      b"Finished reps, not a mastery score." in r.data)

print("\ntemplate integrity")
# Jinja discards anything a child template puts outside a block, with no error
# and no warning. That shipped once: the interview page rendered perfectly and
# had zero JavaScript, so the timer never started and Send did nothing. These
# assert the behaviour, not the file layout, because the file looked fine.
template_client = server_module.app.test_client()
template_client.post("/start", data={
    "handle": "template-student", "problem": "p1", "mode": "interview"})
r = template_client.get("/interview")
page = r.data.decode()
check("interview page has a script tag", "<script>" in page)
check("session state is injected into the page", "var state = " in page)
check("timer function reached the page", "function tick" in page)
check("send handler reached the page", "send.addEventListener" in page)
check("browser Python worker reached the page", "new Worker" in page and "Run code" in page)
check("security policy reaches student pages", "Content-Security-Policy" in r.headers)
check("no unrendered jinja tags leaked", "{%" not in page and "{{" not in page)
_css = template_client.get("/static/rung.css")
_worker = template_client.get("/static/code-runner-worker.js")
check("static files are served from public/static",
      _css.status_code == 200 and _worker.status_code == 200)
check("the module worker is served as JavaScript",
      "javascript" in _worker.headers.get("Content-Type", ""))

# The other templates go through real requests. url_for needs a request
# context, and hitting the route is closer to what actually happens than
# render_template with hand-built arguments would be.
for label, path in [("problem picker", "/"),
                    ("debrief", debrief_path),
                    ("profile", "/profile")]:
    resp = client.get(path, follow_redirects=True)
    body = resp.data.decode()
    check(f"{label} renders cleanly",
          resp.status_code == 200 and "{%" not in body and "{{" not in body)

print("\nsession ownership")
with client.session_transaction() as cookie:
    stolen = cookie["session_id"]
other = server_module.app.test_client()
with other.session_transaction() as cookie:
    cookie["session_id"] = stolen
    cookie["handle"] = "someone-else"
r = other.post("/api/turn", json={"message": "let me in"})
check("another handle cannot drive the session", r.status_code == 400)

print("\ncurriculum gate")
server_module.CURRENT_UNIT = 1
gate_client = server_module.app.test_client()
r = gate_client.post("/start", data={
    "handle": "late-student", "problem": "p2", "mode": "drill"})
check("future-unit problem cannot be started", r.status_code == 302)
check("future-unit post creates no session",
      not Session.select().join(Student).where(Student.handle == "late-student").exists())
server_module.CURRENT_UNIT = None

print("\ninstructor access")
server_module.INSTRUCTOR_CODE = "teacher-test-code"
instructor_client = server_module.app.test_client()
r = instructor_client.get("/instructor")
check("instructor dashboard requires a sign-in", r.status_code == 302)
r = instructor_client.post("/instructor/login", data={"code": "wrong"})
check("wrong instructor code is rejected", b"not valid" in r.data)
r = instructor_client.post("/instructor/login", data={"code": "teacher-test-code"})
check("instructor code opens the class view", r.status_code == 302)
r = instructor_client.get("/instructor")
check("class aggregates render behind instructor access",
      r.status_code == 200 and b"Class signals" in r.data and b"Hardest concepts" in r.data)
server_module.INSTRUCTOR_CODE = ""

print("\ndaily quota")
import datetime
from rung.config import DAILY_DRILL_LIMIT
_now = datetime.datetime.now()
_midnight = server_module._course_midnight()
check("the daily window starts at a midnight within the last day",
      _now - datetime.timedelta(days=1) < _midnight <= _now)
quota_client = server_module.app.test_client()
# Distinct REMOTE_ADDR per request: this is exercising the per-handle daily
# cap, not the per-IP starts-per-hour guard added alongside it, and the two
# would otherwise collide well before DAILY_DRILL_LIMIT is reached.
for i in range(DAILY_DRILL_LIMIT):
    quota_client.post(
        "/start", data={"handle": "eve", "problem": "p1", "mode": "drill"},
        environ_overrides={"REMOTE_ADDR": f"10.0.0.{i}"})
r = quota_client.post(
    "/start", data={"handle": "eve", "problem": "p1", "mode": "drill"},
    environ_overrides={"REMOTE_ADDR": f"10.0.0.{DAILY_DRILL_LIMIT}"})
check("daily drill cap is enforced", b"enough for today" in r.data)

print("\nrate limiting")
server_module._recent_requests.clear()
limited = False
rate_client = server_module.app.test_client()
rate_client.post("/start", data={"handle": "frank", "problem": "p1", "mode": "interview"})
for _ in range(server_module.REQUESTS_PER_MINUTE + 3):
    r = rate_client.post("/api/turn", json={"message": "a message of a reasonable length here"})
    if r.status_code == 429:
        limited = True
        break
check("rate limit fires", limited)

print("\nstarts per IP")
server_module._recent_requests.clear()
starts_client = server_module.app.test_client()
different_handles_same_ip = None
for i in range(server_module.STARTS_PER_HOUR_PER_IP + 2):
    different_handles_same_ip = starts_client.post(
        "/start", data={"handle": f"rotating-{i}", "problem": "p1", "mode": "drill"},
        environ_overrides={"REMOTE_ADDR": "203.0.113.9"})
    if different_handles_same_ip.status_code == 429:
        break
check("rotating handles from one IP still hits the starts cap",
      different_handles_same_ip.status_code == 429)

print("\nsolved tracking")
# A problem the loaded bank has checks for. Created only now, because the
# picker-progress assertions above count exactly two problems.
server_module._recent_requests.clear()
checked = Problem.create(slug="count_letters", title="count_letters", unit=unit,
                         prompt="Write a function count_letters(word) that counts letters.",
                         forbidden_insight="Walk the characters and test each one.")
ProblemConcept.create(problem=checked, concept=concept)
bank_checks = len(server_module.PROBLEMS["count_letters"]["tests"])


def session_for(handle):
    return (Session.select().join(Student)
            .where(Student.handle == handle).order_by(Session.id.desc()).get())


def started(handle, address):
    client_ = server_module.app.test_client()
    client_.post("/start", data={"handle": handle, "problem": "count_letters", "mode": "drill"},
                 environ_overrides={"REMOTE_ADDR": address})
    return client_


solver = started("solver", "198.51.100.1")
page = solver.get("/interview").data.decode()
check("interview page carries the problem's checks",
      '"function": "count_letters"' in page and "Run checks" in page)
check("the check harness reaches the page", "run_checks_json" in page)
check("the forbidden insight is still withheld", "Walk the characters and test each one." not in page)

solver.post("/api/finish", json={"reason": "completed", "code": "def count_letters(w): ...",
                                 "checks": [True] * bank_checks})
solved_session = session_for("solver")
check("passing every check records the problem as solved",
      solved_session.solved is True and solved_session.check_results == "1" * bank_checks)
solver.post("/api/finish", json={"reason": "completed", "checks": [False] * bank_checks})
check("the first check report wins", session_for("solver").solved is True)

for label, report in [("a report of the wrong length", [True]),
                      ("a report that is not booleans", ["yes"] * bank_checks),
                      ("a report that is not a list", "all passed")]:
    liar = started(f"liar-{len(str(report))}", f"198.51.100.{10 + len(str(report)) % 200}")
    liar.post("/api/finish", json={"reason": "completed", "checks": report})
    record = session_for(f"liar-{len(str(report))}")
    check(f"{label} is ignored", record.solved is None and record.check_results is None)

checkless = server_module.app.test_client()
checkless.post("/start", data={"handle": "nochecks", "problem": "p1", "mode": "drill"},
               environ_overrides={"REMOTE_ADDR": "198.51.100.40"})
checkless.post("/api/finish", json={"reason": "completed", "checks": [True]})
check("a problem without checks can never be marked solved",
      session_for("nochecks").solved is None)

partial = started("partial", "198.51.100.50")
partial.post("/api/finish", json={"reason": "completed", "code": "def count_letters(w): ...",
                                  "checks": [True] * (bank_checks - 1) + [False]})
check("a failing check records the attempt as not solved",
      session_for("partial").solved is False)
failing_input = server_module.PROBLEMS["count_letters"]["tests"][-1][0]
shown_call = f"count_letters({failing_input!r})".replace("'", "&#39;").encode()
debrief_url = f"/debrief/{session_for('partial').id}"
r = partial.get(debrief_url)
check("failing inputs stay hidden until the student self-assesses",
      shown_call not in r.data and b"checks passed" not in r.data)
partial.post("/api/rubric", json={"scores": {
    "communication": 1, "problem_solving": 1, "technical_competency": 1, "debugging": 1}})
r = partial.get(debrief_url)
check("the debrief names the failing input afterwards",
      f"{bank_checks - 1} of {bank_checks} checks passed".encode() in r.data and shown_call in r.data)

print("\npicker and profile views")
r = solver.get("/")
check("the picker groups by topic by default",
      b"Problems by topic" in r.data and b"<strong>Strings</strong>" in r.data)
check("the picker labels its difficulty column", b"<span>Difficulty</span>" in r.data)
check("difficulty shows as a three-step meter", b'class="difficulty-meter"' in r.data)
check("solved problems are marked in the picker",
      b"practice-status is-solved" in r.data and b"1 solved" in r.data)
r = solver.get("/?view=unit")
check("the course-unit view is still available",
      b"Problems by course unit" in r.data and b'<span class="tag">Unit 1</span>' in r.data)
r = solver.get("/profile")
check("the profile shows progress by topic",
      b"Progress by topic" in r.data and b"<strong>Strings</strong>" in r.data)
check("the profile lists solved problems",
      b"Code that passed every check" in r.data and b"Count Letters" in r.data)
check("the instructor overview counts solved problems",
      {row["handle"]: row["solved"] for row in
       server_module.student_overview()}["solver"] == 1)

print("\ncost abuse")
# Parallel requests: the turn cap is read before each model call, so it only
# holds if calls for one session happen one at a time.
racer = SessionEngine.start("racer", "p1", "drill")
holder = SessionEngine.load(racer.session.id)
check("the first request takes the turn slot", holder._claim_turn())
calls_before = len(_calls)
second = SessionEngine.load(racer.session.id).send("A parallel message sent while the first is in flight.")
check("a parallel turn is refused, not billed",
      second.get("busy") is True and len(_calls) == calls_before)
holder._release_turn()
check("the slot frees when the turn ends",
      "reply" in SessionEngine.load(racer.session.id).send(
          "Now a message once the first has finished, long enough to count."))
Session.update(turn_lease_until=datetime.datetime.now() - datetime.timedelta(seconds=1)).where(
    Session.id == racer.session.id).execute()
check("a lease left by a crashed request expires on its own",
      SessionEngine.load(racer.session.id)._claim_turn())
SessionEngine.load(racer.session.id)._release_turn()

# Two finish calls at once (double click, timer and End together).
_graded.clear()
finisher = SessionEngine.start("finisher", "p1", "drill")
first, late = SessionEngine.load(finisher.session.id), SessionEngine.load(finisher.session.id)
first.finish("completed")
check("a second, simultaneous finish does not grade again",
      late.finish("completed") is None and len(_graded) == 1)
check("a turn arriving after another request closed the session does not reopen it",
      late.send("one more message after the end").get("ended") is True
      and Session.get_by_id(finisher.session.id).ended_at is not None)

busy_client = server_module.app.test_client()
busy_client.post("/start", data={"handle": "busy", "problem": "p1", "mode": "drill"},
                 environ_overrides={"REMOTE_ADDR": "198.51.100.60"})
with busy_client.session_transaction() as cookie:
    busy_id = cookie["session_id"]
SessionEngine.load(busy_id)._claim_turn()
r = busy_client.post("/api/turn", json={"message": "sent while another turn is in flight"})
check("the API answers a parallel turn with 429", r.status_code == 429)
SessionEngine.load(busy_id)._release_turn()

# The site-wide cap: however many handles and addresses, the day has a ceiling.
saved_cap = server_module.GLOBAL_DAILY_DRILLS
server_module.GLOBAL_DAILY_DRILLS = server_module._sessions_today("drill")
r = server_module.app.test_client().post(
    "/start", data={"handle": "brand-new-handle", "problem": "p1", "mode": "drill"},
    environ_overrides={"REMOTE_ADDR": "198.51.100.70"})
check("a fresh handle from a fresh address still hits the site-wide cap",
      r.status_code == 429 and b"full for today" in r.data
      and not Student.select().where(Student.handle == "brand-new-handle").join(Session).exists())
server_module.GLOBAL_DAILY_DRILLS = saved_cap

# The class code.
server_module.ACCESS_CODE = "class-code-for-tests"
gate_client = server_module.app.test_client()
r = gate_client.get("/")
check("the picker asks for the class code when one is set", b'name="access_code"' in r.data)
r = gate_client.post("/start", data={"handle": "outsider", "problem": "p1", "mode": "drill"},
                     environ_overrides={"REMOTE_ADDR": "198.51.100.80"})
check("starting without the class code is refused",
      r.status_code == 302 and "code=invalid" in r.headers["Location"])
r = gate_client.post("/start", data={"handle": "outsider", "problem": "p1", "mode": "drill",
                                     "access_code": "guess"},
                     environ_overrides={"REMOTE_ADDR": "198.51.100.81"})
check("a wrong class code is refused",
      r.status_code == 302 and "code=invalid" in r.headers["Location"]
      and not Student.select().where(Student.handle == "outsider").exists())
r = gate_client.post("/start", data={"handle": "insider", "problem": "p1", "mode": "drill",
                                     "access_code": "class-code-for-tests"},
                     environ_overrides={"REMOTE_ADDR": "198.51.100.82"})
check("the right class code starts a session", r.headers.get("Location", "").endswith("/interview"))
r = gate_client.post("/start", data={"handle": "insider", "problem": "p1", "mode": "drill"},
                     environ_overrides={"REMOTE_ADDR": "198.51.100.83"})
check("the class code is asked once per browser", r.headers.get("Location", "").endswith("/interview"))
check("the picker stops asking once it is entered", b'name="access_code"' not in gate_client.get("/").data)
server_module.ACCESS_CODE = ""

print("\ncloudfront origin")
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.test import Client
from werkzeug.wrappers import Response


def echo_address(environ, start_response):
    # A bare werkzeug Client sets no REMOTE_ADDR unless a test supplies one.
    return Response(environ.get("REMOTE_ADDR", ""))(environ, start_response)


gate = Client(server_module.OriginGate(echo_address, "s3cret-value-from-cloudfront"))
check("a request without the CloudFront header is refused", gate.get("/").status_code == 403)
check("a wrong header value is refused",
      gate.get("/", headers={"X-Rung-Origin": "guess"}).status_code == 403)
check("a non-ASCII header value is refused rather than crashing",
      gate.get("/", headers={"X-Rung-Origin": "café"}).status_code == 403)
check("the CloudFront header is let through",
      gate.get("/", headers={"X-Rung-Origin": "s3cret-value-from-cloudfront"}).status_code == 200)
check("health stays reachable without it", gate.get("/health").status_code == 200)

# CloudFront appends the viewer's address to X-Forwarded-For, then nginx
# appends CloudFront's. With two trusted hops the student is second from the
# right, and whatever the client wrote further left is ignored.
behind = Client(ProxyFix(echo_address, x_for=2))
r = behind.get("/", headers={"X-Forwarded-For": "6.6.6.6, 203.0.113.7, 130.176.0.1"},
               environ_overrides={"REMOTE_ADDR": "127.0.0.1"})
check("two trusted hops find the student behind CloudFront and nginx",
      r.get_data(as_text=True) == "203.0.113.7")

close_db()

if failures:
    print(f"\n{len(failures)} check(s) failed")
    sys.exit(1)
print("\nall app checks passed")
