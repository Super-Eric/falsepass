"""Plain reward functions (TRL GRPOTrainer style): init, the trainer's calling convention, datasets."""

import asyncio
import json
import os
import sys
import tomllib
from pathlib import Path

import pytest

from falsepass import cli, reward_support, static

TOY = Path(__file__).resolve().parent.parent / "examples" / "reward-toy"


def test_a_reward_file_is_recognised_and_written_as_reward(tmp_path: Path, monkeypatch):
    assert reward_support.uses_reward_functions(TOY / "rewards.py")
    assert not reward_support.uses_reward_functions(TOY / "README.md")
    assert reward_support.declared_functions(TOY / "rewards.py") == ["correctness_reward", "format_reward"]
    monkeypatch.chdir(tmp_path)
    assert cli.main(["init", str(TOY / "rewards.py")]) == 1  # no dataset: a one-line message, not a traceback
    assert cli.main(["init", str(TOY / "rewards.py"), "--dataset", str(TOY / "data.jsonl"), "--func", "correctness_reward",
                     "--func", "format_reward", "--weight", "2", "--weight", "0.5"]) == 0  # fmt: skip
    env = tomllib.loads((tmp_path / "audits/rewards/env.toml").read_text())["env"]
    assert env["framework"] == "reward" and env["task"] == str(TOY / "rewards.py") and env["dataset"] == str(TOY / "data.jsonl")
    assert env["functions"] == ["correctness_reward", "format_reward"] and env["weights"] == [2.0, 0.5]
    assert cli.install_args(env) == []  # the file imports only the standard library
    assert cli.main(["init", str(TOY / "rewards.py"), "--dataset", "x.jsonl", "--func", "nope", "--name", "other"]) == 1
    patched = tmp_path / "patched.py"
    assert cli.source_command(tmp_path / "audits/rewards", [], "falsepass.runner", patched)[:2] == ["env", f"FALSEPASS_SOURCE={patched}"]


def test_imports_become_requirements_and_hub_datasets_add_datasets(tmp_path: Path, monkeypatch):
    source = tmp_path / "r.py"
    source.write_text("import re, json\nimport numpy as np\nfrom math_verify import parse\nfrom sklearn import metrics\n"
                      "import helper\n\ndef r(completions, **kw):\n    return [0.0]\n")  # fmt: skip
    (tmp_path / "helper.py").write_text("")
    assert reward_support.third_party_imports(source) == ["numpy", "math-verify", "scikit-learn"]
    info = reward_support.describe(str(source), "hf:openai/gsm8k", split="test", config="main")
    assert info["extra"] == ["numpy", "math-verify", "scikit-learn", "datasets"]
    assert info["dataset"] == "hf:openai/gsm8k" and info["split"] == "test" and info["dataset_config"] == "main"
    from falsepass import hub

    monkeypatch.setattr(hub, "pypi_version", lambda name: "1.0" if name == "trl" else (_ for _ in ()).throw(RuntimeError(name)))
    module = reward_support.describe("trl.rewards", "data.jsonl")
    assert module["task"] == "trl.rewards" and module["id"] == "local/rewards" and module["extra"] == ["trl"]
    assert reward_support.describe("my_private.rewards", "data.jsonl")["extra"] == []


def test_final_answers_are_read_from_worked_solutions():
    assert reward_support.final_answer("42") == ("42", "")
    worked = "We add the parts.\nSo the total is $\\boxed{\\frac{1}{2}}$ and we are done."
    assert reward_support.final_answer(worked) == ("\\frac{1}{2}", "last \\boxed{} of the worked solution")
    gsm = "Natalia sold 48/2 = 24 clips in May.\nAltogether she sold 48+24 = 72 clips.\n#### 72"
    assert reward_support.final_answer(gsm) == ("72", "what follows #### in the worked solution")
    assert reward_support.prompt_text([{"role": "system", "content": "S"}, {"role": "user", "content": "Q"}]) == "S\nQ"


def _load(monkeypatch, **kwargs):
    monkeypatch.chdir(os.getcwd())  # load() works in the reward file's folder; restore afterwards
    return reward_support.load(str(TOY / "rewards.py"), 100, str(TOY / "data.jsonl"), **kwargs)


def test_the_functions_are_called_as_the_trainer_calls_them(monkeypatch):
    scorer, pool, found = _load(monkeypatch, functions=["correctness_reward", "format_reward"], weights=[2.0, 0.5])
    assert len(pool) == 12 and pool[2].answer == "42" and "<answer>" in pool[2].prompt
    assert found["conversational"] and found["answer_column"] == "answer" and found["prompt_column"] == "prompt"

    def score(text):
        return asyncio.run(scorer.score(pool[2], text))

    good = "<think>6 times 7</think>\n<answer>42</answer>"
    assert score(good).reward == 2.5 and score(good).metrics == {"correctness_reward": 1.0, "format_reward": 1.0}
    assert score("<think>x</think>\n<answer>41</answer>").reward == 0.5
    assert score("<think>x</think>\n<answer>41 or 42</answer>").reward == 2.5  # the planted defect
    arguments = scorer.arguments(pool[2], "hi")
    assert arguments["completions"] == [[{"role": "assistant", "content": "hi"}]] and arguments["answer"] == ["42"]
    assert set(arguments) >= {"prompts", "completion_ids", "trainer_state", "log_extra", "log_metric"} and "prompt" not in arguments


def test_none_nan_signatures_and_async(tmp_path: Path, monkeypatch):
    source = tmp_path / "odd.py"
    source.write_text('''
import math


def skips(completions, **kwargs):
    return [None]


def not_a_number(completions, **kwargs):
    return [math.nan]


def strict_signature(completions, answer):  # no **kwargs: GRPOTrainer would raise TypeError
    return [1.0 if completions[0] == answer[0] else 0.0]


async def later(completions, **kwargs):
    return [0.25]
''')
    (tmp_path / "rows.json").write_text(json.dumps({"data": [{"question": "q", "answer": "a"}]}))
    monkeypatch.chdir(tmp_path)
    scorer, pool, found = reward_support.load(str(source), 10, str(tmp_path / "rows.json"))
    assert found["functions"] == ["skips", "not_a_number", "strict_signature", "later"] and not found["conversational"]
    result = asyncio.run(scorer.score(pool[0], "a"))
    assert result.reward == 1.25 and result.metrics == {"strict_signature": 1.0, "later": 0.25}
    assert "prompts" in scorer.refused["strict_signature"]
    scorer, pool, _ = reward_support.load(str(source), 10, str(tmp_path / "rows.json"), functions=["skips", "not_a_number"])
    result = asyncio.run(scorer.score(pool[0], "a"))
    assert result.reward is None and "every reward function returned None" in result.errors[0]


def test_call_and_map_datasets_and_csv(tmp_path: Path, monkeypatch):
    source = tmp_path / "train.py"
    source.write_text('''
def get_rows(split="train"):
    return [{"question": f"{split} {i}", "raw": f"steps\\n#### {i}"} for i in range(3)]


def to_prompt(row):
    return {"prompt": [{"role": "user", "content": row["question"]}], "answer": row["raw"].split("####")[1].strip()}


def exact(prompts, completions, answer, **kwargs):
    return [1.0 if c[0]["content"].strip() == a else 0.0 for c, a in zip(completions, answer)]
''')
    monkeypatch.chdir(tmp_path)
    scorer, pool, found = reward_support.load(str(source), 10, "call:get_rows", split="test", mapper="to_prompt")
    assert [e.prompt for e in pool] == ["test 0", "test 1", "test 2"] and [e.answer for e in pool] == ["0", "1", "2"]
    assert found["conversational"] and asyncio.run(scorer.score(pool[1], "1")).reward == 1.0
    (tmp_path / "rows.csv").write_text("problem,solution\nhow many?,7\n")
    scorer, pool, found = reward_support.load(str(source), 10, str(tmp_path / "rows.csv"), functions=["exact"], answer="solution")
    assert found["prompt_column"] == "problem" and pool[0].answer == "7"
    with pytest.raises(RuntimeError, match="no column 'nope'"):
        reward_support.load(str(source), 10, str(tmp_path / "rows.csv"), functions=["exact"], answer="nope")


def test_a_swapped_reference_reaches_the_functions(monkeypatch):
    from falsepass.auto import AutoAdapter

    monkeypatch.chdir(os.getcwd())
    adapter = AutoAdapter({"id": "local/rewards", "framework": "reward", "task": str(TOY / "rewards.py"), "dataset": str(TOY / "data.jsonl"),
                           "functions": ["correctness_reward"]})  # fmt: skip
    scorer, examples = adapter.load(4, 0)
    swapped = adapter.with_reference(examples[0], "777")
    assert swapped.raw["row"]["answer"] == "777" and examples[0].raw["row"]["answer"] != "777"
    assert asyncio.run(scorer.score(swapped, "<answer>777</answer>")).reward == 1.0
    assert adapter.loader["kind"] == "reward" and set(adapter.loader["rows"]) == {e.key for e in examples}


def test_source_files_of_installed_functions(monkeypatch):
    scorer, _, _ = _load(monkeypatch, functions=["correctness_reward"])
    files = reward_support.source_files(scorer.funcs)
    assert list(files.values()) == [TOY / "rewards.py"]


def test_the_static_scan_treats_trainer_functions_as_reward_terms():
    hints = {(h.rule, h.where.split(" ")[0]) for h in static.scan(TOY / "rewards.py")}
    assert ("reference_substring_match", "correctness_reward") in hints and ("term_ignores_reference", "format_reward") in hints


@pytest.fixture(autouse=True)
def _forget_loaded_files():
    yield
    for name in [n for n in sys.modules if n.startswith("falsepass_rewards_")]:
        del sys.modules[name]
