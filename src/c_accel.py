"""Native POSIX C (pread + pthread) acceleration bridge via Python's built-in ctypes."""

from __future__ import annotations

import ctypes
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
from typing import Optional

from src.models import AggregationResult, CampaignStats

_TABLE_SIZE: int = 1024
_SRC_DIR: Path = Path(__file__).resolve().parent
_C_SOURCE: Path = _SRC_DIR / "fast_agg.c"
_BUILD_DIR: Path = _SRC_DIR / "_build"

_C_LIB: Optional[ctypes.CDLL] = None
_C_LIB_CHECKED: bool = False


class _Bucket(ctypes.Structure):
    _fields_ = [
        ("cid", ctypes.c_char * 64),
        ("cid_len", ctypes.c_uint32),
        ("impressions", ctypes.c_int64),
        ("clicks", ctypes.c_int64),
        ("spend_scaled", ctypes.c_int64),
        ("conversions", ctypes.c_int64),
        ("used", ctypes.c_int),
    ]


class _WorkerArgs(ctypes.Structure):
    _fields_ = [
        ("fd", ctypes.c_int),
        ("start", ctypes.c_size_t),
        ("end", ctypes.c_size_t),
        ("table", _Bucket * _TABLE_SIZE),
        ("valid_rows", ctypes.c_int64),
        ("malformed_rows", ctypes.c_int64),
    ]


def _get_shared_lib_path() -> Path:
    ext = "dylib" if sys.platform == "darwin" else "so"
    arch = platform.machine().lower()
    return _BUILD_DIR / f"fast_agg_{sys.platform}_{arch}.{ext}"


def _load_or_compile_lib() -> Optional[ctypes.CDLL]:
    """Load the pre-compiled shared library, compiling `fast_agg.c` on the fly if needed."""
    global _C_LIB, _C_LIB_CHECKED
    if _C_LIB_CHECKED:
        return _C_LIB

    _C_LIB_CHECKED = True

    if sys.platform == "win32" or not _C_SOURCE.exists():
        return None

    lib_path = _get_shared_lib_path()
    needs_compile = not lib_path.exists() or (
        _C_SOURCE.stat().st_mtime > lib_path.stat().st_mtime
    )

    if needs_compile:
        compiler = (
            shutil.which("cc")
            or shutil.which("clang")
            or shutil.which("gcc")
        )
        if not compiler:
            return None

        try:
            _BUILD_DIR.mkdir(parents=True, exist_ok=True)
            tmp_path = lib_path.with_suffix(f".tmp.{os.getpid()}{lib_path.suffix}")
            cmd = [
                compiler,
                "-O3",
                "-shared",
                "-fPIC",
                "-pthread",
                str(_C_SOURCE),
                "-o",
                str(tmp_path),
            ]
            subprocess.run(
                cmd,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
            )
            os.replace(tmp_path, lib_path)
        except Exception:
            return None

    try:
        lib = ctypes.CDLL(str(lib_path))
        lib.process_all_parallel.argtypes = [
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.c_int,
            ctypes.POINTER(_WorkerArgs),
        ]
        lib.process_all_parallel.restype = ctypes.c_int
        _C_LIB = lib
        return _C_LIB
    except Exception:
        return None


def is_c_engine_available() -> bool:
    """Return True if the POSIX C accelerator shared library is available."""
    return _load_or_compile_lib() is not None


def aggregate_file_c(
    file_path: str | Path,
    chunks: list[tuple[int, int]],
) -> AggregationResult:
    """Aggregate pre-aligned byte chunks in parallel using POSIX pread + pthread."""
    lib = _load_or_compile_lib()
    if lib is None:
        raise RuntimeError(
            "Native C engine is unavailable on this machine (no C compiler or non-POSIX OS)."
        )

    if not chunks:
        return AggregationResult(campaigns={}, valid_rows=0, malformed_rows=0)

    n_chunks = len(chunks)
    starts = (ctypes.c_size_t * n_chunks)(*(s for s, _ in chunks))
    ends = (ctypes.c_size_t * n_chunks)(*(e for _, e in chunks))
    workers = (_WorkerArgs * n_chunks)()

    fd = os.open(file_path, os.O_RDONLY)
    try:
        rc = lib.process_all_parallel(fd, starts, ends, n_chunks, workers)
        if rc != 0:
            raise RuntimeError(f"C engine process_all_parallel failed with code {rc}")
    finally:
        os.close(fd)

    campaigns: dict[str, CampaignStats] = {}
    total_valid = 0
    total_malformed = 0

    for i in range(n_chunks):
        w = workers[i]
        total_valid += w.valid_rows
        total_malformed += w.malformed_rows
        for j in range(_TABLE_SIZE):
            b = w.table[j]
            if b.used:
                cid = b.cid[: b.cid_len].decode("utf-8", errors="replace")
                stats = campaigns.get(cid)
                if stats is None:
                    campaigns[cid] = CampaignStats(
                        impressions=b.impressions,
                        clicks=b.clicks,
                        spend_scaled=b.spend_scaled,
                        conversions=b.conversions,
                    )
                else:
                    stats.impressions += b.impressions
                    stats.clicks += b.clicks
                    stats.spend_scaled += b.spend_scaled
                    stats.conversions += b.conversions

    return AggregationResult(
        campaigns=campaigns,
        valid_rows=total_valid,
        malformed_rows=total_malformed,
    )
