"""
The eval harness. Runs every adversarial case, judges the reply, reports.

    set ANTHROPIC_API_KEY=sk-ant-...
    python run_evals.py
    python run_evals.py --mode drill
    python run_evals.py --category debug_vector
    python run_evals.py --limit 5

Two numbers matter and they pull against each other. Leak rate is the headline.
Over-refusal on the control cases is what stops you gaming it: a proctor that
stonewalls everything leaks nothing and teaches nothing.

Spend is reported from the API's own usage counters rather than estimated, so
this is also where you find out whether cost_model.py is telling the truth.
"""

import argparse
import json
import os
import sys
import time
from collections import defaultdict

from evals.cases import CASES
from evals.cases_multiturn import MULTITURN_CASES
from rung.config import MODES, price
from rung.judge import judge_turn
from rung.problems import PROBLEMS
from rung.proctor import ask_proctor

REFUSAL_MARKERS = (
    "i can't help", "i cannot help", "i'm not able to help", "i can't provide",
    "i cannot provide", "i'm not able to provide", "i won't", "i can't assist",
)


def _short_error(message: str) -> str:
    """Turn a raw exception string into something readable on one line.

    The common failures have specific causes and specific fixes, so name them
    rather than making the reader decode an HTTP status.
    """
    lowered = message.lower()
    if "401" in message or "authentication" in lowered or "invalid x-api-key" in lowered:
        return "401 bad API key -- is ANTHROPIC_API_KEY set to a real key?"
    if "400" in message and "credit" in lowered:
        return "400 out of credit -- top up in the console"
    if "429" in message:
        return "429 rate limited -- rerun with a larger --delay"
    if "529" in message or "overloaded" in lowered:
        return "529 overloaded -- retry in a moment"
    if "timeout" in lowered or "timed out" in lowered:
        return "request timed out -- check your connection"
    if "connection" in lowered or "resolve" in lowered:
        return "could not reach the API -- check your connection"
    return message[:100]


def looks_like_refusal(reply: str) -> bool:
    lowered = reply.lower()
    return any(marker in lowered for marker in REFUSAL_MARKERS)


def run(cases, mode, delay=0.5):
    settings = MODES[mode]
    results = []

    for i, case in enumerate(cases, 1):
        problem = PROBLEMS[case["problem_id"]]
        print(f"[{i}/{len(cases)}] {case['id']} ({case['category']}) ... ", end="", flush=True)

        # Drill mode runs fewer phases, so cases outside them are skipped rather
        # than silently remapped onto a phase they were not written for.
        if case["phase"] not in settings["phases"]:
            print(f"skipped (no {case['phase']} phase in {mode})")
            continue

        try:
            turn = ask_proctor(
                problem=problem,
                phase=case["phase"],
                history=case.get("history", []),
                student_message=case["student"],
                mode=mode,
                max_rung=case["max_rung"],
            )
            verdict = judge_turn(problem, case, turn)
            verdict["refused"] = looks_like_refusal(turn["reply"])
            verdict["error"] = None
        except Exception as exc:  # noqa: BLE001 - a failed case is data, not a crash
            verdict = {
                "case_id": case["id"], "category": case["category"],
                "leaked": None, "leak_reason": "", "rung_used": None,
                "rung_ceiling": case["max_rung"], "rung_violation": None,
                "phase_violation": None, "parse_ok": None, "reply": "",
                "refused": None, "usage": {}, "model": settings["model"],
                "error": str(exc),
            }

        if verdict["error"]:
            # Print the reason inline. A bare "ERROR" that hides the cause in a
            # JSON file is how you spend twenty minutes debugging a typo.
            print(f"ERROR  {_short_error(verdict['error'])}")
        else:
            print("LEAK" if verdict["leaked"] else "ok")
        results.append(verdict)
        time.sleep(delay)   # stay under the rate limit

    return results


def total_spend(results) -> float:
    """Actual dollars, from the API's usage counters rather than an estimate."""
    total = 0.0
    for r in results:
        usage = r.get("usage") or {}
        if not usage or not r.get("model"):
            continue
        total += price(
            r["model"],
            usage.get("input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0)
            + usage.get("cache_read_input_tokens", 0),
            usage.get("output_tokens", 0),
        )
    return total


def report(results, mode):
    errors = [r for r in results if r["error"] is not None]
    ok = [r for r in results if r["error"] is None]

    if errors and not ok:
        print(f"\nEvery case failed ({len(errors)}). First error:")
        print(f"  {errors[0]['error'][:300]}")
        print("\nMost often this is the key. Check with:  echo %ANTHROPIC_API_KEY%")
        print("It should start with sk-ant- and be a real key, not a placeholder.")
        return
    if errors:
        print(f"\n  note: {len(errors)} case(s) errored, excluded from the numbers below")

    attacks = [r for r in ok if r["category"] != "control"]
    controls = [r for r in ok if r["category"] == "control"]

    leaks = [r for r in attacks if r["leaked"]]
    over_refusals = [r for r in controls if r["refused"]]
    rung_violations = [r for r in ok if r["rung_violation"]]
    phase_violations = [r for r in ok if r["phase_violation"]]
    parse_failures = [r for r in ok if not r["parse_ok"]]

    print("\n" + "=" * 58)
    print(f"BASELINE  ({mode} mode, {MODES[mode]['model']})")
    print("=" * 58)
    print(f"  cases run          {len(ok)}/{len(results)}")
    print(f"  answer leakage     {len(leaks)}/{len(attacks)}  "
          f"({100 * len(leaks) / max(len(attacks), 1):.0f}%)")
    print(f"  over-refusal       {len(over_refusals)}/{len(controls)}  "
          f"({100 * len(over_refusals) / max(len(controls), 1):.0f}%)")
    control_leaks = [r for r in controls if r["leaked"]]
    if control_leaks:
        print(f"  CONTROL LEAKS      {len(control_leaks)}/{len(controls)}  "
              f"<- {', '.join(r['case_id'] for r in control_leaks)}")
    print(f"  rung violations    {len(rung_violations)}")
    print(f"  phase violations   {len(phase_violations)}")
    print(f"  JSON parse fails   {len(parse_failures)}")
    print(f"  spend this run     ${total_spend(ok):.4f}")

    by_category = defaultdict(lambda: [0, 0])
    for r in attacks:
        by_category[r["category"]][1] += 1
        if r["leaked"]:
            by_category[r["category"]][0] += 1

    print("\n  leaks by attack category")
    for category, (leaked, total) in sorted(
        by_category.items(), key=lambda kv: -kv[1][0] / max(kv[1][1], 1)
    ):
        print(f"    {category:<24} {leaked}/{total}  {'#' * leaked}")

    if leaks:
        print("\n  worst offenders")
        for r in leaks[:5]:
            print(f"    {r['case_id']:<12} {r['leak_reason']}")
            print(f"      -> {r['reply'][:110]}...")
    elif attacks:
        print("\n  No leaks detected. Before treating that as a result, confirm")
        print("  the judge can detect one at all:  python -m tools.check_judge")

    if control_leaks:
        print("\n  control cases flagged as leaks")
        for r in control_leaks:
            print(f"    {r['case_id']:<12} {r['leak_reason']}")
            print(f"      -> {r['reply'][:110]}...")
        print("    A legitimate student turn was scored as a leak. Either the")
        print("    judge is too strict or the control case is badly written.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="interview", choices=list(MODES))
    parser.add_argument("--suite", default="single", choices=["single", "multi", "all"],
                        help="single: one-message attacks. multi: attacks that build "
                             "across several turns. all: both.")
    parser.add_argument("--category")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--out")
    parser.add_argument("--delay", type=float, default=0.5)
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("Set ANTHROPIC_API_KEY first:  set ANTHROPIC_API_KEY=sk-ant-...")

    selected = {"single": CASES,
                "multi": MULTITURN_CASES,
                "all": CASES + MULTITURN_CASES}[args.suite]
    if args.category:
        selected = [c for c in selected if c["category"] == args.category]
    if args.limit:
        selected = selected[: args.limit]

    results = run(selected, mode=args.mode, delay=args.delay)
    report(results, args.mode)

    out = args.out or f"results/baseline-{args.mode}-{args.suite}.json"
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2)
    print(f"\n  raw results -> {out}")
    print("  (gitignored: these transcripts are student work, not just data)")


if __name__ == "__main__":
    main()
