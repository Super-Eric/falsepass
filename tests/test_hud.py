"""HUD environments: init without hud installed, grading with it (CI installs hud for this file)."""

import asyncio
import json
import os
import tomllib
from pathlib import Path

import pytest

from falsepass import cli, hub, hud_support, probes, static

TOY = Path(__file__).resolve().parent.parent / "examples" / "hud-toy"


def test_a_hud_folder_is_recognised_and_written_as_hud(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(hub, "pypi_version", lambda package: "0.6.0")
    assert hud_support.uses_hud(TOY) and not hud_support.uses_hud(TOY.parent / "toy-env")
    monkeypatch.chdir(tmp_path)
    assert cli.main(["init", str(TOY), "--answers", str(TOY / "answers.json")]) == 0
    env = tomllib.loads((tmp_path / "audits/hud-toy/env.toml").read_text())["env"]
    assert env["framework"] == "hud" and env["task"] == str(TOY / "tasks.py") and env["answers"] == str(TOY / "answers.json")
    assert cli.install_args(env) == ["--with", "hud==0.6.0"]


def test_static_rules_for_framework_substring_graders(tmp_path: Path):
    source = tmp_path / "grader.py"
    source.write_text(
        "from hud.graders import contains as has\nimport hud.graders as hg\nfrom inspect_ai.scorer import includes\n\n"
        "def a(answer, want):\n    return has(answer, want)\n\ndef b(answer, want):\n    return hg.contains_all(answer, [want])\n\n"
        "scorer = includes()\n"
    )
    assert sorted(h.rule for h in static.scan(source)) == ["hud_contains_grader", "hud_contains_grader", "inspect_includes_scorer"]
    assert [h.rule for h in static.scan(TOY / "env.py")] == ["reference_substring_match"]  # `x in answer` after `answer = yield`


def test_answer_free_sprays_need_no_reference():
    sprays = probes.answer_free_sprays("How many times does 'r' appear in 'strawberry'?")
    assert "17" in sprays["integers_0_to_1000"].split() and sprays["letters_a_to_z"].startswith("A B C")
    assert "strawberry" in sprays["prompt_words"]


def test_the_toy_environment_is_graded_by_its_template(monkeypatch):
    pytest.importorskip("hud")
    monkeypatch.chdir(os.getcwd())  # load() works from the environment's folder; restore afterwards
    answers = json.loads((TOY / "answers.json").read_text())
    scorer, pool = hud_support.load(str(TOY / "tasks.py"), 100, str(TOY / "answers.json"))
    assert len(pool) == len(answers) and "strawberry" in pool[0].prompt and pool[0].answer == answers[0]

    def score(text):
        return asyncio.run(scorer.score(pool[0], text)).reward

    try:
        assert score(pool[0].answer) == 1.0
        assert score("999") == 0.0
        assert score(probes.answer_free_sprays(pool[0].prompt)["integers_0_to_1000"]) == 1.0  # the planted defect
    finally:
        asyncio.run(scorer.close())
