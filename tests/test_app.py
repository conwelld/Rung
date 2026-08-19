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

import sys

from rung.models import (Concept, Problem, ProblemConcept, RubricScore, Session,
                         Student, Turn, Unit, close_db, init_db)

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

init_db(":memory:")

# Flask registers before/teardown hooks at import, so reassigning the module
# attribute does nothing: the app already holds a reference to the original
# function. Clear the registrations instead. The hooks exist to open and close a
# file-backed database per request; an in-memory one lives for the process and
# reopening it would hand every request a fresh empty database.
server_module.app.before_request_funcs.clear()
server_module.app.teardown_request_funcs.clear()
server_module.DB_PATH = ":memory:"

unit = Unit.create(number=1, title="Functions")
concept = Concept.create(slug="loops", title="loops")
problem = Problem.create(slug="p1", title="P1", unit=unit,
                         prompt="Write a thing.", forbidden_insight="Use a loop.")
ProblemConcept.create(problem=problem, concept=concept)

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

print("\ntemplate integrity")
# Jinja discards anything a child template puts outside a block, with no error
# and no warning. That shipped once: the interview page rendered perfectly and
# had zero JavaScript, so the timer never started and Send did nothing. These
# assert the behaviour, not the file layout, because the file looked fine.
r = client.get("/interview")
page = r.data.decode()
check("interview page has a script tag", "<script>" in page)
check("session state is injected into the page", "var state = " in page)
check("timer function reached the page", "function tick" in page)
check("send handler reached the page", "send.addEventListener" in page)
check("no unrendered jinja tags leaked", "{%" not in page and "{{" not in page)

# The other templates go through real requests. url_for needs a request
# context, and hitting the route is closer to what actually happens than
# render_template with hand-built arguments would be.
for label, path in [("problem picker", "/"),
                    ("debrief", f"/debrief/{Session.select().first().id}"),
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

print("\ndaily quota")
from rung.config import DAILY_DRILL_LIMIT
quota_client = server_module.app.test_client()
for _ in range(DAILY_DRILL_LIMIT):
    quota_client.post("/start", data={"handle": "eve", "problem": "p1", "mode": "drill"})
r = quota_client.post("/start", data={"handle": "eve", "problem": "p1", "mode": "drill"})
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

close_db()

if failures:
    print(f"\n{len(failures)} check(s) failed")
    sys.exit(1)
print("\nall app checks passed")
