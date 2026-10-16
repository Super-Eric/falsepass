"""Local environments, install paths and the machine-readable outputs."""

import json
import tomllib
from pathlib import Path

from falsepass import cli, hub, report, verify


def write_env(root: Path, requirement: str = "verifiers>=0.3.1", layout: str = "flat") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "my-env"\nversion = "0.2.0"\ndependencies = ["{requirement}", "datasets"]\n'
    )
    code = "def load_environment(**kwargs):\n    return None\n"
    if layout == "flat":
        (root / "my_env.py").write_text(code)
    elif layout == "src":
        (root / "src" / "my_env").mkdir(parents=True)
        (root / "src" / "my_env" / "__init__.py").write_text(code)
    else:  # a module whose name differs from the package
        (root / "environment.py").write_text(code)
    return root


def test_verifiers_pin_keeps_a_floor_above_the_default():
    assert hub.verifiers_pin(">=0.3.0") == hub.DEFAULT_VERIFIERS
    assert hub.verifiers_pin(">=0.4.0") == "0.4.0"  # 0.3.1 would not install next to it
    assert hub.verifiers_pin("==0.2.5") == "0.2.5"
    assert hub.verifiers_pin(">=0.1.15.dev3") == "0.1.15.dev3"
    assert hub.declared_verifiers(["datasets", "verifiers[rl] >= 0.4.0", "verifiers-extra"]) == "verifiers[rl] >= 0.4.0"


def test_local_environment_is_described_from_its_pyproject(tmp_path: Path):
    for layout, module in (("flat", "my_env"), ("src", "my_env"), ("other", "environment")):
        info = hub.describe_local(write_env(tmp_path / layout, "verifiers>=0.4.0", layout))
        assert (info["id"], info["package"], info["version"], info["module"]) == ("local/my-env", "my-env", "0.2.0", module)
        assert info["verifiers"] == "0.4.0" and info["source"] == str((tmp_path / layout).resolve())


def test_init_on_a_folder_writes_an_env_toml_that_installs_the_folder(tmp_path: Path, monkeypatch):
    source = write_env(tmp_path / "work" / "my-env")
    monkeypatch.chdir(tmp_path)
    assert cli.main(["init", str(source)]) == 0
    env = tomllib.loads((tmp_path / "audits/my-env/env.toml").read_text())["env"]
    assert env["source"] == str(source.resolve()) and "index" in env
    assert str(source.resolve()) in cli.install_args(env)
    assert cli.main(["init", "not-a-slug-or-folder"]) == 1


def test_a_checkout_runs_audits_with_itself():
    assert cli.self_install_args()[0] == "--with-editable"
    assert (Path(cli.self_install_args()[1]) / "falsepass" / "cli.py").is_file()


def test_verify_fix_raises_verifiers_when_the_patched_source_needs_it(tmp_path: Path):
    audit = tmp_path / "audit"
    audit.mkdir()
    (audit / "env.toml").write_text('[env]\nid = "o/my-env"\npackage = "my-env"\nversion = "0.2.0"\nverifiers = "0.3.1"\n')
    (audit / "repro.output.txt").write_text("# uv run -q --isolated --with verifiers==0.3.1 --with my-env==0.2.0 python r.py\nA\n")
    source = write_env(tmp_path / "patched", "verifiers>=0.4.0")
    command = verify.command_for(audit, "repro.py", source)
    assert "verifiers==0.4.0" in command and "verifiers==0.3.1" not in command
    (audit / "repro.output.txt").write_text("A\n")  # built from env.toml
    assert "verifiers==0.4.0" in verify.command_for(audit, "repro.py", source)
    source = write_env(tmp_path / "same", "verifiers>=0.3.0")
    assert "verifiers==0.3.1" in verify.command_for(audit, "repro.py", source)


def payload() -> dict:
    row = {"probe": "P5", "name": "shotgun", "variant": "all_in_one_slot", "expected": "zero", "example": "row-1",
           "score": 1.0, "passed": False, "detail": "expected zero, got 1.0", "completion": "1 2 3", "metrics": {},
           "errors": [], "answer_free": False, "completion_id": "c1"}  # fmt: skip
    ok = {**row, "probe": "P1", "name": "ground_truth", "variant": "reference", "expected": "full", "passed": True}
    return {"meta": {"environment": "local/my-env"}, "results": [row, ok], "static": [], "rewardlint": None}


def test_digest_lists_failing_probes_with_one_completion(tmp_path: Path, monkeypatch, capsys):
    digest = report.digest(payload())
    assert digest["headline"]["false_pass_ceiling"] == 1.0
    assert [(f["probe"], f["failed"], f["runs"], f["completion"]) for f in digest["failing"]] == [("P5", 1, 1, "1 2 3")]
    (tmp_path / "audits/my-env").mkdir(parents=True)
    (tmp_path / "audits/my-env/probe-results.json").write_text(json.dumps(payload()))
    monkeypatch.chdir(tmp_path)
    assert cli.main(["results", "my-env", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["failing"][0]["variant"] == "all_in_one_slot"


def test_scan_json_on_the_bundled_toy_environment(capsys):
    assert cli.main(["scan", str(cli.demo_source()), "--json"]) == 0
    assert [h["rule"] for h in json.loads(capsys.readouterr().out)] == ["reference_substring_match"]


def test_verify_fix_lays_a_plain_folder_over_the_release(tmp_path: Path):
    """An audit's own `src/` has no pyproject.toml: the release stays installed (its
    dependencies are right) and the patched modules come first on PYTHONPATH."""
    audit = tmp_path / "audit"
    audit.mkdir()
    (audit / "env.toml").write_text('[env]\nid = "o/my-env"\npackage = "my-env"\nversion = "0.2.0"\nverifiers = "0.3.1"\n')
    (audit / "repro_auto.py").write_text("print('A')\n")
    assert verify.default_script(audit) == "repro_auto.py"
    (audit / "repro.py").write_text("print('A')\n")
    assert verify.default_script(audit) == "repro.py"
    plain = tmp_path / "src"
    (plain / "my_env").mkdir(parents=True)
    command = verify.command_for(audit, "repro.py", plain)
    assert command[:2] == ["env", f"PYTHONPATH={plain}"]
    assert "my-env==0.2.0" in command  # the release is kept
    (audit / "repro.output.txt").write_text("# uv run -q --isolated --with verifiers==0.3.1 --with my-env==0.2.0 python r.py\nA\n")
    command = verify.command_for(audit, "repro.py", plain)
    assert command[:2] == ["env", f"PYTHONPATH={plain}"] and "my-env==0.2.0" in command
    assert verify.command_for(audit, "repro.py", None)[0] == "uv"  # the unpatched baseline
