"""Data models for the Ad Performance Aggregator."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

SPEND_SCALE: int = 10_000


@dataclass(slots=True)
class CampaignStats:
    """Aggregated performance metrics for a single ad campaign.

    Monetary values (`spend_scaled`) are stored as integer ten-thousandths of a dollar
    (`SPEND_SCALE = 10_000`) to prevent IEEE-754 floating-point drift across millions
    of additions.
    """

    impressions: int = 0
    clicks: int = 0
    spend_scaled: int = 0
    conversions: int = 0

    def merge(self, other: CampaignStats) -> None:
        """Merge another CampaignStats instance into this one in-place."""
        self.impressions += other.impressions
        self.clicks += other.clicks
        self.spend_scaled += other.spend_scaled
        self.conversions += other.conversions

    @property
    def total_spend(self) -> float:
        """Return total spend in dollars as a float."""
        return self.spend_scaled / SPEND_SCALE

    @property
    def ctr(self) -> float:
        """Return Click-Through Rate (total_clicks / total_impressions), or 0.0 if 0 impressions."""
        return self.clicks / self.impressions if self.impressions > 0 else 0.0

    @property
    def cpa(self) -> Optional[float]:
        """Return Cost Per Acquisition (total_spend / total_conversions), or None if 0 conversions."""
        return self.total_spend / self.conversions if self.conversions > 0 else None


@dataclass(slots=True)
class AggregationResult:
    """Container for full file aggregation output and row-level diagnostics."""

    campaigns: dict[str, CampaignStats] = field(default_factory=dict)
    valid_rows: int = 0
    malformed_rows: int = 0
