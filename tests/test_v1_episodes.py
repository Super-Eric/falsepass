"""Whole v1 episodes played by policies (CI installs verifiers 0.4 and examples/v1-tool-toy for the tests that need them)."""

import asyncio
import json
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from falsepass import cli
from falsepass.model import Example, Expect, Policy, ProbeResult
from falsepass.v1_episodes import Player, PolicyEndpoint, plain_messages

TOY = Path(__file__).resolve().parent.parent / "examples" / "v1-tool-toy"


def post(url: str, body: dict) -> tuple[int, bytes]:
    request = urllib.request.Request(url + "/chat/completions", json.dumps(body).encode(), {"content-type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as reply:
            return reply.status, reply.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def test_the_endpoint_answers_as_the_policy_both_streamed_and_not():
    endpoint = PolicyEndpoint()
    try:
        seen = {}

        def act(messages, tools):
            seen["messages"], seen["tools"] = messages, tools
            return {"content": "", "tool_calls": [{"name": "lookup", "arguments": {"id": 7}}]} if len(messages) < 3 else "Done."

        name = endpoint.add(Player(Policy("p", act, wants_tools=True)))
        tools = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]
        status, body = post(endpoint.url, {"model": name, "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}], "tools": tools})
        message = json.loads(body)["choices"][0]["message"]
        assert status == 200 and message["tool_calls"][0]["function"] == {"name": "lookup", "arguments": '{"id": 7}'}
        assert seen["messages"] == [{"role": "user", "content": "hi"}] and seen["tools"] == [{"name": "lookup", "parameters": {"type": "object"}}]
        history = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": None, "tool_calls": [message["tool_calls"][0]]},
                   {"role": "tool", "tool_call_id": "call_1_0", "content": "found"}]  # fmt: skip
        status, body = post(endpoint.url, {"model": name, "messages": history, "stream": True})
        assert status == 200 and b'"content": "Done."' in body and body.rstrip().endswith(b"data: [DONE]")
        assert seen["messages"][1]["tool_calls"] == [{"name": "lookup", "arguments": {"id": 7}}]
        assert post(endpoint.url, {"model": "nobody", "messages": []})[0] == 404
    finally:
        endpoint.close()


def test_a_script_repeats_its_last_line_a_broken_policy_and_the_turn_cap_are_errors():
    player = Player([{"role": "assistant", "content": "a"}, {"role": "assistant", "content": "b"}])
    assert [player.next([], [])["content"] for _ in range(3)] == ["a", "b", "b"]
    assert player.replies[-1] == {"content": "b"}
    endless = Player(Policy("endless", lambda messages: "more"))
    with pytest.raises(RuntimeError, match="did not stop"):
        for _ in range(100):
            endless.next([], [])
    assert plain_messages([{"role": "assistant", "content": None}]) == [{"role": "assistant", "content": ""}]


def test_tool_rows_on_refusal_tasks_are_not_counted():
    from falsepass import probes, report
    from falsepass.adapter import Adapter

    def row(example, variant, score, expected=Expect.ZERO):
        return ProbeResult("T6", "tool_policy", variant, example, expected, score, score <= 0)

    results = [row("task-0", probes.IDLE, 1.0), row("task-0", "every_tool", 1.0), row("task-1", probes.IDLE, 0.0),
               row("task-1", "every_tool", 1.0)]  # fmt: skip
    assert probes.mark_no_control(results, Adapter()) == 2  # doing nothing pays on task-0 only: a refusal task
    assert [r.no_control for r in results] == [True, True, False, False]
    head = report.headline([r.to_dict() for r in results])
    assert head["false_pass_ceiling"] == 1.0 and head["no_control"] == 2 and head["no_control_tasks"] == 1
    everywhere = [row("task-0", probes.IDLE, 1.0), row("task-1", probes.IDLE, 1.0)]
    assert probes.mark_no_control(everywhere, Adapter()) == 0  # paid for doing nothing on every task: a false pass
    declared = Adapter()
    declared.refusal_tasks = {"task-1"}
    assert probes.mark_no_control(everywhere, declared) == 1 and everywhere[1].no_control


def test_a_v1_tool_folder_gets_the_v1_policy_skeleton(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["init", str(TOY), "--policies"]) == 0
    audit = tmp_path / "audits" / "falsepass-v1-tool-toy"
    adapter = (audit / "adapter.py").read_text()
    assert "V1EpisodeScorer(MODULE, LOAD_ARGS, RUNTIME)" in adapter and 'MODULE = "falsepass_v1_tool_toy"' in adapter
    compile(adapter, "adapter.py", "exec")
    env = tomllib.loads((audit / "env.toml").read_text())["env"]
    assert cli.install_args(env)[2:4] == ["--with-editable", str(TOY)]  # a folder: edits are picked up on every run


@pytest.fixture(scope="module")
def toy_tasks():
    pytest.importorskip("falsepass_v1_tool_toy", reason="needs `--with verifiers>=0.4 --with-editable examples/v1-tool-toy`")
    from falsepass.auto import open_taskset

    return list(open_taskset("falsepass_v1_tool_toy").load())


def test_episodes_run_the_taskset_tools_and_reward(toy_tasks):
    from falsepass.v1_episodes import V1EpisodeScorer

    task = toy_tasks[0]  # placed 3 days ago: a refund is owed
    example = Example("task-0", task.data.prompt_text, "", task)
    order = task.data.order

    def solver(messages, tools):
        turn = sum(1 for m in messages if m["role"] == "assistant")
        if turn == 0:
            return {"content": "", "tool_calls": [{"name": "lookup_order", "arguments": {"order_id": order["id"]}}]}
        if turn == 1:
            assert "3 days ago" in messages[-1]["content"]  # the tool really ran
            return {"content": "", "tool_calls": [{"name": "issue_refund", "arguments": {"order_id": order["id"], "amount": 1e9}}]}
        return "Refunded."

    async def run():
        scorer = V1EpisodeScorer("falsepass_v1_tool_toy")
        try:
            over = await scorer.score(example, Policy("over_refund", solver, wants_tools=True))
            idle = await scorer.score(example, "Done.")
            replay = await scorer.score(example, over.info["script"])
            return over, idle, replay
        finally:
            await scorer.close()

    over, idle, replay = asyncio.run(run())
    assert over.reward == 0.0 and over.info["turns"] == 3 and "issue_refund" in over.info["tools"]  # refunded more than paid
    assert idle.reward == 1.0 and idle.info["turns"] == 1  # the planted defect: nothing done, full reward
    assert replay.reward == 0.0 and replay.info["script"] == over.info["script"]  # a recorded episode replays


def test_automatic_mode_plays_a_v1_tool_taskset_and_repro_replays_it(toy_tasks, tmp_path: Path, monkeypatch):
    from falsepass import runner

    monkeypatch.chdir(tmp_path)
    assert cli.main(["init", str(TOY)]) == 0
    audit = tmp_path / "audits" / "falsepass-v1-tool-toy"
    runner.run(audit, n=2, seed=0)
    payload = json.loads((audit / "probe-results.json").read_text())
    assert payload["meta"]["loader"]["kind"] == "v1-episode"
    idle = [r for r in payload["results"] if r["variant"] == "says_done_without_any_tool_call"]
    assert len(idle) == 2 and all(r["score"] == 1.0 and not r["passed"] for r in idle)  # paid on every task: counted
    assert not any(r["probe"] == "P12" for r in payload["results"] if not r["passed"])
    from falsepass import repro

    script = repro.generate(audit, probes=["T6"], per_group=1)
    out = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stderr[-2000:]
    assert "1.000000  expected 0     T6 tool_policy/says_done_without_any_tool_call" in out.stdout
