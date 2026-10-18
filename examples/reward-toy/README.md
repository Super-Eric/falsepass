# reward-toy

Two reward functions written for TRL's `GRPOTrainer` (`rewards.py`) and the dataset they are
trained with (`data.jsonl`, conversational prompts with an `answer` column), with one planted
defect: `correctness_reward` pays when the expected number appears anywhere in the
`<answer>` block (`expected in given`), so an answer that lists several numbers, or names the
right one and then rejects it, is paid in full.

```bash
falsepass init examples/reward-toy/rewards.py --dataset examples/reward-toy/data.jsonl \
    --func correctness_reward --func format_reward --weight 2 --weight 0.5
falsepass audit rewards
```

The functions are called the way the trainer calls them (keyword arguments, one list entry
per completion, every dataset column passed along) and their weighted sum is the reward.
The answer format is inferred from the prompt (`<think>` then `<answer>`), the static scan
points at the line, and the probes that list several answers (P5) or deny the answer (P9,
P15) are paid.
