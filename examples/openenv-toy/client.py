"""Client for the toy arithmetic environment (the usual OpenEnv client shape)."""

from openenv.core import EnvClient
from openenv.core.client_types import StepResult
from openenv.core.env_server.types import State

from .models import ArithAction, ArithObservation


class ArithEnv(EnvClient[ArithAction, ArithObservation, State]):
    def _step_payload(self, action: ArithAction) -> dict:
        return {"answer": action.answer}

    def _parse_result(self, payload: dict) -> StepResult[ArithObservation]:
        data = payload.get("observation", {})
        observation = ArithObservation(
            question=data.get("question", ""),
            instructions=data.get("instructions", ""),
            feedback=data.get("feedback", ""),
            done=payload.get("done", False),
            reward=payload.get("reward"),
        )
        return StepResult(observation=observation, reward=payload.get("reward"), done=payload.get("done", False))

    def _parse_state(self, payload: dict) -> State:
        return State(episode_id=payload.get("episode_id"), step_count=payload.get("step_count", 0))
