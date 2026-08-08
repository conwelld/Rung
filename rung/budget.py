"""
Session limits, enforced server-side.

Three independent ceilings, because they fail in different ways:

  turns    a student who never converges
  tokens   a student who writes essays every turn
  clock    a browser tab left open

Any one of them ending the session is fine. All three existing is what makes
the worst case bounded instead of theoretical. This runs on the server. A limit
enforced in the browser is a suggestion.
"""

import time

from rung.config import get_mode, price


class SessionBudget:
    """Tracks one student's session against its mode's limits."""

    def __init__(self, mode_name: str, clock=time.monotonic):
        self.mode_name = mode_name
        self.mode = get_mode(mode_name)
        self._clock = clock          # injectable so tests do not sleep
        self.started_at = clock()
        self.turns = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.stopped_because = None

    # --- checks -------------------------------------------------------------
    def elapsed_minutes(self) -> float:
        return (self._clock() - self.started_at) / 60

    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def check(self) -> str | None:
        """Return the reason the session must end, or None to continue.

        Called BEFORE spending, not after. Checking afterwards means the call
        that broke the budget already got billed.
        """
        if self.turns >= self.mode["max_turns"]:
            return "turn_limit"
        if self.total_tokens() >= self.mode["token_budget"]:
            return "token_budget"
        if self.elapsed_minutes() >= self.mode["duration_minutes"]:
            return "time_limit"
        return None

    def can_continue(self) -> bool:
        reason = self.check()
        if reason:
            self.stopped_because = reason
            return False
        return True

    # --- accounting ---------------------------------------------------------
    def record(self, usage: dict) -> None:
        """Fold one API response's usage into the running total.

        `usage` is the block the Messages API returns. Cache fields are counted
        as input when present so the totals stay honest if caching is turned on
        later.
        """
        self.turns += 1
        self.input_tokens += (
            usage.get("input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0)
            + usage.get("cache_read_input_tokens", 0)
        )
        self.output_tokens += usage.get("output_tokens", 0)

    def spent(self) -> float:
        return price(self.mode["model"], self.input_tokens, self.output_tokens)

    # --- reporting ----------------------------------------------------------
    def student_message(self) -> str:
        """What the student sees when a limit ends the session. Never mention
        cost to a student: that is not their problem and it discourages the
        practice the tool exists to encourage."""
        return {
            "turn_limit": "That's the end of this session. Let's look at how it went.",
            "time_limit": "Time's up, same as a real interview. Let's review.",
            "token_budget": "We've gone long on this one. Let's wrap up and review.",
        }.get(self.stopped_because, "Session complete.")

    def summary(self) -> dict:
        return {
            "mode": self.mode_name,
            "turns": self.turns,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "elapsed_minutes": round(self.elapsed_minutes(), 1),
            "cost_usd": round(self.spent(), 4),
            "stopped_because": self.stopped_because,
        }


def trim_history(history: list, window: int | None) -> list:
    """Keep only the most recent `window` exchanges.

    An exchange is a student message plus the proctor reply, so the slice is
    2*window entries. Returning full history when window is None is what the
    graded interview uses: it is worth the money there.

    This is the difference between per-turn cost rising through a session and
    staying flat, which is what makes unlimited drilling affordable.
    """
    if window is None or len(history) <= window * 2:
        return history
    return history[-window * 2:]
