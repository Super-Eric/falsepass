"""The helpers for hand-written policy adapters: `sequential` and the below-reference check."""

import asyncio
import json

import pytest

from falsepass import Adapter, Example
from falsepass.model import Expect, Policy, Score
from falsepass.policies import sequential
from falsepass.probes import check

PRICES = {"apple": 3, "pear": 5}


def play(policy: Policy, prompt: str, turns: int = 10) -> tuple[list[dict], str]:
    """A tiny tool loop: `price(item)` returns {"price": n}; a text reply ends the episode."""
    messages = [{"role": "user", "content": prompt}]
    for _ in range(turns):
        reply = policy.act(messages, [])
        reply = {"content": reply} if isinstance(reply, str) else reply
        messages.append({"role": "assistant", **reply})
        if not reply.get("tool_calls"):
            return messages, reply["content"]
        for call in reply["tool_calls"]:
            messages.append({"role": "tool", "content": json.dumps({"price": PRICES[call["arguments"]["item"]]})})
    raise AssertionError("did not stop")


def total(prompt: str, use) -> dict:
    items = prompt.split(":")[1].split(",")
    return {"total": sum(use("price", item=item.strip())["price"] for item in items)}


def test_a_step_by_step_solution_plays_as_a_policy():
    messages, answer = play(sequential("solver", total), "Add up: apple, pear, apple")
    assert json.loads(answer) == {"total": 11}
    assert [c["arguments"]["item"] for m in messages for c in m.get("tool_calls") or []] == ["apple", "pear", "apple"]
    assert play(sequential("plain", total, final=lambda a: f"Total: {a['total']}"), "Add up: pear")[1] == "Total: 5"


def test_a_solution_that_catches_everything_still_asks_for_its_tools():
    def careful(prompt, use):
        try:
            return total(prompt, use)
        except Exception:  # a solver's own fallback must not swallow the next tool call
            return {"total": -1}

    assert json.loads(play(sequential("careful", careful), "Add up: apple")[1]) == {"total": 3}


def test_a_solution_that_changes_its_calls_on_replay_is_named():
    tools = iter(["price", "pear"])  # the first run asks for price, the replay for another tool
    flaky = sequential("flaky", lambda prompt, use: use(next(tools), item="apple"))
    with pytest.raises(RuntimeError, match="'flaky'.*call 1 was 'price' before and is 'pear' now"):
        play(flaky, "Add up: apple")


class Fixed:
    """Scores a completion by a table: the policy's name or the text."""

    def __init__(self, table):
        self.table = table

    async def score(self, example, completion):
        return Score(self.table[completion.name if isinstance(completion, Policy) else completion])


class Toy(Adapter):
    name = "toy"

    def load(self, n, seed):
        return None, []

    def render(self, answer, example):
        return answer


def test_below_reference_judges_against_the_correct_answer_on_the_same_task():
    ex, adapter = Example("t1", "p", "right"), Toy()
    scorer = Fixed({"right": 0.8, "half": 0.5, "same": 0.8, "solver": 0.8})

    def run(completion, **kw):
        return asyncio.run(check("X2", "wrong", "v", Expect.BELOW_REFERENCE, completion, adapter, scorer, ex, **kw))

    assert run("half").passed and run("half", reference=0.6).passed
    same = run("same")  # the reference is rendered from the answer when none is given
    assert not same.passed and same.detail == "the reference earns 0.8 and this 0.8"
    assert not run("same", reference=Policy("solver", lambda m, t: "")).passed
    assert not run("half", reference=0.5).passed
