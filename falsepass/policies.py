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

`sequential` turns a step-by-step solution (many packages ship one: a function that calls
the tools one at a time and returns the answer) into a policy, for the solver (X0) and for
its broken variants.
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


class _NeedsTool(BaseException):  # not Exception: a solver's own `except Exception` must not catch it
    def __init__(self, name: str, arguments: dict[str, Any]) -> None:
        super().__init__(name)
        self.name, self.arguments = name, arguments


def _result(text: str) -> Any:
    import json

    try:
        return json.loads(text)
    except ValueError:
        return text


def sequential(name: str, solve: Callable[[str, Callable[..., Any]], Any], final: Callable[[Any], str] | None = None) -> Policy:
    """A policy from a step-by-step solution `solve(prompt, use)`: `use(tool, arguments)`
    returns that tool's result (parsed as JSON when it is JSON) and `solve` returns the final
    answer. On every turn `solve` runs again from the start against the tool results so far;
    the first call with no result yet becomes the policy's next tool call, and once every call
    has its result, the answer is the final reply (`final(answer)`; default: the text, or
    JSON for anything else). `solve` must call the same tools in the same order when given
    the same results (no randomness, no clock); a different call on replay raises.

        solver = sequential("solver", lambda prompt, use: package.solve({"prompt": prompt}, use))
    """
    import json

    def act(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Any:
        prompt = next((str(m.get("content") or "") for m in messages if m.get("role") == "user"), "")
        made = [c for m in messages if m.get("role") == "assistant" for c in m.get("tool_calls") or []]
        results = [_result(str(m.get("content") or "")) for m in messages if m.get("role") == "tool"]
        position = 0

        def use(tool: str, arguments: dict[str, Any] | None = None, **keywords: Any) -> Any:
            nonlocal position
            arguments = {**(arguments or {}), **keywords}
            if position >= len(results):
                raise _NeedsTool(tool, arguments)
            if position < len(made) and made[position].get("name") != tool:
                raise RuntimeError(f"sequential policy {name!r}: call {position + 1} was {made[position].get('name')!r} "
                                   f"before and is {tool!r} now; solve() must repeat its calls when replayed")  # fmt: skip
            position += 1
            return results[position - 1]

        try:
            answer = solve(prompt, use)
        except _NeedsTool as need:
            return {"content": "", "tool_calls": [{"name": need.name, "arguments": need.arguments}]}
        if final is not None:
            return final(answer)
        return answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False, default=str)

    return Policy(name, act, wants_tools=True)

