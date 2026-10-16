"""Ready-made known-bad policies for games, simulations and tool tasks.

Each one never uses the reference answer, so any reward it earns is a false pass. They
encode what found real holes when environments were played by hand:

- `after_the_end`: answer wrong, then repeat whatever the environment gives away;
- `keeps_talking`: carry on after the environment says the episode is over;
- `never_acts`: say nothing useful at all;
- `special_numbers`: take a message the environment accepts and put NaN, Infinity or a
  negative number wherever it has a number (a cap written as `amount > LIMIT` is false
  for NaN).

Use them from an adapter's `exploits()` (see `falsepass init --policies`).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from falsepass.model import Policy

REVEALED = re.compile(
    r"(?:answer|word|solution|number|target|correct (?:answer|word))\s+(?:was|is)\s*:?\s*\**[\"'`]?([^\s\"'`*.,!?;]+)", re.I
)
TERMINAL = re.compile(
    r"game over|you (?:win|won|lose|lost)\b|the (?:correct )?(?:answer|word|solution) (?:was|is)\b"
    r"|episode (?:is )?(?:over|done|finished)|out of (?:guesses|turns|attempts)",
    re.I,
)
SPECIAL_NUMBERS = {"nan": "nan", "inf": "inf", "negative": "-1", "huge": "1e999", "negative_zero": "-0"}
NUMBER = re.compile(r"(?<![\w.#])\d+(?:\.\d+)?(?![\w.])")  # not ids such as #350352


def said(messages: list[dict[str, Any]]) -> list[str]:
    """What the policy itself said so far."""
    return [str(m.get("content") or "") for m in messages if m.get("role") == "assistant"]


def heard(messages: list[dict[str, Any]]) -> list[str]:
    """What the environment said so far, one entry per message."""
    return [str(m.get("content") or "") for m in messages if m.get("role") == "user"]


def revealed(messages: list[dict[str, Any]]) -> str | None:
    """The answer the environment gave away after the policy's first turn, if any."""
    if not said(messages):
        return None
    for text in heard(messages)[1:]:
        found = REVEALED.search(text)
        if found:
            return found.group(1)
    return None


def after_the_end(wrong: str, say: Callable[[str], str] = lambda answer: answer) -> Policy:
    """Answer `wrong`; once the environment gives the answer away, reply `say(answer)`."""

    def act(messages):
        answer = revealed(messages)
        return say(answer) if answer else wrong

    return Policy("after_the_end", act)


def keeps_talking(first: str, then: str = "Thanks! Let me keep going.", turns: int = 30) -> Policy:
    """Say `first`, then keep replying `then` for as long as the environment listens."""

    def act(messages):
        done = len(said(messages))
        return first if done == 0 else then if done < turns else ""

    return Policy("keeps_talking", act)


def never_acts(text: str = "Let me look into this.") -> Policy:
    """Reply without ever doing anything the task asks for."""
    return Policy("never_acts", lambda messages: text)


def special_numbers(script: list[str]) -> dict[str, list[dict[str, str]]]:
    """Variants of a script of assistant messages with every number in its last message
    replaced by a special value: {"nan": [...], "inf": [...], ...}. Use them as fixed
    scripts (no policy needed): the last message is usually the one that acts."""
    variants = {}
    for name, value in SPECIAL_NUMBERS.items():
        last = NUMBER.sub(value, script[-1])
        if last != script[-1]:
            variants[name] = [{"role": "assistant", "content": text} for text in [*script[:-1], last]]
    return variants
