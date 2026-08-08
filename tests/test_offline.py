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
from rung.config import CACHE_MINIMUM_TOKENS, ENABLE_PROMPT_CACHING, MODES, price
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
check(".env is gitignored", ".env" in gitignore)

print(f"\ncase mix: {case_count_by_category()}")
print(f"total cases: {len(CASES)}")
print(f"interview session ~${session_cost('interview'):.4f}   "
      f"drill session ~${session_cost('drill'):.4f}")

if failures:
    print(f"\n{len(failures)} check(s) failed")
    raise SystemExit(1)
print("\nall offline checks passed")
