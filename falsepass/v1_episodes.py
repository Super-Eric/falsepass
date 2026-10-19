"""Whole episodes of a `verifiers.v1` taskset, played by scripted policies.

`V1TaskScorer` scores a message trace built by hand: right for a task graded on one reply,
blind for a tool or multi-turn taskset, where the reward depends on what the tools did and
what the scripted user said. `V1EpisodeScorer` runs the environment's own episode instead
(its harness, its tool servers, its scripted user, its stop conditions, its rewards), as
`vf-eval` does, and answers the model's requests from a local OpenAI-compatible endpoint
that plays the policy. No model. Outside hosts are blocked through proxy settings (see
`block_network`) unless the audit allows them.

A completion is played as follows: a `Policy` reacts to each request (`act(messages)`, or
`act(messages, tools)` with `wants_tools`); a string or a message list is a fixed script,
one assistant message per model turn (the last one repeated).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from falsepass.model import Completion, Example, Policy, Score
from falsepass.scorers import TURN_CAP, as_messages, tool_schema

KEY_VAR = "FALSEPASS_POLICY_KEY"  # the endpoint ignores it; the client config wants one


def text_of(content: Any) -> str:
    """Message content as text: a string, or the text parts of a content list."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") if isinstance(p, dict) else str(getattr(p, "text", "") or "") for p in content)
    return str(content)


def plain_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Chat-completions request messages as the plain dicts a policy reads: role and text
    content always; tool calls (name and parsed arguments) and tool_call_id when present."""
    out = []
    for m in messages:
        entry: dict[str, Any] = {"role": m.get("role", "?"), "content": text_of(m.get("content"))}
        calls = []
        for c in m.get("tool_calls") or []:
            f = c.get("function", c)
            arguments = f.get("arguments")
            if isinstance(arguments, str):
                with contextlib.suppress(ValueError):
                    arguments = json.loads(arguments)
            calls.append({"name": f.get("name"), "arguments": arguments})
        if calls:
            entry["tool_calls"] = calls
        if m.get("tool_call_id"):
            entry["tool_call_id"] = m["tool_call_id"]
        out.append(entry)
    return out


class Player:
    """One episode's policy: what it is asked, what it answers, and how often."""

    def __init__(self, completion: Completion) -> None:
        if isinstance(completion, Policy):
            self.act, self.wants_tools, self.script = completion.act, completion.wants_tools, None
        else:
            script = [m for m in as_messages(completion) if m.get("role") == "assistant"]
            self.act, self.wants_tools, self.script = None, False, script or [{"content": ""}]
        self.calls = 0
        self.replies: list[dict[str, Any]] = []
        self.error: str | None = None

    def next(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        if self.calls >= TURN_CAP:
            raise RuntimeError(f"rollout did not stop after {TURN_CAP} model turns")
        if self.script is not None:
            entry = self.script[min(self.calls, len(self.script) - 1)]
        else:
            plain = plain_messages(messages)
            entry = self.act(plain, [tool_schema(t) for t in tools]) if self.wants_tools else self.act(plain)
        entry = {"content": entry} if isinstance(entry, str) else dict(entry)
        self.calls += 1
        reply = {"content": entry.get("content") or ""}
        if entry.get("tool_calls"):
            reply["tool_calls"] = [{"name": c["name"], "arguments": c.get("arguments") or {}} for c in entry["tool_calls"]]
        self.replies.append(reply)
        return reply


class PolicyEndpoint:
    """A local chat-completions endpoint on 127.0.0.1 (a free port, its own thread). The
    model name of each request picks the `Player` it is answered by."""

    def __init__(self) -> None:
        self.players: dict[str, Player] = {}
        endpoint = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_POST(self) -> None:  # noqa: N802 - the http.server name
                try:
                    body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
                except ValueError:
                    return self._send(400, {"error": {"message": "falsepass policy endpoint: the request was not JSON"}})
                if not self.path.rstrip("/").endswith("/chat/completions"):
                    return self._send(404, {"error": {"message": (
                        f"falsepass policy endpoint speaks the chat-completions API only; the harness called {self.path}. "
                        "Choose a harness that uses chat completions (--env.agent.harness.id) or write an adapter")}})  # fmt: skip
                player = endpoint.players.get(str(body.get("model", "")))
                if player is None:
                    return self._send(404, {"error": {"message": f"falsepass policy endpoint: no policy named {body.get('model')!r}"}})
                try:
                    reply = player.next(body.get("messages") or [], body.get("tools") or [])
                except Exception as exc:  # noqa: BLE001 - a policy error ends the episode, visibly
                    player.error = f"{type(exc).__name__}: {exc}"
                    return self._send(400, {"error": {"message": f"falsepass policy error: {player.error}"}})
                self._answer(body, reply, player.calls)

            def _answer(self, body: dict[str, Any], reply: dict[str, Any], n: int) -> None:
                calls = [{"id": f"call_{n}_{k}", "type": "function",
                          "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}}
                         for k, c in enumerate(reply.get("tool_calls") or [])]  # fmt: skip
                message: dict[str, Any] = {"role": "assistant", "content": reply["content"]}
                if calls:
                    message["tool_calls"] = calls
                finish = "tool_calls" if calls else "stop"
                base = {"id": f"falsepass-{n}", "created": int(time.time()), "model": body.get("model")}
                usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
                if not body.get("stream"):
                    return self._send(200, {**base, "object": "chat.completion", "usage": usage,
                                            "choices": [{"index": 0, "message": message, "finish_reason": finish}]})  # fmt: skip
                if calls:
                    message["tool_calls"] = [{**c, "index": k} for k, c in enumerate(calls)]
                chunk = {**base, "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": message, "finish_reason": finish}]}
                tail = {**base, "object": "chat.completion.chunk", "choices": [], "usage": usage}
                data = f"data: {json.dumps(chunk)}\n\ndata: {json.dumps(tail)}\n\ndata: [DONE]\n\n".encode()
                self._raw(200, "text/event-stream", data)

            def _send(self, status: int, payload: dict[str, Any]) -> None:
                self._raw(status, "application/json", json.dumps(payload).encode())

            def _raw(self, status: int, ctype: str, data: bytes) -> None:
                self.send_response(status)
                self.send_header("content-type", ctype)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, name="falsepass-policy-endpoint", daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def add(self, player: Player) -> str:
        name = f"falsepass-{uuid.uuid4().hex[:12]}"
        self.players[name] = player
        return name

    def remove(self, name: str) -> None:
        self.players.pop(name, None)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def eval_config(taskset: str, base_url: str, runtime: str = "subprocess", args: list[str] | None = None) -> Any:
    """The run config `vf-eval <taskset> --client.base-url <url> --env.agent.runtime.type
    <runtime> --no-push [args]` would build, parsed by verifiers' own CLI code so that a
    taskset's own config options (`--env.taskset.<option>`) are accepted the same way."""
    from pydantic_config import cli
    from verifiers.v1.cli.resolve import narrow_config, with_positional_taskset
    from verifiers.v1.configs.cli.eval import EvalConfig

    from verifiers.v1.configs.agent import agent_config_fields

    def parse(argv: list[str]) -> Any:
        saved = sys.argv
        try:
            config_type = narrow_config(EvalConfig, argv)
            sys.argv = [saved[0] if saved else "falsepass", *argv]
            return cli(config_type)
        except SystemExit as exc:
            raise RuntimeError(f"verifiers could not build a run config for {taskset!r} with {args or []} (exit {exc.code})") from None
        finally:
            sys.argv = saved

    argv = with_positional_taskset([taskset, "-m", "falsepass", "--client.base-url", base_url, "--client.api-key-var", KEY_VAR,
                                    "--no-push", *(args or [])])  # fmt: skip
    # Every agent seat of the env (`agent` for a plain taskset; a multi-agent env names its own)
    # runs in `runtime`, unless the extra args already chose one for it.
    seats = [name for name in agent_config_fields(parse(argv).env) if f"--env.{name}.runtime.type" not in (args or [])]
    return parse(argv + [flag for name in seats for flag in (f"--env.{name}.runtime.type", runtime)])


PACKAGE_HOSTS = ("pypi.org", "files.pythonhosted.org")


def block_network(allowed: list[str]) -> None:
    """Point HTTP(S) proxies at a closed port for this process and everything it starts, so an
    environment that calls an outside service (registers an agent, posts a result) fails to
    reach it instead of acting there. 127.0.0.1, localhost, the package indexes and `allowed`
    are exempt. Best effort: a client that ignores proxy settings, or a raw socket, is not stopped."""
    closed = "http://127.0.0.1:9"
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ[name] = closed
    exempt = ",".join(["127.0.0.1", "localhost", "::1", *PACKAGE_HOSTS, *allowed])
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = exempt


def outside_hosts(module: str) -> list[str]:
    """Hosts named in URLs in the package's Python source (not localhost): services the
    environment may call when it runs."""
    import importlib.util
    import re
    from pathlib import Path

    spec = importlib.util.find_spec(module.split(".")[0])
    roots = list(spec.submodule_search_locations or []) if spec and spec.submodule_search_locations else \
        ([str(Path(spec.origin).parent)] if spec and spec.origin else [])
    hosts: set[str] = set()
    for root in roots:
        for path in Path(root).rglob("*.py"):
            try:
                text = path.read_text(errors="replace")
            except OSError:
                continue
            hosts |= set(re.findall(r"https?://([A-Za-z0-9.-]+\.[A-Za-z]{2,})", text))
    return sorted(h for h in hosts if h not in ("localhost",) and not h.endswith(("example.com", "example.org")))


class V1EpisodeScorer:
    """Plays each completion through the taskset's own episode loop (see the module docstring).
    The environment comes up once, on the first score, and is torn down by `close()`."""

    multi_turn = True
    concurrent_safe = True

    def __init__(self, taskset: str, load_args: dict[str, Any] | None = None, runtime: str = "subprocess",
                 args: list[str] | None = None, network: bool | list[str] = False) -> None:  # fmt: skip
        self.taskset, self.load_args, self.runtime, self.args = taskset, dict(load_args or {}), runtime, list(args or [])
        self.network = network  # False: outside hosts blocked; a list: those hosts allowed; True: everything
        self.endpoint: PolicyEndpoint | None = None
        self.env: Any = None
        self.config: Any = None
        self._stack: contextlib.AsyncExitStack | None = None
        self._starting: asyncio.Lock | None = None
        self.weights: dict[str, float] = {}
        self.notes: list[str] = []

    async def start(self) -> None:
        self._starting = self._starting or asyncio.Lock()
        async with self._starting:
            if self.env is not None:
                return
            from verifiers.v1.utils.loaders import load_environment

            os.environ.setdefault(KEY_VAR, "unused")
            if self.network is not True:
                block_network(list(self.network or []))
            self.endpoint = PolicyEndpoint()
            self.config = eval_config(self.taskset, self.endpoint.url, self.runtime, self.args)
            if self.load_args:  # the taskset options the audit's tasks were loaded with
                taskset = self.config.env.taskset
                self.config.env.taskset = type(taskset).model_validate({**taskset.model_dump(), **self.load_args})
            env = load_environment(self.config.env)
            runs_code = sorted({h.config.id for h in getattr(env, "_harnesses", {}).values() if getattr(h, "EXECUTES_CODE", False)})
            if runs_code and self.runtime == "subprocess":
                self.notes.append(f"the harness ({', '.join(runs_code)}) can run code, and runs it on this machine (subprocess "
                                  "runtime); the generic policies send only empty arguments. For isolation set "
                                  "`runtime = \"docker\"` in env.toml")  # fmt: skip
            stack = contextlib.AsyncExitStack()
            await stack.enter_async_context(env.serving())
            self.env, self._stack = env, stack

    async def score(self, example: Example, completion: Completion) -> Score:
        from verifiers.v1.clients import ModelContext

        try:
            await self.start()
        except Exception as exc:  # noqa: BLE001 - one line the caller can act on
            return Score(None, {}, [f"could not start the environment: {type(exc).__name__}: {exc}"[:800]])
        assert self.endpoint is not None
        player = Player(completion)
        name = self.endpoint.add(player)
        try:
            ctx = ModelContext(client=self.config.client, model=name, sampling=self.config.sampling)
            episode = await self.env.run_episode(example.raw, ctx)
        except Exception as exc:  # noqa: BLE001
            return Score(None, {}, [f"episode failed: {type(exc).__name__}: {exc}"[:800]], self._info(player, None))
        finally:
            self.endpoint.remove(name)
        errors = [f"{e.type}: {e.message}"[:800] if hasattr(e, "message") else str(e)[:800] for e in episode.errors]
        trace = episode.traces[0] if episode.traces else None
        if trace is not None:
            errors += [f"{e.type}: {e.message}"[:800] if hasattr(e, "message") else str(e)[:800] for e in trace.errors]
        if player.error and not any(player.error in e for e in errors):
            errors.append(player.error)
        info = self._info(player, trace)
        if trace is None or not episode.ok:
            return Score(None, {}, errors or ["the episode did not finish"], info)
        metrics = {k: v.score for k, v in trace.rewards.items() if v is not None and v.score is not None}
        metrics.update({k: v for k, v in trace.metrics.items() if isinstance(v, (int, float))})
        if not self.weights:
            self.weights = {k: float(v.weight) for k, v in trace.rewards.items() if v is not None}
        return Score(float(trace.reward), metrics, errors, info)

    @staticmethod
    def _info(player: Player, trace: Any) -> dict[str, Any]:
        transcript = []
        for node in getattr(trace, "nodes", None) or []:
            message = getattr(node, "message", None)
            if message is None:
                continue
            role = getattr(message, "role", None) or type(message).__name__.replace("Message", "").lower()
            text = text_of(getattr(message, "content", None))
            calls = " ".join(f"[tool call {c.name}({c.arguments})]" for c in getattr(message, "tool_calls", None) or [])
            transcript.append({"role": role, "content": " ".join(filter(None, [text, calls]))})
        tools = [getattr(t, "name", None) or (t.get("name") if isinstance(t, dict) else None) for t in getattr(trace, "tools", None) or []]
        return {"turns": player.calls, "stop": getattr(trace, "stop_condition", None), "transcript": transcript,
                "script": [{"role": "assistant", **r} for r in player.replies], "tools": [t for t in tools if t]}  # fmt: skip

    async def close(self) -> None:
        if self._stack is not None:
            with contextlib.suppress(Exception):
                await self._stack.aclose()
        if self.endpoint is not None:
            self.endpoint.close()
        self._stack = self.env = self.endpoint = None

