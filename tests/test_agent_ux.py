"""What an agent sees: previews that keep the answer, safe text, forgiving names, clear errors."""

import json
import os

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


def test_a_folder_given_to_init_dir_points_at_the_audit_inside(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "triage" / "toy").mkdir(parents=True)
    (tmp_path / "triage" / "toy" / "env.toml").write_text("[env]\n")
    assert cli.main(["results", "triage"]) == 1
    err = capsys.readouterr().err
    assert "contains one" in err and str(tmp_path.joinpath("triage", "toy").relative_to(tmp_path)) in err


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


def _row(probe, variant, example, score, errors=()):
    return {"probe": probe, "name": "n", "variant": variant, "expected": "zero", "passed": score in (None, 0.0),
            "score": score, "detail": "", "example": example, "completion": "x", "completion_id": "",
            "answer_free": True, "metrics": {}, "errors": list(errors)}


def test_a_no_control_run_where_every_reply_scores_the_same_is_inconclusive():
    rows = [_row(p, "v", e, s) for e, s in (("a", -0.07), ("b", -0.02)) for p in ("P3", "P4", "T5")]
    payload = {"meta": {"environment": "t", "adapter": {"validity": "no control: the reference cannot be replayed"}},
               "results": rows, "static": []}
    digest = report.digest(payload)
    assert digest["status"] == "inconclusive" and "scored the same on each of 2 tasks" in digest["reason"]
    assert "scale_points" in digest["next"] and digest["failing"] == []
    rows[0]["score"] = -0.5  # one reply differs: the run says something
    assert report.digest(payload).get("status") != "inconclusive"


def test_a_trace_falsepass_cannot_build_is_not_blamed_on_the_environment():
    rows = [_row("P1", "reference", "a", None, ["could not build trace: AttributeError: 'T' object has no attribute 'key'"])] * 3
    digest = report.digest({"meta": {"environment": "t", "adapter": {"validity": "ok"}}, "results": rows, "static": []})
    assert digest["status"] == "inconclusive" and "falsepass's side" in digest["next"]


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


def test_offline_keeps_the_dataset_library_off_the_hub(tmp_path, monkeypatch, capsys):
    """`--offline` puts HF_HUB_OFFLINE=1 in front of the isolated command (and in the
    environment every child inherits), so a cached dataset loads without a Hub round trip."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.delenv("HF_DATASETS_OFFLINE", raising=False)
    folder = tmp_path / "audits" / "x"
    folder.mkdir(parents=True)
    folder.joinpath("env.toml").write_text('[env]\nid = "o/x"\npackage = "x"\nversion = "1.0"\nmodule = "x"\n'
                                           'index = "https://example.invalid/simple/"\nverifiers = "0.4.0"\n')
    assert cli.main(["audit", "x", "--print-command"]) == 0
    assert capsys.readouterr().out.startswith("uv run --isolated")
    assert cli.main(["audit", "x", "--offline", "--print-command"]) == 0
    assert capsys.readouterr().out.startswith("env HF_HUB_OFFLINE=1 HF_DATASETS_OFFLINE=1 uv run --isolated")
    assert os.environ["HF_HUB_OFFLINE"] == "1"
    assert cli.main(["reach", "x", "--probe", "P3", "--print-command"]) == 0  # the flag is per call
    assert capsys.readouterr().out.startswith("uv run --isolated")


def test_structured_answers_get_a_validity_note(tmp_path):
    """A plan or an action can have several valid forms; the only wrong answer automatic mode has
    is another task's reference. The digest and the draft say so instead of calling it a false pass."""
    from falsepass import draft

    row = {"probe": "P6", "name": "format_only", "variant": "wrong_in_format", "example": "task-0", "expected": "zero",
           "score": 1.0, "passed": False, "detail": "expected zero, got 1.0", "completion": '{"a": 1}', "metrics": {},
           "errors": [], "answer_free": False, "completion_id": ""}
    meta = {"environment": "o/e", "date": "2026-11-01", "versions": {}, "examples": 1, "seed": 0, "python": "3.12",
            "falsepass": "0", "max_reward": 1.0, "bad_tolerance": 0.0, "weights": {},
            "adapter": {"validity": "ok", "format": "plain reply", "examples_tried": 1, "reference_reward_by_format": {},
                        "answers": "JSON objects: another task's reference stands in for a wrong answer", "turns": "single-turn", "note": ""}}
    payload = {"meta": meta, "results": [row, {**row, "probe": "P3", "name": "empty", "variant": "empty"}], "static": [], "rewardlint": None}
    failing = {f["probe"]: f for f in report.digest(payload)["failing"]}
    assert "validity grader" in failing["P6"]["note"] and "note" not in failing["P3"]
    folder = tmp_path / "e"
    folder.mkdir()
    (folder / "probe-results.json").write_text(json.dumps(payload))
    text = draft.draft(folder).read_text()
    assert text.count("- Note: the reference is a structured answer") == 1  # on P6, not on P3
    plain = {**payload, "meta": {**meta, "adapter": {**meta["adapter"], "answers": "open"}}}
    assert "note" not in report.digest(plain)["failing"][0]
