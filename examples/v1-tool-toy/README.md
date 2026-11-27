# v1-tool-toy

A tiny `verifiers.v1` tool taskset for trying falsepass: a refund desk with two tools
(`lookup_order`, `issue_refund`), ten tasks, and one planted defect. The reward checks that
no order was refunded wrongly, but never that the refund owed was made, so an agent that
does nothing earns full reward on every task.

    falsepass init ./examples/v1-tool-toy
    falsepass audit falsepass-v1-tool-toy

falsepass plays each task as a whole episode through the taskset's own loop (its tool
server, its stop conditions, its reward), with generic policies that never read the task.
Expected: a false-pass ceiling of 1 via `says_done_without_any_tool_call`.

The same package holds a second taskset, `falsepass_v1_file_toy`: reverse a word and write it
to `answer.txt`, under verifiers' default `bash` harness. Its planted defect: the reward grades
the final reply and never reads the file. Probe T9 writes the reference to `answer.txt` in a
whole episode and replies that it did; expected: `file_then_note` scores 0 while
`file_then_reply` (the same file, plus the reference as the reply) scores 1. The fix,
`reads_the_file`, plugs in through env.toml:

    vf_args = ["--env.taskset.task.rewards", "{\"correct\": {\"fn\": \"falsepass_v1_file_toy:reads_the_file\"}}"]
