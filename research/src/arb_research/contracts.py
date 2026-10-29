"""Wire contract shared by the API and isolated engine process."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


def now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Parameters(StrictModel):
    fast_window: Annotated[int, Field(strict=True, ge=1, le=1000)]
    slow_window: Annotated[int, Field(strict=True, ge=2, le=1000)]

    @model_validator(mode="after")
    def ordered(self):
        if self.fast_window >= self.slow_window:
            raise ValueError("fast_window must be less than slow_window")
        return self


class Strategy(StrictModel):
    id: Literal["sma_cross"]
    parameters: Parameters


class Execution(StrictModel):
    fee_bps: Annotated[float, Field(strict=True, ge=0, le=10000)]
    slippage_bps: Annotated[float, Field(strict=True, ge=0, lt=10000)]
    fill_policy: Literal["next_bar_open"]


class CreateBacktest(StrictModel):
    dataset_id: Annotated[str, Field(min_length=1, max_length=128)]
    engine: Literal["fast", "realistic"]
    strategy: Strategy
    execution: Execution
    start: AwareDatetime
    end: AwareDatetime
    initial_capital: Annotated[float, Field(strict=True, ge=0.01, le=1e12)]
    currency: Literal["USD"]

    @model_validator(mode="after")
    def interval(self):
        if Decimal(str(self.initial_capital)) % Decimal("0.01") != 0:
            raise ValueError("initial_capital must be a whole number of USD cents")
        self.start = self.start.astimezone(UTC)
        self.end = self.end.astimezone(UTC)
        if self.start >= self.end:
            raise ValueError("start must precede end")
        return self
