"""The refund desk: orders, and the two things an agent can do with them."""

import verifiers.v1 as vf
from pydantic import BaseModel, Field


class Order(BaseModel):
    id: str
    amount: float
    days_ago: int


class DeskState(vf.State):  # the episode's state: the tool server sees it too
    orders: list[Order] = Field(default_factory=list)
    refunds: dict[str, float] = Field(default_factory=dict)


def lookup_order(s: DeskState, order_id: str) -> str:
    for o in s.orders:
        if o.id == order_id:
            return f"Order {o.id}: {o.amount:.2f} EUR, placed {o.days_ago} days ago."
    return f"No order {order_id}."


def issue_refund(s: DeskState, order_id: str, amount: float) -> str:
    if not any(o.id == order_id for o in s.orders):
        return f"No order {order_id}."
    s.refunds[order_id] = s.refunds.get(order_id, 0.0) + float(amount)
    return f"Refunded {float(amount):.2f} EUR on order {order_id}."
