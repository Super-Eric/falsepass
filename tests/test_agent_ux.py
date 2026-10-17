"""What an agent sees: previews that keep the answer, safe text, forgiving names, clear errors."""

import json

from falsepass import cli, report
from falsepass.mcp_server import _safe
from falsepass.probes import preview


def test_preview_keeps_the_end_where_the_answer_is():
    text = "reasoning " * 60 + "#### 107 = 100"
    shown = preview(text)
    assert shown.startswith("reasoning") and shown.endswith("#### 107 = 100") and f"[{len(text)} chars]" in shown
    assert preview("short") == "short"


def test_digest_previews_the_full_completion_and_names_it():
    full = "x" * 900 + " the end"
    row = {"probe": "P5", "name": "shotgun", "variant": "v", "expected": "zero", "passed": False, "score": 1.0,
           "detail": "", "example": "e1", "completion": "x" * 300 + "…", "completion_id": "abc", "answer_free": False,
           "metrics": {}, "errors": []}
    payload = {"meta": {"environment": "t"}, "results": [row], "static": []}
    failing = report.digest(payload, completions={"abc": full})["failing"][0]
    assert failing["completion_id"] == "abc" and failing["completion"].endswith("the end")


def test_lone_surrogates_are_escaped_for_json_rpc():
    safe = _safe({"failing": [{"completion": "bad \ud800 char"}]})
    json.dumps(safe).encode("utf-8")  # would raise on a raw surrogate
    assert "\\ud800" in safe["failing"][0]["completion"]


def test_names_with_underscores_find_hyphenated_audits(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "audits" / "arc-easy").mkdir(parents=True)
    (tmp_path / "audits" / "arc-easy" / "env.toml").write_text("[env]\n")
    assert cli.find_audit("arc_easy") == tmp_path.joinpath("audits", "arc-easy").relative_to(tmp_path)
    assert cli.main(["results", "arc_easy"]) == 1
    assert "no completed audit" in capsys.readouterr().err
    assert cli.main(["results", "nothing"]) == 1
    assert "set up here: arc-easy" in capsys.readouterr().err


def test_init_refuses_to_overwrite_without_force(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    source = cli.demo_source()
    assert cli.main(["init", str(source), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["name"] == "falsepass-toy-env"
    assert cli.main(["init", str(source), "--json"]) == 0  # the same target again: nothing to do, not an error
    again = json.loads(capsys.readouterr().out)
    assert again["existing"] is True and again["name"] == "falsepass-toy-env"
    env_toml = tmp_path / "audits" / "falsepass-toy-env" / "env.toml"
    env_toml.write_text(env_toml.read_text().replace('id = "', 'id = "other/', 1))  # set up for something else
    assert cli.main(["init", str(source)]) == 1
    assert "--force" in capsys.readouterr().err
    assert cli.main(["init", str(source), "--force"]) == 0


def test_inconclusive_digest_keeps_only_what_holds():
    rows = [
        {"probe": "P3", "name": "empty", "variant": "empty", "expected": "zero", "passed": False, "score": 1.0,
         "detail": "", "example": "e", "completion": "", "completion_id": "", "answer_free": True, "metrics": {}, "errors": []},
        {"probe": "P6", "name": "format_only", "variant": "wrong", "expected": "zero", "passed": False, "score": 1.0,
         "detail": "", "example": "e", "completion": "x", "completion_id": "", "answer_free": False, "metrics": {}, "errors": []},
    ]
    payload = {"meta": {"environment": "t", "adapter": {"validity": "inconclusive: the reference scored 1.000 ..."}},
               "results": rows, "static": []}
    digest = report.digest(payload)
    assert digest["status"] == "inconclusive" and digest["headline"] is None and digest["failing"] == []
    assert digest["facts_that_hold"] == ["P3 empty/empty: expected zero, got 1 on 1 of 1 tasks"]
    assert "--policies" in digest["next"]


def test_a_policy_episode_is_kept_and_replayed_as_a_script():
    from falsepass import probes, repro
    from falsepass.model import Policy

    probes.STORE.clear()
    policy = Policy("p", lambda messages: "hi")
    script = [{"role": "assistant", "content": "GUESS: crane"}, {"role": "assistant", "content": "GUESS: fifth"}]
    key = probes.remember_episode(policy, {"script": script, "transcript": [{"role": "user", "content": "go"}, *script]})
    assert probes.STORE[key]["script"] == script
    row = {"probe": "X1", "name": "policy", "variant": "p", "example": "row-1", "expected": "zero", "passed": False,
           "score": 0.7, "completion_id": key}
    cases = repro.select([row], dict(probes.STORE), None, 2)
    assert cases[0]["completion"] == script


def test_ready_made_policies():
    from falsepass import policies

    convo = [{"role": "user", "content": "Guess the word."}, {"role": "assistant", "content": "GUESS: crane"},
             {"role": "user", "content": "Game over! The word was FIFTH."}]
    assert policies.revealed(convo) == "FIFTH" and policies.TERMINAL.search(convo[-1]["content"])
    assert policies.after_the_end("GUESS: crane", lambda w: f"GUESS: {w.lower()}").act(convo) == "GUESS: fifth"
    assert policies.after_the_end("GUESS: crane").act(convo[:1]) == "GUESS: crane"
    talker = policies.keeps_talking("GUESS: crane", turns=2)
    assert [talker.act(convo[:1]), talker.act(convo), talker.act(convo + convo[1:])] == ["GUESS: crane", "Thanks! Let me keep going.", ""]
    variants = policies.special_numbers(["lookup #12", "ACTION: issue_refund #350352 580.62"])
    assert variants["nan"][-1]["content"] == "ACTION: issue_refund #350352 nan"
    assert variants["inf"][0]["content"] == "lookup #12"  # only the acting message changes


def test_policy_skeleton_is_valid_python(tmp_path, monkeypatch, capsys):
    import ast

    monkeypatch.chdir(tmp_path)
    assert cli.main(["init", str(cli.demo_source()), "--policies", "--json"]) == 0
    name = json.loads(capsys.readouterr().out)["name"]
    source = (tmp_path / "audits" / name / "adapter.py").read_text()
    ast.parse(source)
    assert "policies.after_the_end" in source and "special_number_scripts" in source
