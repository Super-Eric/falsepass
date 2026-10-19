import verifiers.v1 as vf

from falsepass_v1_tool_toy import desk
from falsepass_v1_tool_toy.desk import DeskState


class DeskToolset(vf.Toolset[vf.SharedToolsetConfig, DeskState]):
    TOOL_PREFIX = None

    @vf.tool
    def lookup_order(self, order_id: str) -> str:
        """Show an order's amount and age."""
        return desk.lookup_order(self.state, order_id)

    @vf.tool
    def issue_refund(self, order_id: str, amount: float) -> str:
        """Refund `amount` EUR on an order. Takes effect immediately."""
        return desk.issue_refund(self.state, order_id, amount)


if __name__ == "__main__":
    DeskToolset.run()
