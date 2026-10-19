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
