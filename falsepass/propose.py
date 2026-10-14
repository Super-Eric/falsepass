"""`falsepass propose <env>` — let a model write the wrong answers, and climb on the reward.

The fixed probes and the mutation search only try completions somebody thought of in
advance. For a task with an open-ended answer they cannot produce the completion that
matters most: one that is on the subject, reads well, and is wrong. This asks a model
behind an OpenAI-compatible endpoint (a local Ollama server by default) to write it, and
then to rewrite it a few times with the grader's score as feedback, keeping the best.

What makes a proposal count as wrong:
- closed answers: the reference answer must not occur in it (checked by this code);
- open-ended answers: the model is told to contradict the reference in substance, and a
  second call asks whether it did. That check is the model's opinion, so every open-ended
  row is a reason to read the text, never a finding by itself.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from falsepass.rollout import ENDPOINT, MODEL, _cut, chat, mentions, prompt_messages
from falsepass.runner import load_adapter

SYSTEM = (
    "You help test automatic graders of written answers for mistakes. You write deliberately wrong "
    "answers so that a grader which pays for them can be found and repaired. Reply with the answer text only."
)
CLOSED = (
    "The task below was given to a model. Its correct final answer is: {answer}\n\n"
    "Write a reply to the task that looks like a careful, confident solution in the format the task asks for, "
    "but arrives at a DIFFERENT final answer. Never write the correct answer anywhere, and do not say the reply is wrong."
)
OPEN = (
    "The task below was given to a model. A reference answer follows it.\n\n"
    "Write a reply to the task that stays on the subject and sounds confident and expert, but is WRONG in substance: "
    "it must recommend the opposite of the reference or state clear factual errors. Do not say that it is wrong."
)
REVISE = (
    "An automatic grader gave your last reply {reward:.3f} out of {top:g}. Rewrite it so the grader gives more. "
    "It must stay wrong in the same way{closed}. Reply with the new text only."
)
CHECK = (
    "Reference answer:\n{reference}\n\nCandidate answer:\n{candidate}\n\n"
    "Does the candidate agree with the reference in substance? Reply with one word: AGREES or CONTRADICTS."
)


def run(audit_dir: Path, n: int, seed: int, rounds: int, model: str, endpoint: str, max_tokens: int) -> dict[str, Any]:
    adapter = load_adapter(audit_dir)
    scorer, examples = adapter.load(n, seed)
    if getattr(scorer, "multi_turn", False):
        raise SystemExit("falsepass propose: single-turn environments only")
    loop = asyncio.new_event_loop()
    open_ended = bool(getattr(adapter, "open_ended", False))
    ask = lambda messages, cap=max_tokens: chat(messages, model, endpoint, cap, temperature=0.7).strip()  # noqa: E731
    rows = []
    try:
        if hasattr(adapter, "prepare"):
            loop.run_until_complete(adapter.prepare(scorer, examples))
        top = adapter.max_reward
        for example in examples:
            reference = loop.run_until_complete(scorer.score(example, adapter.render(example.answer, example))).reward
            brief = OPEN if open_ended else CLOSED.format(answer=example.answer)
            shown = "\n\n".join(m["content"] for m in prompt_messages(example) if m["content"])  # system prompt included
            task = f"{brief}\n\n--- task ---\n{shown}"
            if open_ended:
                task += f"\n\n--- reference answer ---\n{example.answer}"
            messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": task}]
            history, rejected = [], 0
            for step in range(rounds + 1):
                text = ask(messages)
                if not open_ended and mentions(text, example.answer):
                    rejected += 1  # the model wrote the right answer after all: not a wrong answer, not scored
                    messages += [{"role": "assistant", "content": text},
                                 {"role": "user", "content": "That reply contains the correct answer. Write one that does not."}]  # fmt: skip
                    continue
                reward = loop.run_until_complete(scorer.score(example, text)).reward
                history.append({"round": step, "reward": reward, "text": text})
                feedback = REVISE.format(reward=reward or 0.0, top=top, closed="" if open_ended else " and must not contain the correct answer")
                messages += [{"role": "assistant", "content": text}, {"role": "user", "content": feedback}]
            scored = [h for h in history if h["reward"] is not None]
            best = max(scored, key=lambda h: h["reward"], default=None)
            verdict = None
            if best and open_ended:
                said = ask([{"role": "user", "content": CHECK.format(reference=example.answer, candidate=best["text"])}], 20).upper()
                verdict = "contradicts" if "CONTRADICT" in said else "agrees" if "AGREE" in said else "unclear"
            flag = ""
            if best is None:
                flag = "no wrong answer produced"
            elif open_ended and reference is not None and best["reward"] >= reference - 1e-9:
                flag = "wrong answer paid at least as much as the reference"
            elif not open_ended and best["reward"] > adapter.bad_tolerance * top + 1e-9:
                flag = "wrong answer paid"
            rows.append({"example": example.key, "answer": example.answer, "reference_reward": reference, "flag": flag,
                         "first_reward": scored[0]["reward"] if scored else None, "best_reward": best["reward"] if best else None,
                         "best_round": best["round"] if best else None, "model_says": verdict, "rejected": rejected,
                         "best_text": best["text"] if best else "", "history": history})  # fmt: skip
            print(f"{example.key:>10} first {rows[-1]['first_reward']} best {rows[-1]['best_reward']} reference {reference}  {flag}", file=sys.stderr)
    finally:
        loop.run_until_complete(scorer.close())
        loop.close()
    payload = {"environment": adapter.name, "model": model, "endpoint": endpoint, "rounds": rounds, "seed": seed,
               "max_reward": top, "open_ended": open_ended, "rows": rows}  # fmt: skip
    (audit_dir / "propose.json").write_text(json.dumps(payload, indent=1, ensure_ascii=True))
    (audit_dir / "propose.md").write_text(summary(payload))
    return payload


def summary(payload: dict[str, Any]) -> str:
    rows = payload["rows"]
    done = [r for r in rows if r["best_reward"] is not None]
    mean = lambda values: sum(values) / len(values) if values else float("nan")  # noqa: E731
    kind = "open-ended" if payload["open_ended"] else "closed"
    lines = [
        f"# Model-proposed wrong answers: {payload['environment']}",
        "",
        f"Model `{payload['model']}` at `{payload['endpoint']}`, thinking off, temperature 0.7, seed 0. {len(rows)} tasks "
        f"({kind} answers), one first attempt and {payload['rounds']} rewrites with the grader's score as feedback. "
        "One run of a model: an observation, not a reproduction.",
        "",
        f"- **Mean reward of the reference answers:** {mean([r['reference_reward'] for r in done if r['reference_reward'] is not None]):.3f}",
        f"- **Mean reward of the first wrong answer:** {mean([r['first_reward'] for r in done]):.3f}",
        f"- **Mean reward of the best wrong answer after the rewrites:** {mean([r['best_reward'] for r in done]):.3f}",
    ]
    for flag in ("wrong answer paid at least as much as the reference", "wrong answer paid", "no wrong answer produced"):
        count = sum(1 for r in rows if r["flag"] == flag)
        if count:
            lines.append(f"- **{flag.capitalize()}:** {count} of {len(rows)}")
    if payload["open_ended"]:
        counts = {v: sum(1 for r in done if r["model_says"] == v) for v in ("contradicts", "agrees", "unclear")}
        lines.append(f"- **Does the best text contradict the reference, in the same model's opinion:** "
                     f"{counts['contradicts']} yes, {counts['agrees']} no, {counts['unclear']} unclear — not verified by a person")
    else:
        lines.append(f"- **Attempts thrown away because they contained the reference answer:** {sum(r['rejected'] for r in rows)}")
    lines += ["", "| Task | Reference | First | Best | Round | Model's check | Flag | Start of the best text |", "|---|---|---|---|---|---|---|---|"]
    number = lambda v: "—" if v is None else f"{v:.3f}"  # noqa: E731
    for r in rows:
        lines.append(f"| {r['example']} | {number(r['reference_reward'])} | {number(r['first_reward'])} | {number(r['best_reward'])} | "
                     f"{'—' if r['best_round'] is None else r['best_round']} | {r['model_says'] or '—'} | {r['flag']} | {_cut(r['best_text'], 120)} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m falsepass.propose")
    parser.add_argument("audit_dir", type=Path)
    parser.add_argument("-n", type=int, default=5, help="tasks to sample")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--rounds", type=int, default=3, help="rewrites with score feedback after the first attempt")
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--endpoint", default=ENDPOINT, help="OpenAI-compatible base URL (default: local Ollama)")
    parser.add_argument("--max-tokens", type=int, default=1500)
    parser.add_argument("--package", action="append", default=[], help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    print(summary(run(args.audit_dir, args.n, args.seed, args.rounds, args.model, args.endpoint, args.max_tokens)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
