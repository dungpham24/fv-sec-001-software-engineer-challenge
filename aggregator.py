#!/usr/bin/env python3
"""CLI entrypoint for the FV-SEC001 Ad Performance Aggregator."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import time
from typing import Optional, Sequence

from src.exporter import write_results
from src.processor import aggregate_file, resolve_engine


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="aggregator.py",
        description=(
            "High-throughput parallel CSV aggregator for advertising performance data "
            "(FV-SEC001 Challenge)."
        ),
    )
    parser.add_argument(
        "--input",
        "-i",
        required=True,
        help="Path to the input ad_data.csv file.",
    )
    parser.add_argument(
        "--output",
        "-o",
        required=True,
        help="Directory where top10_ctr.csv and top10_cpa.csv will be written.",
    )
    parser.add_argument(
        "--workers",
        "-w",
        type=int,
        default=0,
        help="Number of worker threads/processes (default: 0 = auto-detect CPU core count).",
    )
    parser.add_argument(
        "--chunk-size-mb",
        type=float,
        default=None,
        help="Optional byte chunk size in MiB per task (default: dynamic zero-straggler sizing).",
    )
    parser.add_argument(
        "--engine",
        "-e",
        choices=["auto", "c", "python"],
        default="auto",
        help="Execution engine: 'auto' (native C if available, else Python), 'c', or 'python' (default: auto).",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the CLI pipeline and return an exit status code."""
    args = parse_args(argv)

    input_path = Path(args.input)
    output_dir = Path(args.output)

    if not input_path.exists() or not input_path.is_file():
        print(f"Error: Input file does not exist: {input_path}", file=sys.stderr)
        return 1

    chunk_size_bytes: Optional[int] = None
    if args.chunk_size_mb is not None:
        chunk_size_bytes = int(args.chunk_size_mb * 1024 * 1024)
        if chunk_size_bytes <= 0:
            print(
                f"Error: --chunk-size-mb must be greater than 0 (got {args.chunk_size_mb})",
                file=sys.stderr,
            )
            return 1

    start_time = time.perf_counter()
    try:
        active_engine = resolve_engine(args.engine)
        result = aggregate_file(
            file_path=input_path,
            workers=args.workers if args.workers > 0 else None,
            chunk_size=chunk_size_bytes,
            engine=active_engine,
        )
        ctr_path, cpa_path = write_results(
            output_dir=output_dir,
            campaigns=result.campaigns,
        )
    except Exception as exc:  # pragma: no cover - defensive top-level CLI handler
        print(f"Error processing file: {exc}", file=sys.stderr)
        return 1

    elapsed = time.perf_counter() - start_time

    print(
        f"Aggregation complete in {elapsed:.3f}s [engine={active_engine}] | "
        f"Campaigns: {len(result.campaigns):,} | "
        f"Valid rows: {result.valid_rows:,} | "
        f"Malformed rows skipped: {result.malformed_rows:,}"
    )
    print(f"Outputs written to: {ctr_path} and {cpa_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
