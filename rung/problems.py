"""
Problem bank loader.

The real question bank is course material. It belongs to the department, gets
reused every semester, and publishing it would hand the question list to every
future student. It lives in `data/problems.json`, which is gitignored.

`data/problems.sample.json` is committed instead: a small generic bank covering
every problem id used by the evaluation suite. Ask before replacing that with
the real bank in any public repository.

Each problem carries a `forbidden_insight`: the one structural idea that, once
spoken, ends the student's problem-solving work. The judge checks replies
against it, which is how "have you considered a dictionary?" gets caught as a
leak even though it contains no code.

`concepts` are the diagnostic tags. Hint depth is attributed to these, so a
student's profile is per-concept rather than per-problem.

`topic` is the subject or data structure a problem is listed under in the
picker (Strings, Lists, Dictionaries...). It is optional so an older bank still
loads; a problem without one is listed under its unit's title instead.

`tests` are [input, expected] pairs. They never run on the server; the browser
runs them with rung/checks.py, and passing all of them is what "solved" means.

Turtle problems are deliberately excluded: turtle needs tkinter, which Pyodide
does not have. See DECISIONS.md.
"""

import json
import pathlib
import re

DATA_DIR = pathlib.Path(__file__).resolve().parent.parent / "data"
PRIVATE_BANK = DATA_DIR / "problems.json"
SAMPLE_BANK = DATA_DIR / "problems.sample.json"
VALID_CHALLENGES = frozenset({"warmup", "core", "stretch"})
REQUIRED_FIELDS = frozenset({
    "unit", "title", "challenge", "concepts", "prompt",
    "forbidden_insight", "tests",
})


def validate_problems(problems: dict, source: str) -> dict:
    """Fail early with actionable bank errors instead of breaking a session."""
    if not isinstance(problems, dict) or not problems:
        raise ValueError(f"Problem bank {source} must be a non-empty JSON object")

    errors = []
    for slug, data in problems.items():
        location = f"{source}:{slug}"
        if not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9_]+", slug):
            errors.append(f"{location} has an invalid slug")
        if not isinstance(data, dict):
            errors.append(f"{location} must be an object")
            continue
        missing = sorted(REQUIRED_FIELDS - set(data))
        if missing:
            errors.append(f"{location} is missing {', '.join(missing)}")
            continue
        if not isinstance(data["unit"], int) or isinstance(data["unit"], bool) \
                or data["unit"] < 1:
            errors.append(f"{location}.unit must be a positive integer")
        if not isinstance(data["title"], str) or not data["title"].strip():
            errors.append(f"{location}.title must be non-empty text")
        if data["challenge"] not in VALID_CHALLENGES:
            errors.append(
                f"{location}.challenge must be one of "
                f"{', '.join(sorted(VALID_CHALLENGES))}")
        if "topic" in data and (not isinstance(data["topic"], str)
                                or not data["topic"].strip()
                                or len(data["topic"]) > 40):
            errors.append(f"{location}.topic must be short non-empty text")
        concepts = data["concepts"]
        if not isinstance(concepts, list) or not concepts \
                or not all(isinstance(c, str) and c.strip() for c in concepts):
            errors.append(f"{location}.concepts must be a non-empty string list")
        elif len(concepts) != len(set(concepts)):
            errors.append(f"{location}.concepts contains duplicates")
        for field in ("prompt", "forbidden_insight"):
            if not isinstance(data[field], str) or len(data[field].strip()) < 20:
                errors.append(f"{location}.{field} must contain meaningful text")
        tests = data["tests"]
        if not isinstance(tests, list) or not all(
                isinstance(case, list) and len(case) == 2 for case in tests):
            errors.append(f"{location}.tests must be a list of [input, expected] pairs")

    if errors:
        raise ValueError("Invalid problem bank:\n  " + "\n  ".join(errors))
    return problems


def _read_bank(path: pathlib.Path) -> dict:
    return validate_problems(
        json.loads(path.read_text(encoding="utf-8")), str(path.name))


def load_problems(path: pathlib.Path | None = None) -> tuple[dict, str]:
    """Return (problems, which_bank). Prefers the private bank when present."""
    if path is not None:
        return _read_bank(path), str(path.name)
    if PRIVATE_BANK.exists():
        return _read_bank(PRIVATE_BANK), "private"
    if SAMPLE_BANK.exists():
        return _read_bank(SAMPLE_BANK), "sample"
    raise FileNotFoundError(
        f"No problem bank found. Expected {PRIVATE_BANK} or {SAMPLE_BANK}."
    )


PROBLEMS, BANK_SOURCE = load_problems()
