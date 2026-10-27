"""Ranking and CSV export utilities for aggregated campaign metrics."""

from __future__ import annotations

from pathlib import Path

from src.models import CampaignStats

CSV_HEADER: str = (
    "campaign_id,total_impressions,total_clicks,total_spend,total_conversions,CTR,CPA"
)


def get_top10_ctr(
    campaigns: dict[str, CampaignStats],
    limit: int = 10,
) -> list[tuple[str, CampaignStats]]:
    """Return top `limit` campaigns with highest CTR (requiring impressions > 0).

    Deterministic tie-breaker order:
      1. Highest CTR (-stats.ctr)
      2. Highest total_clicks (-stats.clicks)
      3. Highest total_impressions (-stats.impressions)
      4. Lexicographical campaign_id (campaign_id)
    """
    if limit <= 0:
        return []

    eligible = [
        (cid, stats) for cid, stats in campaigns.items() if stats.impressions > 0
    ]
    eligible.sort(
        key=lambda item: (
            -item[1].ctr,
            -item[1].clicks,
            -item[1].impressions,
            item[0],
        )
    )
    return eligible[:limit]


def get_top10_cpa(
    campaigns: dict[str, CampaignStats],
    limit: int = 10,
) -> list[tuple[str, CampaignStats]]:
    """Return top `limit` campaigns with lowest CPA, strictly excluding 0 conversions.

    Deterministic tie-breaker order:
      1. Lowest CPA (stats.cpa)
      2. Highest total_conversions (-stats.conversions)
      3. Lowest spend_scaled (stats.spend_scaled)
      4. Lexicographical campaign_id (campaign_id)
    """
    if limit <= 0:
        return []

    eligible = [
        (cid, stats) for cid, stats in campaigns.items() if stats.conversions > 0
    ]
    eligible.sort(
        key=lambda item: (
            item[1].cpa if item[1].cpa is not None else float("inf"),
            -item[1].conversions,
            item[1].spend_scaled,
            item[0],
        )
    )
    return eligible[:limit]


def format_csv_row(campaign_id: str, stats: CampaignStats) -> str:
    """Format a single campaign record as a CSV line matching the specification."""
    cpa_val = stats.cpa
    cpa_str = f"{cpa_val:.2f}" if cpa_val is not None else "null"
    return (
        f"{campaign_id},"
        f"{stats.impressions},"
        f"{stats.clicks},"
        f"{stats.total_spend:.2f},"
        f"{stats.conversions},"
        f"{stats.ctr:.4f},"
        f"{cpa_str}"
    )


def _write_csv_file(
    file_path: Path,
    rows: list[tuple[str, CampaignStats]],
) -> None:
    """Write header and formatted campaign rows to a CSV file with LF newlines."""
    lines = [CSV_HEADER]
    for cid, stats in rows:
        lines.append(format_csv_row(cid, stats))
    with file_path.open("w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")


def write_results(
    output_dir: str | Path,
    campaigns: dict[str, CampaignStats],
    limit: int = 10,
) -> tuple[Path, Path]:
    """Generate `top10_ctr.csv` and `top10_cpa.csv` inside `output_dir`.

    Returns:
        (ctr_csv_path, cpa_csv_path)
    """
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    ctr_path = out_path / "top10_ctr.csv"
    cpa_path = out_path / "top10_cpa.csv"

    top_ctr = get_top10_ctr(campaigns, limit=limit)
    top_cpa = get_top10_cpa(campaigns, limit=limit)

    _write_csv_file(ctr_path, top_ctr)
    _write_csv_file(cpa_path, top_cpa)

    return ctr_path, cpa_path
