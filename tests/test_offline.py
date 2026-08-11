"""
Offline checks. No API key, no network, no cost.

Run this before run_evals.py. Every failure here is a bug in your harness
rather than a finding about the model, and it is far cheaper to find out now.
It also scans for committed credentials, so make it the last thing you run
before pushing.

    python test_offline.py
"""

import pathlib
import re

from rung.budget import SessionBudget, trim_history
from evals.cases import CASES, case_count_by_category
from rung.config import (CACHE_MINIMUM_TOKENS, ENABLE_PROMPT_CACHING, MODES,
                         RETAIN_TRANSCRIPTS, price)
from tools.cost_model import session_cost, session_tokens
from rung.judge import check_code_leak
from rung.problems import PROBLEMS, SAMPLE_BANK, load_problems
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
check("drill costs under a tenth of an interview",
      session_cost("drill") < session_cost("interview") / 10)
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
sample, _ = load_problems(SAMPLE_BANK)
check("committed sample bank loads", len(sample) >= 3)
check("sample problems are complete",
      all(p.get("forbidden_insight") and p.get("concepts") and p.get("prompt")
          for p in sample.values()))

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
from rung.models import (Concept, Problem, ProblemConcept, RubricScore, Session,
                         Student, Turn, Unit, close_db, database, init_db)
from rung.diagnostics import (_concept_profile_n_plus_one, class_overview,
                              concept_profile, session_summary, unit_progress,
                              weakest_concepts)
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

check("turn text is null by default (no transcripts stored)",
      all(t.student_text is None and t.proctor_text is None for t in Turn.select()))

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
check("transcript retention is off by default", RETAIN_TRANSCRIPTS is False)
check(".env is gitignored", ".env" in gitignore)

print(f"\ncase mix: {case_count_by_category()}")
print(f"total cases: {len(CASES)}")
print(f"interview session ~${session_cost('interview'):.4f}   "
      f"drill session ~${session_cost('drill'):.4f}")

if failures:
    print(f"\n{len(failures)} check(s) failed")
    raise SystemExit(1)
print("\nall offline checks passed")
