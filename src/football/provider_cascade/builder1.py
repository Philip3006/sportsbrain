"""Narrow, model-neutral seam from the cascade into Builder 1 shadow code."""

from __future__ import annotations

from dataclasses import dataclass

from src.football.provider_cascade.contracts import (
    CascadeDecisionTrace,
    CascadeResult,
    NormalizedOddsObservation,
)


@dataclass(frozen=True)
class Builder1OddsInput:
    """Only accepted normalized odds plus routing provenance crosses this seam."""

    observation: NormalizedOddsObservation
    routing: CascadeDecisionTrace

    def validate(self) -> None:
        self.observation.validate()
        self.routing.validate()
        if self.routing.selected_provider != self.observation.provider_identity:
            raise ValueError("Builder 1 input provider differs from cascade selection")
        if (
            self.routing.fail_closed
            or not self.routing.no_bet
            or self.routing.publication
        ):
            raise ValueError("Builder 1 input violates the shadow safety boundary")

    def model_inputs(self):
        """Return the existing contract objects; no model is imported here."""

        self.validate()
        return self.observation.fixture, self.observation.as_market_snapshot()


def accepted_for_builder1(result: CascadeResult) -> Builder1OddsInput:
    """Convert a routed result or fail closed before any M5 invocation."""

    result.validate()
    if not result.accepted or result.observation is None:
        raise ValueError("cascade failed closed; Builder 1 must not receive odds")
    input_value = Builder1OddsInput(result.observation, result.trace)
    input_value.validate()
    return input_value
