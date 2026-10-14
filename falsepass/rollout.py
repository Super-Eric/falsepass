"""`falsepass rollout <env>` — cross-check with a real model.

The probes say what a grader *would* pay for completions nobody has produced yet. This
asks the complementary question on a small sample: what does a model actually write, and
does the grader pay it for the answer or for something else? Each task's prompt goes to a
model behind an OpenAI-compatible endpoint (a local Ollama server by default, so nothing
is billed), and the reply is scored by the same scorer the probes use.

Two rows per task are flagged for reading, never as findings by themselves:
- paid although the reference answer does not occur in the reply;
- not paid in full although it does.
For open-ended tasks the reference never occurs verbatim, so only the comparison with
the reward of the reference itself is reported.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import urllib.request
from pathlib import Path
from typing import Any

from falsepass.model import Example, Policy
from falsepass.runner import load_adapter

ENDPOINT = "http://localhost:11434/v1"
MODEL = "gemma4:26b-a4b-it-qat"


TRUNCATED: list[bool] = []  # one entry per chat() call: did the reply hit the token cap?


def chat(messages: list[dict[str, Any]], model: str, endpoint: str, max_tokens: int, temperature: float = 0) -> str:
    """One reply, thinking off, temperature 0. The cap is sent as `max_tokens`: Ollama
    honours that name and ignores `max_completion_tokens`."""
    body = {"model": model, "messages": [{"role": m["role"], "content": m.get("content") or ""} for m in messages],
            "temperature": temperature, "seed": 0, "max_tokens": max_tokens, "reasoning_effort": "none"}  # fmt: skip
    request = urllib.request.Request(f"{endpoint}/chat/completions", json.dumps(body).encode(),
                                     {"Content-Type": "application/json", "Authorization": "Bearer local"})  # fmt: skip
    with urllib.request.urlopen(request, timeout=900) as response:
        choice = json.load(response)["choices"][0]
    TRUNCATED.append(choice.get("finish_reason") == "length")
    return choice["message"].get("content") or ""


def prompt_messages(example: Example) -> list[dict[str, Any]]:
    """The task as the environment would send it: the row's own messages when it has them."""
    raw = example.raw
    if isinstance(raw, dict) and isinstance(raw.get("prompt"), list):
        return [{"role": m["role"], "content": m.get("content") or ""} for m in raw["prompt"]]
    system = getattr(getattr(raw, "data", None), "system_prompt", None)  # v1 tasks carry it separately
    head = [{"role": "system", "content": system}] if isinstance(system, str) and system.strip() else []
    return [*head, {"role": "user", "content": example.prompt}]


def _norm(text: str) -> str:
    return re.sub(r"[\W_]+", " ", text.lower()).strip()


def mentions(reply: str, answer: str) -> bool:
    return bool(_norm(answer)) and f" {_norm(answer)} " in f" {_norm(reply)} "


def model_policy(scorer: Any, model: str, endpoint: str, max_tokens: int) -> Policy:
    """The model as a player. A legacy environment gets verifiers' own client for the
    endpoint, so the tool schemas the environment offers reach the model and its tool
    calls come back; otherwise the model sees the conversation as plain text."""
    if hasattr(scorer, "env"):
        try:
            from openai import AsyncOpenAI
            from verifiers.clients import OpenAIChatCompletionsClient

            client = OpenAIChatCompletionsClient(AsyncOpenAI(base_url=endpoint, api_key="local", timeout=900))
            sampling = {"temperature": 0, "seed": 0, "max_tokens": max_tokens, "reasoning_effort": "none"}
            return Policy("model", client=client, model=model, sampling=sampling)
        except ImportError:
            pass
    return Policy("model", lambda messages: chat(messages, model, endpoint, max_tokens))


def run(audit_dir: Path, n: int, seed: int, model: str, endpoint: str, max_tokens: int, tag: str = "") -> dict[str, Any]:
    adapter = load_adapter(audit_dir)
    scorer, examples = adapter.load(n, seed)
    loop = asyncio.new_event_loop()
    multi_turn = bool(getattr(scorer, "multi_turn", False))
    open_ended = bool(getattr(adapter, "open_ended", False))
    rows = []
    try:
        if hasattr(adapter, "prepare"):
            loop.run_until_complete(adapter.prepare(scorer, examples))
        top = adapter.max_reward
        for example in examples:
            TRUNCATED.clear()
            if multi_turn:
                completion: Any = model_policy(scorer, model, endpoint, max_tokens)
                score = loop.run_until_complete(scorer.score(example, completion))
                said = [m["content"] for m in score.info.get("transcript", []) if m["role"] == "assistant"]
                reply = "\n---\n".join(said)
            else:
                reply = chat(prompt_messages(example), model, endpoint, max_tokens)
                score = loop.run_until_complete(scorer.score(example, reply))
            reference = None
            if example.answer.strip() and not multi_turn:
                reference = loop.run_until_complete(scorer.score(example, adapter.render(example.answer, example))).reward
            reward = score.reward
            cut_off = any(TRUNCATED)
            has_answer = None if open_ended or multi_turn or not example.answer.strip() else mentions(reply, example.answer)
            flag = ""
            if reward is None:
                flag = "reward code raised"
            elif cut_off:
                flag = "reply cut off at the token cap"
            elif has_answer is False and reward > adapter.bad_tolerance * top + 1e-9:
                flag = "paid without the reference in the reply"
            elif has_answer is True and reward < top - 1e-9:
                flag = "reference in the reply, not paid in full"
            elif reference is not None and reward > reference + 1e-9:
                flag = "paid more than the reference"
            rows.append({"example": example.key, "reward": reward, "reference_reward": reference, "answer": example.answer,
                         "mentions_reference": has_answer, "flag": flag, "metrics": score.metrics,
                         "errors": score.errors, "turns": score.info.get("turns"), "truncated": cut_off, "reply": reply})  # fmt: skip
            print(f"{example.key:>10} reward {reward if reward is None else round(reward, 3)}  {flag}", file=sys.stderr)
    finally:
        loop.run_until_complete(scorer.close())
        loop.close()
    payload = {"environment": adapter.name, "model": model, "endpoint": endpoint, "max_tokens": max_tokens, "seed": seed,
               "max_reward": top, "open_ended": open_ended, "multi_turn": multi_turn,
               "format": (getattr(adapter, "inferred", None) or {}).get("format"), "rows": rows}  # fmt: skip
    stem = f"rollout-{tag}" if tag else "rollout"
    (audit_dir / f"{stem}.json").write_text(json.dumps(payload, indent=1, ensure_ascii=True))
    (audit_dir / f"{stem}.md").write_text(summary(payload))
    return payload


def _cut(text: str, width: int = 110, tail: bool = False) -> str:
    text = " ".join(str(text).split())
    if len(text) > width:
        text = "…" + text[-(width - 1):] if tail else text[: width - 1] + "…"
    return text.replace("|", "\\|")


def summary(payload: dict[str, Any]) -> str:
    rows = payload["rows"]
    scored = [r for r in rows if r["reward"] is not None]
    mean = lambda values: sum(values) / len(values) if values else float("nan")  # noqa: E731
    references = [r["reference_reward"] for r in rows if r["reference_reward"] is not None]
    lines = [
        f"# Rollout check: {payload['environment']}",
        "",
        f"Model `{payload['model']}` at `{payload['endpoint']}`, thinking off, temperature 0, seed 0, "
        f"at most {payload['max_tokens']} tokens per reply. {len(rows)} tasks. One run: model output is not "
        "guaranteed to repeat, so this is an observation, not a reproduction.",
        "",
        f"- **Mean reward of the model's replies:** {mean([r['reward'] for r in scored]):.3f}",
    ]
    cut = sum(1 for r in rows if r.get("truncated"))
    if cut:
        lines.append(f"- **Replies cut off at the token cap:** {cut} of {len(rows)} — these say nothing about the "
                     "grader; raise `--max-tokens`")
    if references:
        lines.append(f"- **Mean reward of the reference answers on the same tasks:** {mean(references):.3f}")
        above = sum(1 for r in scored if r["reference_reward"] is not None and r["reward"] > r["reference_reward"] + 1e-9)
        lines.append(f"- **Replies paid more than the reference:** {above} of {len(scored)}")
    if not payload["open_ended"] and not payload["multi_turn"]:
        known = [r for r in scored if r["mentions_reference"] is not None]
        lines.append(f"- **Replies in which the reference answer occurs:** {sum(r['mentions_reference'] for r in known)} of {len(known)}")
    for flag in ("paid without the reference in the reply", "reference in the reply, not paid in full",
                 "paid more than the reference", "reward code raised"):  # fmt: skip
        count = sum(1 for r in rows if r["flag"] == flag)
        if count:
            lines.append(f"- **{flag.capitalize()}:** {count} — read these; a flag is a reason to look, not a finding")
    names = sorted({name for r in scored for name in r["metrics"]} - {"num_turns"})
    if names:
        lines += ["", "| Reward term | Mean over the model's replies |", "|---|---|"]
        lines += [f"| {name} | {mean([r['metrics'][name] for r in scored if name in r['metrics']]):.3f} |" for name in names]
    lines += ["", "| Task | Reward | Reference | Flag | Expected answer | End of the reply |", "|---|---|---|---|---|---|"]
    for r in rows:
        reward = "raised" if r["reward"] is None else f"{r['reward']:.3f}"
        reference = "—" if r["reference_reward"] is None else f"{r['reference_reward']:.3f}"
        lines.append(f"| {r['example']} | {reward} | {reference} | {r['flag']} | {_cut(r['answer'], 40)} | {_cut(r['reply'], tail=True)} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m falsepass.rollout")
    parser.add_argument("audit_dir", type=Path)
    parser.add_argument("-n", type=int, default=20, help="tasks to sample")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--endpoint", default=ENDPOINT, help="OpenAI-compatible base URL (default: local Ollama)")
    parser.add_argument("--max-tokens", type=int, default=600)
    parser.add_argument("--tag", default="", help="write rollout-<tag>.json/.md, to keep runs with several models apart")
    parser.add_argument("--package", action="append", default=[], help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    print(summary(run(args.audit_dir, args.n, args.seed, args.model, args.endpoint, args.max_tokens, args.tag)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
