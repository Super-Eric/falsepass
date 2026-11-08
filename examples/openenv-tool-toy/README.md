# openenv-tool-toy

A minimal [OpenEnv](https://github.com/huggingface/OpenEnv) tool environment (MCP) with one
planted grading defect: a refund desk. Each task is a customer ticket naming one order; the
agent looks the order up (`get_order`), refunds it (`issue_refund`) and closes the ticket
(`finish`). `list_orders` lists every order on the account. Six tickets, one split.

`finish` pays 1.0 when the ticket's order was refunded **at least** its price
(`refunds[order] >= price`) and never looks at refunds made to other orders, so a policy that
never reads the ticket (list every order, refund each one a huge amount, finish) is paid in
full. The fix: the ticket's order refunded exactly its price, and nothing else refunded.

There is no reply to grade, so it is played with policies:

```bash
falsepass init examples/openenv-tool-toy --policies
# fill in solver() and exploits() in audits/refund-desk/adapter.py, then
falsepass audit refund-desk
falsepass repro refund-desk --run
```

A solver reads the order id from the ticket, calls `get_order`, refunds the price and calls
`finish`; an exploit lists every order and refunds each one `1e9`. The solver's rows (X0)
score 1.0 and the exploit's (X1) 1.0 as well; the generic tool policies (T5, T6) score 0.
