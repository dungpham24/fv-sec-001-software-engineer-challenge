"""FV-SEC001 Parallel Ad Performance Aggregator package."""

from src.c_accel import aggregate_file_c, is_c_engine_available
from src.exporter import (
    CSV_HEADER,
    format_csv_row,
    get_top10_cpa,
    get_top10_ctr,
    write_results,
)
from src.models import AggregationResult, CampaignStats, SPEND_SCALE
from src.processor import (
    DEFAULT_CHUNK_SIZE,
    aggregate_file,
    calculate_byte_chunks,
    process_chunk,
    resolve_engine,
)

__all__ = [
    "SPEND_SCALE",
    "CampaignStats",
    "AggregationResult",
    "DEFAULT_CHUNK_SIZE",
    "calculate_byte_chunks",
    "process_chunk",
    "aggregate_file",
    "resolve_engine",
    "is_c_engine_available",
    "aggregate_file_c",
    "CSV_HEADER",
    "get_top10_ctr",
    "get_top10_cpa",
    "format_csv_row",
    "write_results",
]
