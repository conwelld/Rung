"""
Offline checks. No API key, no network, no cost.

Run this before run_evals.py. Every failure here is a bug in your harness
rather than a finding about the model, and it is far cheaper to find out now.
It also scans for committed credentials, so make it the last thing you run
before pushing.

    python test_offline.py
"""

import importlib
import pathlib
import re
import sys

from rung.budget import SessionBudget, trim_history
from evals.cases import CASES, case_count_by_category
from rung.config import (CACHE_MINIMUM_TOKENS, ENABLE_PROMPT_CACHING, MODES,
                         RETAIN_TRANSCRIPTS, price)
from tools.cost_model import session_cost, session_tokens
from rung.judge import check_code_leak
from rung.problems import (BANK_SOURCE, PROBLEMS, SAMPLE_BANK,
                           VALID_CHALLENGES, load_problems, validate_problems)
from rung.proctor import _parse, build_system_block, build_system_prompt

failures = []


def check(label, condition):
    if condition:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}")
        failures.append(label)


print("\ntier 1 leak detection")
check("fenced block caught", check_code_leak("here you go:\n```python\nx=1\n```")[0])
check("def caught", check_code_leak("try def compress_run(text):")[0])
check("for loop caught", check_code_leak("you would write for c in text:")[0])
check("append caught", check_code_leak("then call result.append(c)")[0])
check("setdefault caught", check_code_leak("use d.setdefault(v, [])")[0])
check("real return caught", check_code_leak("    return counter")[0])
check("plain question passes", not check_code_leak(
    "What happens when you reach the last character in the string?")[0])
check("complexity question passes", not check_code_leak(
    "What is the time complexity of the plan you just described?")[0])
check("prose 'return' passes", not check_code_leak(
    "So what should this return for an empty input?")[0])
check("prose 'for ... in' passes", not check_code_leak(
    "What happens for every character in the string that is not a letter?")[0])
check("prose 'while' passes", not check_code_leak(
    "Does anything change while you are walking through the list?")[0])

print("\nJSON parsing")
check("clean json", _parse('{"rung_used":2,"advance_phase":false,"reply":"hi"}', 4)["parse_ok"])
check("fenced json", _parse('```json\n{"rung_used":1,"advance_phase":true,"reply":"hi"}\n```', 4)["rung_used"] == 1)
check("garbage falls back to reply", _parse("not json", 4)["reply"] == "not json")
check("garbage marked unparsed", not _parse("not json", 4)["parse_ok"])
check("ceiling carried through", _parse('{"rung_used":1}', 2)["rung_ceiling"] == 2)
# Regression: Haiku wraps JSON in a fence after a sentence of preamble. The old
# parser only handled text STARTING with a fence, so this fell through to the
# raw-text path where leftover backticks tripped tier 1 and a correct refusal
# was scored as an answer leak.
preamble = 'Here is my response:\n```json\n{"rung_used":2,"advance_phase":false,"reply":"I cannot do that."}\n```'
check("fence after preamble parses", _parse(preamble, 4)["parse_ok"])
check("fence after preamble strips backticks",
      "```" not in _parse(preamble, 4)["reply"])
check("parsed refusal is not a code leak",
      not check_code_leak(_parse(preamble, 4)["reply"])[0])
check("bare object in prose parses",
      _parse('Sure. {"rung_used":0,"advance_phase":true,"reply":"ok"} Done.', 4)["parse_ok"])
check("braces inside reply survive",
      _parse('{"rung_used":1,"advance_phase":false,"reply":"What does {} mean?"}', 4)["reply"]
      == "What does {} mean?")
check("real garbage still marked unparsed",
      not _parse("I refuse to answer in JSON.", 4)["parse_ok"])

print("\nmode configuration")
check("drill is cheaper than interview", session_cost("drill") < session_cost("interview"))
check("drill costs under fifteen percent of an interview",
      session_cost("drill") < session_cost("interview") * 0.15)
check("drill caps the ladder below rung 4", MODES["drill"]["max_rung"] < 4)
check("drill windows history", MODES["drill"]["history_window"] is not None)
check("interview keeps full history", MODES["interview"]["history_window"] is None)
check("every mode has a priced model",
      all(m["model"] in {"claude-sonnet-5", "claude-haiku-4-5"} for m in MODES.values()))
check("every mode phase has a rule",
      all(p in ("CLARIFY", "APPROACH", "CODE", "DEBUG")
          for m in MODES.values() for p in m["phases"]))

print("\ncost model")
_, _ = session_tokens(10, None)
lin_in, _ = session_tokens(20, 6)
quad_in, _ = session_tokens(20, None)
check("windowing reduces input tokens", lin_in < quad_in)
small, _ = session_tokens(10, None)
big, _ = session_tokens(20, None)
check("unwindowed cost grows faster than linearly", big > small * 2)
check("windowed cost grows about linearly",
      session_tokens(20, 6)[0] < session_tokens(10, 6)[0] * 2.4)
check("price is positive", price("claude-sonnet-5", 1000, 100) > 0)
check("haiku is cheaper than sonnet per token",
      price("claude-haiku-4-5", 1000, 100) < price("claude-sonnet-5", 1000, 100))

print("\nhistory windowing")
history = [{"role": "user", "content": f"m{i}"} for i in range(20)]
check("window trims to 2x exchanges", len(trim_history(history, 6)) == 12)
check("window keeps the most recent", trim_history(history, 3)[-1]["content"] == "m19")
check("no window returns everything", len(trim_history(history, None)) == 20)
check("short history untouched", len(trim_history(history[:4], 6)) == 4)

print("\nsession budget")
clock = [0.0]
b = SessionBudget("drill", clock=lambda: clock[0])
check("fresh session can continue", b.can_continue())
for _ in range(MODES["drill"]["max_turns"]):
    b.record({"input_tokens": 100, "output_tokens": 50})
check("turn limit stops the session", not b.can_continue())
check("stop reason recorded", b.stopped_because == "turn_limit")
check("student message hides cost", "$" not in b.student_message())

b2 = SessionBudget("interview", clock=lambda: clock[0])
b2.record({"input_tokens": 200_000, "output_tokens": 0})
check("token budget stops the session", not b2.can_continue())
check("token stop reason", b2.stopped_because == "token_budget")

clock[0] = 0.0
b3 = SessionBudget("interview", clock=lambda: clock[0])
clock[0] = 60 * 31
check("clock stops the session", not b3.can_continue())
check("time stop reason", b3.stopped_because == "time_limit")

b4 = SessionBudget("drill", clock=lambda: clock[0])
b4.record({"input_tokens": 500, "cache_read_input_tokens": 200, "output_tokens": 50})
check("cache tokens counted as input", b4.input_tokens == 700)
check("summary reports spend", b4.summary()["cost_usd"] >= 0)

print("\ncase and problem integrity")
missing = sorted({c["problem_id"] for c in CASES} - set(PROBLEMS))
check("every case points at a real problem", not missing)
if missing:
    print(f"        missing from the loaded bank: {', '.join(missing)}")
check("every case has a valid phase",
      all(c["phase"] in ("CLARIFY", "APPROACH", "CODE", "DEBUG") for c in CASES))
check("no rung ceiling above 4", all(1 <= c["max_rung"] <= 4 for c in CASES))
check("case ids are unique", len({c["id"] for c in CASES}) == len(CASES))
check("controls exist", any(c["category"] == "control" for c in CASES))
check("some cases run in drill mode",
      any(c["phase"] in MODES["drill"]["phases"] for c in CASES))
check("every problem has a forbidden insight",
      all(p.get("forbidden_insight") for p in PROBLEMS.values()))
check("every problem has concept tags", all(p.get("concepts") for p in PROBLEMS.values()))
check("every problem has a course-relative challenge",
      all(p.get("challenge") in VALID_CHALLENGES for p in PROBLEMS.values()))
check("every problem has a valid test collection",
      all(isinstance(p.get("tests"), list)
          and all(isinstance(case, list) and len(case) == 2
                  for case in p["tests"])
          for p in PROBLEMS.values()))
if BANK_SOURCE == "private":
    check("private curriculum has the curated 40-problem breadth", len(PROBLEMS) == 40)
sample, _ = load_problems(SAMPLE_BANK)
check("committed sample bank loads", len(sample) >= 3)
check("sample problems are complete",
      all(p.get("forbidden_insight") and p.get("concepts") and p.get("prompt")
          for p in sample.values()))

print("\nproblem topics")
check("every loaded problem is listed under a topic",
      all(isinstance(p.get("topic"), str) and p["topic"].strip() for p in PROBLEMS.values()))
check("sample bank problems are listed under a topic",
      all(p.get("topic") for p in sample.values()))
check("topics group problems rather than naming each one",
      len({p["topic"] for p in PROBLEMS.values()}) < len(PROBLEMS))
_bad = {"x_bad": {**next(iter(sample.values())), "topic": "   "}}
try:
    validate_problems(_bad, "test")
    check("a blank topic is rejected", False)
except ValueError:
    check("a blank topic is rejected", True)
_legacy = {"x_old": {k: v for k, v in next(iter(sample.values())).items() if k != "topic"}}
check("a bank without topics still loads", validate_problems(_legacy, "test") is _legacy)

print("\nbrowser checks harness")
# rung/checks.py is the code the Pyodide worker runs in the student's browser.
# Exercising it here under CPython is the only place it is tested at all.
from rung.checks import run_checks, run_checks_json

_one = run_checks("def f(xs):\n    return len(xs)", "f", [[[4, 9, 9, 1], 4]])
check("a list input goes to a one-parameter function whole", _one["passed"] == [True])
_two = run_checks("def f(xs, target):\n    return target in xs", "f", [[[[1, 2], 2], True]])
check("a list input is unpacked for a function that needs several", _two["passed"] == [True])
_opt = run_checks("def f(xs, k=2):\n    return len(xs)", "f", [[[5, 6], 2]])
check("an optional extra parameter does not trigger unpacking", _opt["passed"] == [True])
check("tuples compare equal to the bank's JSON lists",
      run_checks("def f(x):\n    return [(3, 4)]", "f", [[0, [[3, 4]]]])["passed"] == [True])
check("integer keys compare equal to the bank's JSON string keys",
      run_checks("def f(x):\n    return {1: ['a']}", "f", [[0, {"1": ["a"]}]])["passed"] == [True])
check("floats compare with tolerance",
      run_checks("def f(x):\n    return 0.1 + 0.2", "f", [[0, 0.3]])["passed"] == [True])
check("1 does not pass a check that expects True",
      run_checks("def f(x):\n    return 1", "f", [[0, True]])["passed"] == [False])
check("a wrong answer fails without an error",
      run_checks("def f(x):\n    return 2", "f", [[0, 3]]) == {"passed": [False], "error": None})
_raise = run_checks("def f(x):\n    return 10 // x", "f", [[5, 2], [0, 0]])
check("an exception fails only its own check", _raise["passed"] == [True, False])
check("an exception names the check but not its input",
      _raise["error"].startswith("Check 2 raised: ZeroDivisionError") and "[0" not in _raise["error"])
_syntax = run_checks("def f(x)\n    return x", "f", [[1, 1], [2, 2]])
check("code that does not compile fails every check",
      _syntax["passed"] == [False, False] and "SyntaxError" in _syntax["error"])
_missing = run_checks("def g(x):\n    return x", "f", [[1, 1]])
check("a missing function is named in the error", "named f" in _missing["error"])
_mutating = "def f(xs):\n    xs.append(0)\n    return len(xs)"
check("each check gets a fresh copy of its input",
      run_checks(_mutating, "f", [[[1], 2], [[1], 2]])["passed"] == [True, True])
check("the browser entry point round-trips JSON",
      run_checks_json("def f(x):\n    return x", "f", "[[1, 1]]")
      == '{"passed": [true], "error": null}')
_bank_ok = True
for _slug, _data in PROBLEMS.items():
    if _data["tests"]:
        _result = run_checks("def %s(*args):\n    return None" % _slug, _slug, _data["tests"])
        _bank_ok = _bank_ok and len(_result["passed"]) == len(_data["tests"])
check("every bank problem's checks run to completion", _bank_ok)

print("\nprompt assembly")
# Pick a real problem from whichever bank loaded rather than naming one. The
# sample bank and the private bank hold the same ids, but a future bank may not,
# and a KeyError here would mask every check after it.
sample_id = next(iter(PROBLEMS))
sample_problem = PROBLEMS[sample_id]
prompt = build_system_prompt(sample_problem, "APPROACH", 2)
check(f"prompt builds from a real problem ({sample_id})", len(prompt) > 500)
check("ceiling stated in prompt", "no deeper than rung 2" in prompt)
check("rungs above ceiling omitted", "Rung 3:" not in prompt and "Rung 4:" not in prompt)
check("forbidden insight embedded", sample_problem["forbidden_insight"][:40] in prompt)
check("problem statement embedded", sample_problem["prompt"][:40] in prompt)
check("phase rules embedded", "plain language before writing any" in prompt)
check("rung 4 present when allowed",
      "Rung 4:" in build_system_prompt(sample_problem, "APPROACH", 4))

print("\nprompt caching")
block = build_system_block(prompt, "claude-sonnet-5")
if ENABLE_PROMPT_CACHING:
    check("caching only used above the model minimum",
          isinstance(block, str) or len(prompt) / 4 >= CACHE_MINIMUM_TOKENS["claude-sonnet-5"])
else:
    check("caching disabled sends a plain string", isinstance(block, str))
check("prompt is still under the Sonnet cache floor (why it stays off)",
      len(prompt) / 4 < CACHE_MINIMUM_TOKENS["claude-sonnet-5"])

print("\nschema")
import datetime

from rung.models import (Concept, Problem, ProblemConcept, RubricScore, Session,
                         Student, Turn, Unit, add_missing_columns, close_db,
                         database, init_db)
from rung.diagnostics import (_concept_profile_n_plus_one, class_overview,
                              concept_profile, problem_status, session_summary,
                              student_overview, unit_progress, weakest_concepts)
from peewee import IntegrityError

# :memory: means every run starts clean and no test can touch real data.
init_db(":memory:")

check("all tables created", set(database.get_tables()) >= {
    "student", "unit", "concept", "problem", "problemconcept",
    "session", "turn", "rubricscore"})

_unit = Unit.create(number=1, title="Functions")
_c_str = Concept.create(slug="string-traversal", title="string traversal")
_c_dict = Concept.create(slug="dict-construction", title="dict construction")
_p1 = Problem.create(slug="p1", title="p1", unit=_unit, prompt="x", forbidden_insight="y")
_p2 = Problem.create(slug="p2", title="p2", unit=_unit, prompt="x", forbidden_insight="y")
ProblemConcept.create(problem=_p1, concept=_c_str)
ProblemConcept.create(problem=_p2, concept=_c_dict)

# SQLite ignores foreign keys unless the pragma is on, so this asserts the
# pragma took rather than asserting peewee declared the column.
try:
    Session.create(student=9999, problem=_p1, mode="drill", model="m")
    check("foreign keys are enforced", False)
except IntegrityError:
    check("foreign keys are enforced", True)

try:
    ProblemConcept.create(problem=_p1, concept=_c_str)
    check("problem-concept pairs are unique", False)
except IntegrityError:
    check("problem-concept pairs are unique", True)

_student = Student.create(handle="tester")
_s1 = Session.create(student=_student, problem=_p1, mode="interview", model="m")
for i, rung in enumerate([0, 1, 2], start=1):
    Turn.create(session=_s1, ordinal=i, phase="CODE", rung_used=rung, rung_ceiling=4)
_s2 = Session.create(student=_student, problem=_p2, mode="interview", model="m")
for i, rung in enumerate([3, 4, 4], start=1):
    Turn.create(session=_s2, ordinal=i, phase="CODE", rung_used=rung, rung_ceiling=4)

try:
    Turn.create(session=_s1, ordinal=1, phase="CODE", rung_used=0, rung_ceiling=4)
    check("turn ordinals are unique per session", False)
except IntegrityError:
    check("turn ordinals are unique per session", True)

check("turn text defaults to null in the schema",
      all(t.student_text is None and t.proctor_text is None for t in Turn.select()))
check("rubric scores carry a source", "self" in RubricScore.SOURCES
      and "proctor" in RubricScore.SOURCES)
check("rubric score is nullable (refusing to score is valid)",
      RubricScore.score.null is True)

_profile = {row["slug"]: row for row in concept_profile(_student)}
check("profile covers both concepts", set(_profile) == {"string-traversal", "dict-construction"})
check("weak concept ranks first", concept_profile(_student)[0]["slug"] == "dict-construction")
check("avg rung computed correctly", _profile["dict-construction"]["avg_rung"] == 3.67)
check("max rung computed correctly", _profile["dict-construction"]["max_rung"] == 4)
check("deep turns counted (rung >= 3)", _profile["dict-construction"]["deep_turns"] == 3)
check("shallow concept has no deep turns", _profile["string-traversal"]["deep_turns"] == 0)
check("turns counted per concept", _profile["string-traversal"]["turns"] == 3)

# The point of the single-query version is speed, not a different answer. If it
# ever disagrees with the naive one, the join is wrong.
_fast = {r["slug"]: round(r["avg_rung"], 2) for r in concept_profile(_student)}
_slow = {r["slug"]: round(r["avg_rung"], 2) for r in _concept_profile_n_plus_one(_student)}
check("single query agrees with the N+1 version", _fast == _slow)

check("min_turns filters thin evidence", weakest_concepts(_student, min_turns=99) == [])
check("weakest returns the struggling concept",
      weakest_concepts(_student, min_turns=3)[0]["slug"] == "dict-construction")

_units = unit_progress(_student)
check("unit progress returns every unit", len(_units) == 1)
Unit.create(number=2, title="Lists")
check("units with no sessions still appear",
      any(u["sessions"] == 0 for u in unit_progress(_student)))

_summary = session_summary(_s2)
check("session summary counts turns", _summary["turns"] == 3)
check("session summary finds deepest rung", _summary["deepest_rung"] == 4)

check("class overview aggregates across students", len(class_overview()) == 2)

print("\nsolved and practiced")
_now = datetime.datetime.now()
# _s2 (p2) finished and solved with rungs 3,4,4. A second, easier solve of the
# same problem must lower best_depth to its own deepest rung, and must not
# count as a second solved problem.
_s2.ended_at, _s2.solved, _s2.check_results = _now, True, "11"
_s2.save()
_s3 = Session.create(student=_student, problem=_p2, mode="drill", model="m",
                     ended_at=_now, solved=True, check_results="11")
Turn.create(session=_s3, ordinal=1, phase="CODE", rung_used=1, rung_ceiling=2)
# _s1 (p1) is still open: attempted, but neither practiced nor solved.
_status = problem_status(_student)
check("a finished, fully checked session counts as solved", _status[_p2.id]["solved"])
check("an open session is neither practiced nor solved",
      not _status[_p1.id]["practiced"] and not _status[_p1.id]["solved"])
check("best depth is the least-helped solve", _status[_p2.id]["best_depth"] == 1)
check("per-problem turns feed topic averages",
      _status[_p2.id]["turns"] == 4 and _status[_p2.id]["rung_total"] == 12)
check("no student means no status", problem_status(None) == {})
check("instructor overview counts distinct solved problems",
      {r["handle"]: r["solved"] for r in student_overview()}["tester"] == 1)

print("\ncolumn migration")
# A database created before check_results existed. create_tables() never
# alters a table, so without the migration every Session query would fail.
database.execute_sql("ALTER TABLE session DROP COLUMN check_results")
add_missing_columns()
check("a missing column is added to an existing table",
      "check_results" in {c.name for c in database.get_columns("session")})
add_missing_columns()
check("the migration is safe to run twice",
      Session.get_by_id(_s3.id).check_results is None)

# CASCADE means deleting a session takes its turns with it, rather than leaving
# orphans that quietly inflate every future count.
_before = Turn.select().count()
_s1.delete_instance(recursive=True)
check("deleting a session removes its turns", Turn.select().count() == _before - 3)

close_db()

print("\nsecret scan")
# Matches real key material. The docs contain the literal `sk-ant-...`
# placeholder, which has no key body, so requiring 20+ trailing key characters
# separates documentation from an actual committed credential.
REAL_KEY = re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
tracked = [
    p for p in REPO_ROOT.rglob("*")
    if p.is_file()
    and p.suffix in {".py", ".md", ".txt", ".json", ".cfg", ".toml", ".yml"}
    and "__pycache__" not in p.parts
    and "results" not in p.parts
    and ".git" not in p.parts
]
found = [p.name for p in tracked
         if REAL_KEY.search(p.read_text(encoding="utf-8", errors="ignore"))]
check(f"no key material in {len(tracked)} tracked files", not found)
if found:
    print(f"        credential found in: {', '.join(found)}")

here = REPO_ROOT
check("proctor reads the key from the environment",
      'os.environ.get("ANTHROPIC_API_KEY")' in (here / "rung" / "proctor.py").read_text())
gitignore = (here / ".gitignore").read_text()
check("results are gitignored (they contain student work)", "results/" in gitignore)
check("private question bank is gitignored", "data/problems.json" in gitignore)
check("database is gitignored (student performance data)", "data/*.db" in gitignore)
check("transcripts do not survive a finished session", RETAIN_TRANSCRIPTS is False)
check(".env is gitignored", ".env" in gitignore)

print("\ndeployment safety")
import os as _os

_saved = dict(_os.environ)
_srv = None
try:
    # Production with no secret key must refuse to start rather than invent a
    # random one. A random key works fine on one worker and silently logs
    # everyone out on a restart or a second worker, which is a miserable bug to
    # track down. Failing at boot is the kinder behaviour.
    _os.environ["RUNG_ENV"] = "production"
    _os.environ.pop("RUNG_SECRET_KEY", None)
    try:
        # The module may or may not be imported yet depending on test order, so
        # handle both: a first import and a reload both have to raise.
        if "app.server" in sys.modules:
            importlib.reload(sys.modules["app.server"])
        else:
            import app.server  # noqa: F401
        check("production without a secret key refuses to boot", False)
    except RuntimeError:
        check("production without a secret key refuses to boot", True)

    _os.environ["RUNG_SECRET_KEY"] = "x" * 64
    import app.server as _srv
    _srv = importlib.reload(_srv)
    check("production sets a secure cookie",
          _srv.app.config["SESSION_COOKIE_SECURE"] is True)
    check("cookie is not readable from javascript",
          _srv.app.config["SESSION_COOKIE_HTTPONLY"] is True)
    check("production is not development", _srv.IS_DEV is False)

    _os.environ["RUNG_ENV"] = "development"
    importlib.reload(_srv)
    check("development allows the cookie over plain http",
          _srv.app.config["SESSION_COOKIE_SECURE"] is False)
finally:
    _os.environ.clear()
    _os.environ.update(_saved)
    if _srv is not None:
        _os.environ.setdefault("RUNG_ENV", "development")
        importlib.reload(_srv)

_render = pathlib.Path(__file__).resolve().parent.parent / "render.yaml"
if _render.exists():
    _text = _render.read_text()
    check("deploy config runs a single worker", "--workers 1" in _text)
    check("deploy config never commits an api key",
          "sync: false" in _text and "sk-ant" not in _text)
    check("deploy config generates its secret rather than hardcoding one",
          "generateValue: true" in _text)
else:
    check("render.yaml present", False)

print("\naws deployment")
# Each of these broke, or would have broken, a real Elastic Beanstalk deploy.
_root = pathlib.Path(__file__).resolve().parent.parent
_procfile = (_root / "Procfile").read_text()
check("Procfile is one command with no comment lines (EB cannot parse them)",
      _procfile.strip().startswith("web: ") and "#" not in _procfile
      and len(_procfile.strip().splitlines()) == 1)
_eb = (_root / ".ebextensions" / "01-rung.config").read_text()
check("nginx serves /static from the app's folder, not a missing one",
      "/static: public/static" in _eb)
check("EB trusts exactly CloudFront and nginx for the client address",
      'RUNG_PROXY_HOPS: "2"' in _eb)
check("no secret is committed to EB config",
      not re.search(r"(RUNG_SECRET_KEY|ANTHROPIC_API_KEY|RUNG_ORIGIN_SECRET)\s*:", _eb))
check("the module worker is served with a JavaScript extension",
      (_root / "public" / "static" / "code-runner-worker.js").exists()
      and not list((_root / "public" / "static").glob("*.mjs")))
_backups = (_root / ".ebextensions" / "02-backups.config").read_bytes()
check("backup scripts have Unix line endings (bash rejects \\r)", b"\r" not in _backups)
check("a restore can never fail a deploy", b"ignoreErrors: true" in _backups)
_ebignore = [line.strip() for line in (_root / ".ebignore").read_text().splitlines()
             if line.strip() and not line.lstrip().startswith("#")]
check("local databases stay out of the deploy bundle", "data/*.db" in _ebignore)
check("the private bank is not excluded from the deploy bundle",
      not any(line.startswith("data/problems") for line in _ebignore))
_cf = (_root / "deploy" / "cloudfront.yaml").read_text()
check("CloudFront never caches pages (they carry student state)",
      "4135ea2d-6df8-44a3-9df3-4b5a84be39ad" in _cf)
check("CloudFront forwards cookies, query strings and bodies",
      "b689b0a8-53d0-40ab-baf2-68738e2966ac" in _cf and "POST" in _cf)
check("CloudFront sends the origin secret header", "X-Rung-Origin" in _cf and "NoEcho: true" in _cf)

print("\nvercel deployment")
import json as _json
import tomllib as _tomllib

_pyproject = _tomllib.loads((_root / "pyproject.toml").read_text())
_requirements = [line.strip() for line in (_root / "requirements.txt").read_text().splitlines()
                 if line.strip() and not line.startswith("#")]
check("pyproject.toml and requirements.txt list the same dependencies",
      sorted(_pyproject["project"]["dependencies"]) == sorted(_requirements))
_entry = _pyproject["tool"]["vercel"]["entrypoint"]
_entry_file = _entry.split(":")[0].replace(".", "/") + ".py"
_vercel = _json.loads((_root / "vercel.json").read_text())
check("the Vercel entrypoint exists", (_root / _entry_file).exists())
# The Vercel CLI detects this app as a service, and in services mode a
# top-level `functions` key is rejected outright ("the owning service is
# ambiguous"), so the deploy fails before it starts. Fluid compute's default
# 300 second limit already covers a slow grading call.
check("vercel.json has no top-level functions key (rejected in services mode)",
      "functions" not in _vercel)
check("the function runs next to the Supabase region", _vercel.get("regions") == ["iad1"])
# Services mode ignores pyproject's entrypoint and refuses to build without
# its own ("must specify an entrypoint for runtime python").
check("every Vercel service names the same entrypoint as pyproject.toml",
      all(service.get("entrypoint") == _entry
          for service in _vercel.get("services", {}).values()))
# Vercel's Python build leaves public/ out of the bundle, and in services mode
# every request goes to Flask, so without this the CSS and the Python runner
# 404 in production while every local test passes.
check("the deployed function bundle includes public/ (CSS, Python runner)",
      all(service.get("functions", {}).get(_entry_file, {}).get("includeFiles") == "public/**"
          for service in _vercel.get("services", {}).values()))
_vercelignore = [line.strip() for line in (_root / ".vercelignore").read_text().splitlines()
                 if line.strip() and not line.lstrip().startswith("#")]
check("the Supabase password in .env is never uploaded", ".env" in _vercelignore)
check("old EB bundles and logs are never uploaded", ".elasticbeanstalk" in _vercelignore)
check("local databases and archives are never uploaded",
      "data/*.db" in _vercelignore and "*.zip" in _vercelignore)
check("the private bank is uploaded",
      not any(line.startswith("data/problems") for line in _vercelignore))
check("static files sit where Vercel's CDN serves them",
      (_root / "public" / "static" / "rung.css").exists() and not (_root / "app" / "static").exists())

_saved = dict(_os.environ)
try:
    import rung.config as _config
    _os.environ["RUNG_PROXY_HOPS"] = "two"
    try:
        importlib.reload(_config)
        check("a malformed proxy hop count refuses to boot", False)
    except RuntimeError:
        check("a malformed proxy hop count refuses to boot", True)
    _os.environ["RUNG_PROXY_HOPS"] = "2"
    check("proxy hops parse from the environment", importlib.reload(_config).PROXY_HOPS == 2)
finally:
    _os.environ.clear()
    _os.environ.update(_saved)
    importlib.reload(_config)

print(f"\ncase mix: {case_count_by_category()}")
print(f"total cases: {len(CASES)}")
print(f"interview session ~${session_cost('interview'):.4f}   "
      f"drill session ~${session_cost('drill'):.4f}")

if failures:
    print(f"\n{len(failures)} check(s) failed")
    raise SystemExit(1)
print("\nall offline checks passed")
