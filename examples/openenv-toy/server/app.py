"""The FastAPI app of the toy environment, built the way `openenv init` builds one."""

from openenv.core.env_server.http_server import create_app

try:
    from ..models import ArithAction, ArithObservation
    from .arith_environment import ArithEnvironment
except ImportError:  # run from inside the folder, as in the Docker image
    from models import ArithAction, ArithObservation  # type: ignore[no-redef]
    from server.arith_environment import ArithEnvironment  # type: ignore[no-redef]

app = create_app(ArithEnvironment, ArithAction, ArithObservation, env_name="arith_env")


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
