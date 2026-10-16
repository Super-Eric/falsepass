"""Multi-turn support, exercised against a tiny real verifiers environment."""

import asyncio

import pytest

vf = pytest.importorskip("verifiers")
from datasets import Dataset  # noqa: E402

from falsepass import Adapter, Example, probes, search  # noqa: E402
from falsepass.model import Policy  # noqa: E402
from falsepass.scorers import V0RolloutScorer  # noqa: E402


class CountingEnv(vf.MultiTurnEnv):
    """Two-turn task: say the word, then say it again when asked. `leaky=True` keeps a
    counter on the environment object, so episodes are not independent."""

    def __init__(self, leaky: bool = False, **kwargs):
        super().__init__(**kwargs)
        self.leaky, self.episodes = leaky, 0

    async def setup_state(self, state):
        self.episodes += 1
        return state

    async def env_response(self, messages, state, **kwargs):
        suffix = f" (episode {self.episodes})" if self.leaky else ""
        return [vf.UserMessage(content=f"Say it once more.{suffix}")]


def said_twice(completion, answer, **kwargs) -> float:
    replies = [m["content"] for m in completion if m["role"] == "assistant"]
    return float(len(replies) == 2 and all(r.strip() == answer for r in replies))


def make(leaky: bool = False, max_turns: int = 2):
    data = Dataset.from_list([{"question": "Say the word: heron", "answer": "heron"}])
    env = CountingEnv(leaky=leaky, dataset=data, rubric=vf.Rubric(funcs=[said_twice]), max_turns=max_turns)
    row = env.get_dataset()[0]
    return V0RolloutScorer(env), Example("row-0", "Say the word: heron", "heron", row)


class Words(Adapter):
    name = "toy/say-twice"

    def render(self, answer, example):
        return answer

    def wrong_answer(self, example):
        return "stork"


def test_rollout_scorer_drives_the_environment_loop():
    scorer, ex = make()
    good = asyncio.run(scorer.score(ex, "heron"))
    assert good.reward == 1.0 and good.info["turns"] == 2
    assert [m["role"] for m in good.info["transcript"]][-3:] == ["assistant", "user", "assistant"]
    assert asyncio.run(scorer.score(ex, "stork")).reward == 0.0
    scripted = [{"role": "assistant", "content": "heron"}, {"role": "assistant", "content": "stork"}]
    assert asyncio.run(scorer.score(ex, scripted)).reward == 0.0


def test_state_reset_probe_catches_state_shared_between_episodes():
    scorer, ex = make()
    assert asyncio.run(probes.t4_state_reset(Words(), scorer, ex))[0].passed
    scorer, ex = make(leaky=True)
    result = asyncio.run(probes.t4_state_reset(Words(), scorer, ex))[0]
    assert not result.passed and "transcripts differ" in result.detail


def test_turn_limit_probe_reports_bounded_and_unbounded_episodes():
    scorer, ex = make()
    bounded = asyncio.run(probes.t5_turn_limit(Words(), scorer, ex))[0]
    assert bounded.passed and "stopped after 2 model turns" in bounded.detail
    scorer, ex = make(max_turns=-1)
    unbounded = asyncio.run(probes.t5_turn_limit(Words(), scorer, ex))[0]
    assert not unbounded.passed and "still running" in unbounded.detail


def test_search_mutates_every_turn_of_a_script():
    script = [{"role": "assistant", "content": "a"}, {"role": "user", "content": "u"}, {"role": "assistant", "content": "b"}]
    mutated = search.apply(lambda text, rng: text.upper(), script, None)
    assert [m["content"] for m in mutated] == ["A", "u", "B"]
    scorer, ex = make()
    results = {r.probe: r for r in asyncio.run(search.make_probe(40)(Words(), scorer, ex))}
    assert results["S1"].passed and set(results) == {"S1", "S2", "S3"}


def test_scripted_tool_calls_reach_the_environments_tools():
    """A tool-using environment: the script issues tool calls, the env runs its real tool."""
    ledger: list[int] = []

    def deposit(amount: int) -> str:
        """Add an amount to the ledger."""
        ledger.append(amount)
        return f"balance {sum(ledger)}"

    def reported_balance(completion, answer, **kwargs) -> float:
        return float(str(answer) in str(completion[-1]["content"]))

    data = Dataset.from_list([{"question": "Deposit 5 twice and report the balance.", "answer": "10"}])
    env = vf.ToolEnv(tools=[deposit], dataset=data, rubric=vf.Rubric(funcs=[reported_balance]), max_turns=6)
    scorer, ex = V0RolloutScorer(env), Example("row-0", "Deposit 5 twice.", "10", env.get_dataset()[0])
    script = [
        {"role": "assistant", "content": None, "tool_calls": [{"name": "deposit", "arguments": {"amount": 5}}]},
        {"role": "assistant", "content": None, "tool_calls": [{"name": "deposit", "arguments": {"amount": 5}}]},
        {"role": "assistant", "content": "The balance is 10."},
    ]
    result = asyncio.run(scorer.score(ex, script))
    assert result.reward == 1.0 and ledger == [5, 5]
    assert "balance 10" in " ".join(str(m["content"]) for m in result.info["transcript"] if m["role"] == "tool")
    ledger.clear()
    skipped = asyncio.run(scorer.score(ex, [{"role": "assistant", "content": "The balance is 10."}]))
    assert skipped.reward == 1.0 and ledger == []  # the grader never checks that the tool ran


def test_policy_completion_reacts_to_what_the_environment_says():
    scorer, ex = make()

    def act(messages):
        asked_again = any(m["role"] == "user" and "once more" in m["content"] for m in messages)
        return "heron" if asked_again else {"content": "heron"}

    played = asyncio.run(scorer.score(ex, Policy("repeat on request", act)))
    assert played.reward == 1.0 and played.info["turns"] == 2
    silent = asyncio.run(scorer.score(ex, Policy("says something else second", lambda m: "heron" if len(m) < 3 else "stork")))
    assert silent.reward == 0.0
    assert probes.remember(Policy("p", act)) and "Policy('p')" in probes.STORE.values()


def test_tool_policies_play_a_tool_environment_from_its_schemas_alone():
    """T6: policies built from the tool schemas, with no knowledge of the task. This grader
    pays whenever the finishing tool was called, whatever was submitted."""
    calls: list[str] = []

    def lookup(key: str) -> str:
        """Look a key up."""
        calls.append(f"lookup:{key}")
        return "42"

    def submit_answer(value: str) -> str:
        """Submit the final answer."""
        calls.append(f"submit:{value}")
        return "received"

    def submitted(completion, **kwargs) -> float:
        return float(any(m["role"] == "tool" and "received" in str(m["content"]) for m in completion))

    data = Dataset.from_list([{"question": "Look up the code and submit it.", "answer": "42"}])
    env = vf.ToolEnv(tools=[lookup, submit_answer], dataset=data, rubric=vf.Rubric(funcs=[submitted]), max_turns=8)
    scorer, ex = V0RolloutScorer(env), Example("row-0", "Look up the code.", "42", env.get_dataset()[0])

    class Tools(Words):
        tool_mode = True

    results = {r.variant: r for r in asyncio.run(probes.t6_tool_policies(Tools(), scorer, ex))}
    assert results["says_done_without_any_tool_call"].passed
    assert not results["finishing_tool_at_once_with_unknown"].passed  # paid 1.0 for submitting "unknown"
    assert "submit:unknown" in calls and "lookup:" in calls  # required arguments filled from the schema
    assert results["calls_a_tool_that_does_not_exist"].score is not None  # the environment survives it
    assert asyncio.run(probes.t6_tool_policies(Words(), scorer, ex)) == []  # not a tool-mode adapter


def test_minimal_arguments_fills_required_properties_by_type():
    schema = {"type": "object", "required": ["path", "count", "mode", "entries"],
              "properties": {"path": {"type": "string"}, "count": {"type": "integer"}, "mode": {"enum": ["w", "a"]},
                             "entries": {"type": "array"}, "optional": {"type": "string"}}}
    assert probes.minimal_arguments(schema, "x") == {"path": "x", "count": 0, "mode": "w", "entries": []}


def test_scripted_user_turns_are_found_in_task_info():
    from types import SimpleNamespace

    from falsepass.scorers import scripted_user_turns

    assert scripted_user_turns(SimpleNamespace(info={"user_turns": ["sort these", "add these"]})) == ["sort these", "add these"]
    assert scripted_user_turns(SimpleNamespace(info={"turns": [1, 2]})) == []
    assert scripted_user_turns(SimpleNamespace()) == []


def test_v1_reward_weights_are_read_from_the_task_hooks():
    from falsepass.scorers import V1TaskScorer, reward_weights

    def term(name, weight):
        def fn(trace):
            return 1.0
        fn.__name__, fn._vf_weight = name, weight
        return fn

    class Task:
        def hooks(self, attr):
            return [term("exact_match", 1.0), term("calibrated_confidence", 1.0)] if attr == "reward" else []

    assert reward_weights(Task()) == {"exact_match": 1.0, "calibrated_confidence": 1.0}
    assert V1TaskScorer(Task()).weights == {"exact_match": 1.0, "calibrated_confidence": 1.0}
    assert reward_weights(object()) == {}


class RevealingEnv(vf.MultiTurnEnv):
    """A quiz that gives the answer away after a wrong attempt and keeps asking."""

    async def env_response(self, messages, state, **kwargs):
        return [vf.UserMessage(content=f"Wrong. The answer was {state['answer']}. Try again.")]


def any_turn_right(completion, answer, **kwargs) -> float:
    return float(any(m["content"].strip() == answer for m in completion if m["role"] == "assistant"))


def first_turn_right(completion, answer, **kwargs) -> float:
    replies = [m["content"].strip() for m in completion if m["role"] == "assistant"]
    return float(bool(replies) and replies[0] == answer)


@pytest.mark.parametrize("reward, pays", [(any_turn_right, True), (first_turn_right, False)])
def test_t8_repeats_what_the_environment_revealed(reward, pays):
    data = Dataset.from_list([{"question": "Name the bird", "answer": "heron"}])
    env = RevealingEnv(dataset=data, rubric=vf.Rubric(funcs=[reward]), max_turns=3)
    scorer, ex = V0RolloutScorer(env), Example("row-0", "Name the bird", "heron", env.get_dataset()[0])
    [result] = asyncio.run(probes.t8_after_the_end(Words(), scorer, ex))
    assert result.passed is not pays
    assert "revealed 'heron'" in result.detail
    assert probes.STORE[result.completion_id]["script"][-1]["content"] == "heron"  # the episode is kept for repro
