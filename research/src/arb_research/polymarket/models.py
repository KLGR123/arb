"""Stable local contracts; upstream SDK models are converted at the boundary."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Tag(Model):
    id: str
    label: str | None = None
    slug: str | None = None


class Token(Model):
    id: str
    outcome: str
    outcome_index: int
    protocol: Literal["v1", "v2", "unknown"]


class SelectedAsset(Model):
    protocol_version: Literal["v1", "v2"]
    asset_kind: Literal["ctf_token", "position"]
    asset_id: Annotated[str, Field(strict=True, pattern=r"^[0-9]+$")]
    ledger: Literal["ConditionalTokens", "PositionManager"]
    chain_id: str | None = None
    event_id: str
    market_id: str
    condition_id: str
    raw_condition_id: str
    outcome: str
    outcome_index: int = Field(ge=0)
    module: str | None = None
    source: str = "gamma"
    api_version: str | None = None
    schema_version: Literal["selected-assets.v1"] = "selected-assets.v1"
    sdk_version: str
    observed_at: str | None = None
    received_at: str | None = None


class AssetSelection(Model):
    schema_version: Literal["selected-assets.v1"] = "selected-assets.v1"
    protocol_version: Literal["v1", "v2", "unknown"] = "unknown"
    status: Literal["selected", "quarantined"] = "quarantined"
    reason: str | None = "identity_view_missing_resync_required"
    assets: list[SelectedAsset] = Field(default_factory=list)
    raw_identity: dict = Field(default_factory=dict)


class Market(Model):
    id: str
    event_id: str
    condition_id: str
    slug: str | None = None
    question: str | None = None
    rules: str | None = None
    resolution_source: str | None = None
    closed: bool | None = None
    protocol: Literal["v1", "v2", "unknown"]
    neg_risk: bool | None = None
    neg_risk_group: str | None = None
    tags: list[Tag] = Field(default_factory=list)
    tokens: list[Token]
    asset_selection: AssetSelection = Field(default_factory=AssetSelection)


class Event(Model):
    id: str
    slug: str | None = None
    title: str | None = None
    description: str | None = None
    closed: bool | None = None
    tags: list[Tag] = Field(default_factory=list)
    markets: list[Market]


class PricePoint(Model):
    timestamp: int = Field(gt=0)
    price: float = Field(ge=0, le=1)
    resolution_seconds: int = Field(ge=0)


class PriceRow(PricePoint):
    event_id: str
    market_id: str
    token_id: str


class Resolution(Model):
    market_id: str
    condition_id: str
    status: str
    # SDK normalizes micro-pUSD payouts to pUSD/share, including split settlement.
    payouts: list[float] | None = None
    split_50_50: bool = False
    was_disputed: bool
    extended_review: bool
    proposed_price: str | None = None
    reproposed_price: str | None = None
    price: str | None = None
    resolved_at: str | None = None
    last_updated_at: str
    raw: dict


class PricePage(Model):
    data: list[PricePoint]
    next_cursor: str | None = None


class History(Model):
    data: list[PriceRow]
    next_offset: int | None = None


class ResolutionResult(Model):
    data: list[Resolution]
    missing_market_ids: list[str]


class SyncResult(Model):
    compliance: dict
    event_id: str
    markets: int
    tokens: int
    points_received: int
    resolutions: int
    missing_resolution_market_ids: list[str]
    empty_token_ids: list[str]
    quarantined_markets: list[dict] = Field(default_factory=list)
