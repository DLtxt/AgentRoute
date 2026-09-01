"""Request and response schemas for the gateway."""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.tiers.base import TierName


class QueryRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=100_000)
    # Capabilities the caller needs. Checked against the selected tier's
    # manifest before dispatch; an empty list is the plain-generation case.
    capabilities: list[str] = Field(default_factory=lambda: ["text_generation"])


class QueryResponse(BaseModel):
    request_id: str
    response: str
    tier: TierName
    cached: bool
    reason: str
    latency_ms: float
    input_tokens: int
    output_tokens: int
    cost_usd: float
