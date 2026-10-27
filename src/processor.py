"""Parallel byte-range chunking and streaming CSV aggregation engine (Dual-Engine: C & Pure Python)."""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
import gc
import multiprocessing as mp
import os
from pathlib import Path
import sys
from typing import Optional

from src.c_accel import aggregate_file_c, is_c_engine_available
from src.models import AggregationResult, CampaignStats, SPEND_SCALE

DEFAULT_CHUNK_SIZE: int = 32 * 1024 * 1024  # 32 MiB
_BLOCK_READ_SIZE: int = 8 * 1024 * 1024  # 8 MiB per block read in Pure Python worker
_UTF8_BOM: bytes = b"\xef\xbb\xbf"
_MOJIBAKE_BOM: bytes = b"\xc3\xaf\xc2\xbb\xc2\xbf"


def _strip_bom(data: bytes) -> bytes:
    """Strip leading UTF-8 BOM (raw 3-byte or double-encoded 6-byte) and surrounding whitespace."""
    s = data.strip()
    if s.startswith(_UTF8_BOM):
        s = s[3:].strip()
    elif s.startswith(_MOJIBAKE_BOM):
        s = s[6:].strip()
    return s


def calculate_byte_chunks(
    file_path: str | Path,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> list[tuple[int, int]]:
    """Divide a CSV file into newline-aligned byte ranges [(start_byte, end_byte), ...].

    Opens the file in binary mode ("rb") to avoid CRLF -> LF byte offset translation drift.
    Each chunk boundary is aligned immediately after a '\\n' byte (or EOF).
    """
    if chunk_size <= 0:
        raise ValueError(f"chunk_size must be a positive integer, got {chunk_size}")

    path = Path(file_path)
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"Input file not found: {path}")

    file_size = path.stat().st_size
    if file_size == 0:
        return []

    chunks: list[tuple[int, int]] = []
    start = 0

    with path.open("rb") as f:
        while start < file_size:
            target = min(start + chunk_size, file_size)
            f.seek(target)
            if target < file_size:
                f.readline()
                end = f.tell()
            else:
                end = file_size

            if start < end:
                chunks.append((start, end))
            start = end

    return chunks


def process_chunk(
    file_path: str | Path,
    start_byte: int,
    end_byte: int,
) -> tuple[dict[str, tuple[int, int, int, int]], int, int]:
    """Process a single byte range [start_byte, end_byte) using Ultra Pure Python (Option 1).

    Uses 8 MiB block reads + C-level `bytes.splitlines()` + EAFP dictionary subscripting
    with deferred key decoding (~50 unique campaign keys per chunk).

    Returns:
        (campaigns_dict, valid_rows, malformed_rows)
        where campaigns_dict maps campaign_id (str) -> (impressions, clicks, spend_scaled, conversions).
    """
    local_stats: dict[bytes, list[int]] = {}
    valid_rows = 0
    malformed_rows = 0

    int_fn = int
    float_fn = float
    round_fn = round
    spend_scale = float(SPEND_SCALE)
    block_size = _BLOCK_READ_SIZE

    gc_was_enabled = gc.isenabled()
    gc.disable()
    try:
        with open(file_path, "rb") as f:
            f.seek(start_byte)
            remaining = end_byte - start_byte

            while remaining > 0:
                read_size = block_size if remaining > block_size else remaining
                block = f.read(read_size)
                if not block:
                    break
                remaining -= len(block)

                if remaining > 0 and not block.endswith(b"\n"):
                    tail = f.readline()
                    block += tail
                    remaining -= len(tail)

                for line in block.splitlines():
                    try:
                        cid_b, date_b, imp_b, clk_b, spend_b, conv_b = line.split(b",")
                        imp = int_fn(imp_b)
                        clk = int_fn(clk_b)
                        conv = int_fn(conv_b)
                        spend_val = float_fn(spend_b)

                        if (
                            imp >= clk >= 0
                            and conv >= 0
                            and spend_val >= 0.0
                            and date_b
                            and not date_b.isspace()
                        ):
                            spend_scaled = round_fn(spend_val * spend_scale)
                            valid_rows += 1
                            try:
                                acc = local_stats[cid_b]
                                acc[0] += imp
                                acc[1] += clk
                                acc[2] += spend_scaled
                                acc[3] += conv
                                acc[4] += 1
                            except KeyError:
                                local_stats[cid_b] = [imp, clk, spend_scaled, conv, 1]
                        else:
                            malformed_rows += 1
                    except (ValueError, OverflowError):
                        stripped = _strip_bom(line)
                        if not stripped:
                            continue
                        first_col = _strip_bom(stripped.split(b",", 1)[0])
                        if first_col.lower() == b"campaign_id":
                            continue
                        malformed_rows += 1
    finally:
        if gc_was_enabled:
            gc.enable()

    campaigns_dict: dict[str, tuple[int, int, int, int]] = {}
    for cid_b, acc in local_stats.items():
        cid_clean = _strip_bom(cid_b)
        if not cid_clean:
            valid_rows -= acc[4]
            malformed_rows += acc[4]
            continue
        if cid_clean.lower() == b"campaign_id":
            valid_rows -= acc[4]
            continue

        cid_str = cid_clean.decode("utf-8", errors="replace")
        existing = campaigns_dict.get(cid_str)
        if existing is None:
            campaigns_dict[cid_str] = (acc[0], acc[1], acc[2], acc[3])
        else:
            campaigns_dict[cid_str] = (
                existing[0] + acc[0],
                existing[1] + acc[1],
                existing[2] + acc[2],
                existing[3] + acc[3],
            )

    return campaigns_dict, valid_rows, malformed_rows


def _merge_chunk_output(
    campaigns: dict[str, CampaignStats],
    chunk_campaigns: dict[str, tuple[int, int, int, int]],
) -> None:
    """Merge a worker's raw tuple dictionary into the master CampaignStats map."""
    for cid, (imp, clk, spend_scaled, conv) in chunk_campaigns.items():
        stats = campaigns.get(cid)
        if stats is None:
            campaigns[cid] = CampaignStats(
                impressions=imp,
                clicks=clk,
                spend_scaled=spend_scaled,
                conversions=conv,
            )
        else:
            stats.impressions += imp
            stats.clicks += clk
            stats.spend_scaled += spend_scaled
            stats.conversions += conv


def resolve_engine(engine: str = "auto") -> str:
    """Resolve 'auto' to 'c' if available, else 'python'."""
    norm = engine.strip().lower()
    if norm not in ("auto", "c", "python"):
        raise ValueError(f"Unsupported engine '{engine}'. Choose from: auto, c, python")
    if norm == "c":
        if not is_c_engine_available():
            raise RuntimeError("Native C engine requested (--engine c), but C compiler/POSIX is unavailable.")
        return "c"
    if norm == "auto":
        return "c" if is_c_engine_available() else "python"
    return "python"


def aggregate_file(
    file_path: str | Path,
    workers: Optional[int] = None,
    chunk_size: Optional[int] = None,
    engine: str = "auto",
) -> AggregationResult:
    """Aggregate an ad performance CSV file using the selected parallel engine ('auto', 'c', or 'python')."""
    active_engine = resolve_engine(engine)

    effective_workers = (
        workers if (workers is not None and workers > 0) else (os.cpu_count() or 4)
    )

    path = Path(file_path)
    if chunk_size is not None:
        effective_chunk_size = chunk_size
    else:
        file_size = path.stat().st_size if (path.exists() and path.is_file()) else DEFAULT_CHUNK_SIZE
        if active_engine == "c":
            effective_chunk_size = max(
                64 * 1024,
                (file_size // max(1, effective_workers)) + 1,
            )
        else:
            effective_chunk_size = max(
                1024 * 1024,
                (file_size // max(1, effective_workers * 2)) + 1,
            )

    chunks = calculate_byte_chunks(path, chunk_size=effective_chunk_size)
    if not chunks:
        return AggregationResult(campaigns={}, valid_rows=0, malformed_rows=0)

    if active_engine == "c":
        return aggregate_file_c(path, chunks)

    campaigns: dict[str, CampaignStats] = {}
    total_valid = 0
    total_malformed = 0

    if len(chunks) == 1 or effective_workers == 1:
        for start_byte, end_byte in chunks:
            chunk_map, valid, malformed = process_chunk(path, start_byte, end_byte)
            _merge_chunk_output(campaigns, chunk_map)
            total_valid += valid
            total_malformed += malformed
    else:
        max_workers = min(effective_workers, len(chunks))
        gil_enabled = (
            sys._is_gil_enabled() if hasattr(sys, "_is_gil_enabled") else True
        )
        if not gil_enabled:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = [
                    executor.submit(process_chunk, path, start_byte, end_byte)
                    for start_byte, end_byte in chunks
                ]
                for future in futures:
                    chunk_map, valid, malformed = future.result()
                    _merge_chunk_output(campaigns, chunk_map)
                    total_valid += valid
                    total_malformed += malformed
        else:
            start_methods = mp.get_all_start_methods()
            mp_ctx = (
                mp.get_context("fork")
                if ("fork" in start_methods and sys.platform != "win32")
                else None
            )
            with ProcessPoolExecutor(
                max_workers=max_workers,
                mp_context=mp_ctx,
            ) as executor:
                futures = [
                    executor.submit(process_chunk, path, start_byte, end_byte)
                    for start_byte, end_byte in chunks
                ]
                for future in futures:
                    chunk_map, valid, malformed = future.result()
                    _merge_chunk_output(campaigns, chunk_map)
                    total_valid += valid
                    total_malformed += malformed

    return AggregationResult(
        campaigns=campaigns,
        valid_rows=total_valid,
        malformed_rows=total_malformed,
    )
