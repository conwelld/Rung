"""
API connectivity check. Run this when the harness reports errors.

    python -m tools.check_api

Makes the smallest possible request against each configured model and prints
the full response body. `raise_for_status()` in the harness keeps the status
line and throws away the message explaining it, which is exactly the part you
need, so this asks directly.

Costs a fraction of a cent. Nothing here touches the problem bank or the
adversarial suite.
"""

import json
import os
import sys

import requests

from rung.config import JUDGE_MODEL, MODES

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"


def check_env() -> str | None:
    key = os.environ.get("ANTHROPIC_API_KEY")

    if not key:
        print("  FAIL  ANTHROPIC_API_KEY is not set in this window.")
        print("        set ANTHROPIC_API_KEY=sk-ant-api03-...")
        print("        Note: `set` only lasts for this cmd window.")
        return None

    print(f"  ok    key is set ({len(key)} chars, starts {key[:12]}...)")

    if key != key.strip():
        print("  FAIL  key has leading or trailing whitespace. Re-set it without.")
        return None
    if not key.startswith("sk-ant-"):
        print(f"  FAIL  key should start with sk-ant-, got {key[:10]!r}")
        return None
    if "..." in key:
        print("  FAIL  key contains '...', so a placeholder got pasted.")
        return None
    if len(key) < 50:
        print(f"  WARN  key is only {len(key)} chars, which looks truncated.")

    return key


def try_model(key: str, model: str) -> bool:
    """Smallest legal request. Prints the body on failure, which is the point."""
    print(f"\n  testing {model}")
    try:
        response = requests.post(
            API_URL,
            headers={
                "x-api-key": key,
                "anthropic-version": API_VERSION,
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 16,
                "messages": [{"role": "user", "content": "Reply with the word ok."}],
            },
            timeout=30,
        )
    except requests.RequestException as exc:
        print(f"    FAIL  could not reach the API: {exc}")
        return False

    if response.status_code == 200:
        body = response.json()
        text = "".join(b.get("text", "") for b in body.get("content", []))
        usage = body.get("usage", {})
        print(f"    ok    replied {text.strip()!r}  "
              f"({usage.get('input_tokens')} in, {usage.get('output_tokens')} out)")
        return True

    print(f"    FAIL  HTTP {response.status_code}")
    try:
        error = response.json().get("error", {})
        print(f"    type: {error.get('type')}")
        print(f"    says: {error.get('message')}")
        explain(response.status_code, error.get("message", ""), model)
    except (json.JSONDecodeError, AttributeError):
        print(f"    body: {response.text[:400]}")
    return False


def explain(status: int, message: str, model: str) -> None:
    """Translate the common failures into the thing you actually do next."""
    lowered = message.lower()

    if "credit balance" in lowered or "billing" in lowered:
        print("\n    -> The account has no funds. A valid key still cannot call")
        print("       the API without credit. Go to console.anthropic.com,")
        print("       Billing. New accounts need phone verification before the")
        print("       trial credit is claimable.")
    elif "model" in lowered and status == 400:
        print(f"\n    -> {model!r} was rejected. Either the name is wrong or your")
        print("       account cannot reach it yet. Newer models sometimes roll")
        print("       out to higher tiers first. Check the models list in the")
        print("       console and update MODES in rung/config.py to match.")
    elif status == 401:
        print("\n    -> The key was rejected. Regenerate it in the console.")
    elif status == 429:
        print("\n    -> Rate limited. Rerun with a larger --delay.")
    elif status == 529:
        print("\n    -> The API is overloaded. Try again shortly.")


def main() -> int:
    print("\nenvironment")
    key = check_env()
    if not key:
        return 1

    models = sorted({m["model"] for m in MODES.values()} | {JUDGE_MODEL})
    results = {model: try_model(key, model) for model in models}

    print("\n" + "=" * 52)
    working = [m for m, good in results.items() if good]
    broken = [m for m, good in results.items() if not good]

    for model in working:
        print(f"  ok    {model}")
    for model in broken:
        print(f"  FAIL  {model}")

    if not broken:
        print("\n  All models reachable. Run the harness:")
        print("    python -m evals.run_evals --limit 5")
        return 0

    if working:
        print(f"\n  {len(broken)} model(s) unavailable. Point MODES in")
        print(f"  rung/config.py at one that works, e.g. {working[0]}")
    else:
        print("\n  No models reachable. The message above says why.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
