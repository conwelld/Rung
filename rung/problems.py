"""
Problem bank loader.

The real question bank is course material. It belongs to the department, gets
reused every semester, and publishing it would hand the question list to every
future student. It lives in `data/problems.json`, which is gitignored.

`data/problems.sample.json` is committed instead: three universal intro
exercises so the repo runs for anyone who clones it. Ask before replacing that
with the real bank in any public repository.

Each problem carries a `forbidden_insight`: the one structural idea that, once
spoken, ends the student's problem-solving work. The judge checks replies
against it, which is how "have you considered a dictionary?" gets caught as a
leak even though it contains no code.

`concepts` are the diagnostic tags. Hint depth is attributed to these, so a
student's profile is per-concept rather than per-problem.

Turtle problems are deliberately excluded: turtle needs tkinter, which Pyodide
does not have. See DECISIONS.md.
"""

import json
import pathlib

DATA_DIR = pathlib.Path(__file__).resolve().parent.parent / "data"
PRIVATE_BANK = DATA_DIR / "problems.json"
SAMPLE_BANK = DATA_DIR / "problems.sample.json"


def load_problems(path: pathlib.Path | None = None) -> tuple[dict, str]:
    """Return (problems, which_bank). Prefers the private bank when present."""
    if path is not None:
        return json.loads(path.read_text(encoding="utf-8")), str(path.name)
    if PRIVATE_BANK.exists():
        return json.loads(PRIVATE_BANK.read_text(encoding="utf-8")), "private"
    if SAMPLE_BANK.exists():
        return json.loads(SAMPLE_BANK.read_text(encoding="utf-8")), "sample"
    raise FileNotFoundError(
        f"No problem bank found. Expected {PRIVATE_BANK} or {SAMPLE_BANK}."
    )


PROBLEMS, BANK_SOURCE = load_problems()
