"""The FastAPI app of the toy refund desk: an MCP tool environment, served the usual way."""

from openenv.core.env_server.http_server import create_app
from openenv.core.env_server.mcp_types import CallToolAction, CallToolObservation

try:
    from .refund_environment import RefundEnvironment
except ImportError:  # run from inside the folder, as in the Docker image
    from server.refund_environment import RefundEnvironment  # type: ignore[no-redef]

app = create_app(RefundEnvironment, CallToolAction, CallToolObservation, env_name="refund_desk")


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
