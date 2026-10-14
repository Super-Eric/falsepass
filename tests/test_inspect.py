"""Inspect AI tasks: init without inspect_ai installed, scoring with it (CI installs inspect-ai for this file)."""

import asyncio
import tomllib
from pathlib import Path

import pytest

from falsepass import cli, hub, inspect_support

TOY = Path(__file__).resolve().parent.parent / "examples" / "inspect-toy" / "toy_task.py"


def test_task_files_and_projects_are_recognised(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(hub, "pypi_version", lambda package: "9.9.9")
    assert inspect_support.task_functions(TOY) == ["toy_sums"] and inspect_support.uses_inspect(TOY)
    info = inspect_support.describe_file(TOY)
    assert info["task"] == f"{TOY}@toy_sums" and info["framework"] == "inspect" and info["inspect_ai"] == "9.9.9"
    project = tmp_path / "proj"
    (project / "src" / "my_eval").mkdir(parents=True)
    (project / "pyproject.toml").write_text('[project]\nname = "my-eval"\nversion = "1.0"\ndependencies = ["inspect-ai>=0.3"]\n')
    (project / "src" / "my_eval" / "tasks.py").write_text("from inspect_ai import task\n\n@task\ndef first():\n    pass\n\n@task()\ndef second():\n    pass\n")
    assert inspect_support.uses_inspect(project)
    info = inspect_support.describe_folder(project, "second")
    assert info["task"] == "my_eval.tasks:second" and info["others"] == ["my_eval.tasks:first"]
    assert info["source"] == str(project.resolve())
    reg = inspect_support.describe_registry("inspect_evals/gsm8k")
    assert (reg["id"], reg["package"], reg["version"]) == ("inspect/inspect_evals/gsm8k", "inspect-evals", "9.9.9")


def test_init_writes_an_inspect_env_toml_that_installs_inspect(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(hub, "pypi_version", lambda package: "0.3.1")
    monkeypatch.chdir(tmp_path)
    assert cli.main(["init", "inspect_evals/arc_easy", "--with", "inspect-evals[math]"]) == 0
    env = tomllib.loads((tmp_path / "audits/arc-easy/env.toml").read_text())["env"]
    assert env["framework"] == "inspect" and env["task"] == "inspect_evals/arc_easy"
    args = cli.install_args(env)
    assert "inspect-ai==0.3.1" in args and "inspect-evals==0.3.1" in args and "inspect-evals[math]" in args
    assert not any(a.startswith("verifiers") for a in args)


def test_the_toy_task_is_scored_through_its_own_solver_and_scorer():
    pytest.importorskip("inspect_ai")
    from falsepass.model import Example

    scorer, pool = inspect_support.load(f"{TOY}@toy_sums", {"n": 5}, 100)
    example: Example = pool[0]
    assert example.prompt.count("+") == 1 and example.answer.isdigit()

    def score(text):
        return asyncio.run(scorer.score(example, text)).reward

    assert score(example.answer) == 1.0
    assert score("12345") == 0.0
    assert score(f"1 2 3 {example.answer} 4") == 1.0  # the planted defect: includes() is a substring test
    assert asyncio.run(scorer.score(example, [{"role": "user", "content": "x"}])).errors == []  # no reply: scored, not raised
    swapped = inspect_support.with_reference(example, "7")
    assert asyncio.run(scorer.score(swapped, "7")).reward == 1.0
