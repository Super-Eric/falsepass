"""falsepass example: two reward functions written for TRL's GRPOTrainer, one planted defect.

    trainer = GRPOTrainer(model, reward_funcs=[correctness_reward, format_reward],
                          args=GRPOConfig(reward_weights=[2.0, 0.5]), train_dataset=dataset)

The prompts in data.jsonl are conversations, so the trainer passes each completion as a
one-message list. The planted defect is in `correctness_reward`: the expected number is
looked for anywhere in the <answer> block, so "41 or 42" counts as 42.
"""

import re

ANSWER = re.compile(r"<answer>(.*?)</answer>", re.DOTALL)
FORMAT = re.compile(r"^<think>.*?</think>\s*<answer>.*?</answer>\s*$", re.DOTALL)


def correctness_reward(prompts, completions, answer, **kwargs):
    rewards = []
    for completion, expected in zip(completions, answer):
        found = ANSWER.search(completion[0]["content"])
        given = found.group(1).strip() if found else ""
        rewards.append(1.0 if expected in given else 0.0)  # the planted defect: should be given == expected
    return rewards


def format_reward(completions, **kwargs):
    return [1.0 if FORMAT.match(c[0]["content"]) else 0.0 for c in completions]
