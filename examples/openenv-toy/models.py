"""Action and observation of the toy arithmetic environment."""

from openenv.core.env_server.types import Action, Observation
from pydantic import Field


class ArithAction(Action):
    answer: str = Field(..., description="The answer to the question, as text")


class ArithObservation(Observation):
    question: str = Field(default="", description="The question to answer")
    instructions: str = Field(default="", description="How to answer")
    feedback: str = Field(default="", description="After a step: how the answer was graded")
