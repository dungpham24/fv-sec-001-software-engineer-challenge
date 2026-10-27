# FV-SEC001 — Ad Performance Aggregator (Dual-Engine High-Performance Python Solution)

A high-performance, zero-third-party-dependency Python CLI application that processes the **1.04 GB (`26,843,544` rows)** advertising dataset (`ad_data.csv`), aggregates campaign metrics, and generates the Top 10 CTR (`top10_ctr.csv`) and Top 10 CPA (`top10_cpa.csv`) reports in **`0.259 seconds` (~4.03 GB/s)** using its built-in `ctypes` POSIX C accelerator, or **`2.20 seconds`** using its 100% Pure Python engine, with **`~23–32 MB` peak RAM**.

---

## Architecture & Dual-Engine Design

```text
ad_data.csv (1.04 GB, 26,843,544 rows, CRLF)
   │
   ├──► 1. Dynamic Zero-Straggler Byte-Range Chunker ("rb" mode, newline-aligned)
   │
   ├──► 2. Dual-Engine Parallel Map-Reduce (--engine auto | c | python)
   │       │
   │       ├─► [Engine A: Native C Accelerator (--engine c / default auto)]
   │       │     • Zero-copy pread() + pthread worker pool via Python ctypes
   │       │     • SIMD-backed memchr() newline scanner + custom fixed-point parser
   │       │     • L1-cached 1024-slot FNV-1a open-addressing hash table per thread
   │       │     • Throughput: ~4.03 GB/s (~103.6M rows/sec) -> 0.259s total
   │       │
   │       └─► [Engine B: Ultra Pure Python (--engine python / fallback)]
   │             • 8 MiB block streaming + C-level bytes.splitlines()
   │             • EAFP inner loop + direct dict subscripting + GC control
   │             • POSIX fork / ProcessPoolExecutor + Free-Threaded Python detection
   │             • Throughput: ~474 MB/s (~12.2M rows/sec) -> 2.200s total
   │
   └──► 3. Deterministic Ranking & Exporter
           ├─ results/top10_ctr.csv (Top 10 Highest CTR)
           └─ results/top10_cpa.csv (Top 10 Lowest CPA, excluding conversions == 0)
```

### Key Engineering Decisions
1. **Dual-Engine Architecture (`--engine [auto|c|python]`) with Zero `pip` Dependencies**:
   - **Engine `c` (`src/fast_agg.c` + `src/c_accel.py`)**: Uses Python's standard library `ctypes` to invoke a lightweight, auto-compiled POSIX C99 kernel (`pread` + `pthread` + `FNV-1a` open-addressing hash table). Processes all 26.8M rows in **`0.259s`** while keeping Peak RSS at **`~32.2 MB`**.
   - **Engine `python` (`src/processor.py`)**: A 100% Pure Python implementation using `8 MiB` block `splitlines()`, an EAFP (`try/except`) inner loop that avoids per-row LBYL string checks, deferred UTF-8 key decoding, and dynamic zero-straggler chunking. Runs in **`2.20s`** and serves as an automatic fallback if no C compiler is present.
2. **Dynamic Zero-Straggler Byte Chunking**:
   - Instead of fixed `32 MB` chunks (which leave idle CPU cores on the final remainder chunks), `aggregate_file()` dynamically divides `file_size` into equal newline-aligned slices matched to the worker count (`1× workers` for `pthread` workers, `2× workers` for Python `ProcessPoolExecutor`), eliminating tail latency.
3. **Binary Mode (`"rb"`) to Prevent CRLF (`\r\n`) Byte-Offset Drift**:
   - The official `ad_data.csv` dataset uses Windows **`\r\n` (CRLF)** line endings. Reading in text mode (`"r"`) translates `\r\n` (2 bytes) into `\n` (1 byte), causing byte offset counters to drift by ~26.8 MB across the file. Both engines operate strictly on raw byte offsets.
4. **Exact Integer-Scaled Currency Math (`SPEND_SCALE = 10_000`)**:
   - Summing 26.8 million IEEE-754 floating-point `spend` values in standard `float` introduces cumulative representation drift. Both engines accumulate `spend` as scaled 64-bit / arbitrary-precision integers (`spend_scaled = round(spend * 10_000)`), ensuring zero floating-point drift.
5. **64-Bit Integer Safety**:
   - Each campaign accumulates **~13.7 billion impressions**, which overflows a 32-bit signed integer (`2,147,483,647`) by more than 6×. Using `int64_t` in C and arbitrary-precision `int` in Python prevents overflow.

---

## Project Structure

```text
.
├── aggregator.py              # CLI entrypoint (--input, --output, --engine, --workers)
├── src/
│   ├── __init__.py            # Package exports
│   ├── models.py              # CampaignStats & AggregationResult dataclasses
│   ├── processor.py           # Dynamic byte chunker & Ultra Pure Python engine
│   ├── c_accel.py             # ctypes loader & auto-builder for the POSIX C engine
│   ├── fast_agg.c             # Native C99 pread + pthread + FNV-1a parallel kernel
│   └── exporter.py            # Top-10 ranking, deterministic tie-breaking, CSV writer
├── tests/
│   └── test_aggregator.py     # 13 test suites verifying all 12 edge cases on BOTH engines
├── results/
│   ├── top10_ctr.csv          # Generated Top 10 highest CTR campaigns
│   └── top10_cpa.csv          # Generated Top 10 lowest CPA campaigns
├── Dockerfile                 # Containerized build with pre-compiled C accelerator
├── PROMPTS.md                 # Raw, unedited AI assistant prompts used
└── README.md                  # Documentation & benchmark report
```

---

## Setup & Usage Instructions

### Prerequisites
- **Python 3.10+** (tested on Python 3.12 and Python 3.14)
- **Zero external third-party packages required** — uses **100% Python Standard Library** (and auto-compiles `src/fast_agg.c` if `cc`/`clang`/`gcc` is available, falling back automatically to Pure Python otherwise).

### 1. Unzip the Dataset
```bash
unzip ad_data.csv.zip
```

### 2. Run the Aggregator (CLI)
```bash
# Default (auto-selects Native C engine if compiler is available, else Pure Python)
python3 aggregator.py --input ad_data.csv --output results/

# Explicitly run with the Native C engine (~0.26s)
python3 aggregator.py --input ad_data.csv --output results/ --engine c

# Explicitly run with the 100% Pure Python engine (~2.20s)
python3 aggregator.py --input ad_data.csv --output results/ --engine python
```

#### CLI Options
- `--input`, `-i`: Path to input CSV file *(required)*.
- `--output`, `-o`: Directory for output CSV files *(required)*.
- `--engine`, `-e`: Execution engine: `auto` (default), `c`, or `python`.
- `--workers`, `-w`: Number of parallel workers (`0` = auto-detect CPU cores, `1` = single-worker streaming).
- `--chunk-size-mb`: Override byte chunk size in MB (default: dynamically balanced across workers).

### 3. Run Automated Unit & Edge-Case Tests
```bash
python3 -m unittest discover -s tests -v
```

### 4. Run with Docker (Optional)
```bash
# Build image (pre-compiles the C shared library inside python:3.12-slim)
docker build -t fv-ad-aggregator .

# Run container mounting current directory
docker run --rm -v "$(pwd):/data" fv-ad-aggregator --input /data/ad_data.csv --output /data/results/
```

---

## Libraries Used

Only modules from the **Python Standard Library** are used:
- `os`, `sys`, `pathlib`, `platform`, `shutil`, `subprocess` — File system metadata and C shared library build management
- `ctypes` — Zero-overhead FFI binding to the POSIX C `pread`/`pthread` kernel
- `gc`, `math` — Garbage collector control and `NaN`/`Inf` validation
- `argparse`, `dataclasses`, `time` — CLI interface, data models, and high-resolution timing
- `multiprocessing`, `concurrent.futures` — Parallel execution (`fork` / `ProcessPoolExecutor` / `ThreadPoolExecutor`)
- `unittest`, `tempfile` — Automated dual-engine test suite

---

## Benchmark Results (1.04 GB `ad_data.csv`)

### Test Environment
- **Dataset**: `ad_data.csv` (`1,043,304,870 bytes` / `~1.04 GB`, `26,843,544` valid data rows, `50` unique campaigns)
- **OS / Hardware**: macOS (Apple Silicon, 10 CPU cores, 16 GB RAM)
- **Python Version**: Python 3.14.7 (`clang-2100.1.1.101`)

### Performance Comparison Across Engines

| Engine Mode | Command | Processing Time | Throughput | Peak RSS (`maxrss`) | Peak Footprint | Speedup vs 1-Worker |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: |
| **Native C Parallel (`--engine c`)** | `python3 aggregator.py -i ad_data.csv -o results/ -e c` | **0.259 s** (`0.32s` wall) | **4.03 GB/s** (`103.6M rows/s`) | **32.20 MB** | **23.31 MB** | **59.6×** |
| **Ultra Pure Python Parallel (`--engine python`)** | `python3 aggregator.py -i ad_data.csv -o results/ -e python` | **2.200 s** (`2.25s` wall) | **474 MB/s** (`12.2M rows/s`) | **47.00 MB** | **18.11 MB** | **7.0×** |
| **Baseline Pure Python Parallel (v1)** | Fixed `32MB` chunks + line-by-line `readline()` | `3.528 s` (`3.60s` wall) | `295 MB/s` (`7.6M rows/s`) | `26.44 MB` | `17.56 MB` | `4.4×` |
| **Single-Worker Pure Python (`-w 1`)** | Sequential streaming on 1 CPU core | `15.439 s` (`15.50s` wall) | `67.6 MB/s` (`1.7M rows/s`) | `24.75 MB` | `16.13 MB` | `1.0×` |

*(Cross-verification via `diff -u` confirmed **100% byte-for-byte identical CSV outputs** across `--engine c`, `--engine python`, and `--workers 1`.)*

### Raw Benchmark Logs (`/usr/bin/time -l`)

#### 1. Native C Engine (`--engine c` / `--engine auto`)
```text
$ /usr/bin/time -l python3 aggregator.py --input ad_data.csv --output results/ --engine c
Aggregation complete in 0.259s [engine=c] | Campaigns: 50 | Valid rows: 26,843,544 | Malformed rows skipped: 0
Outputs written to: results/top10_ctr.csv and results/top10_cpa.csv
        0.32 real         1.35 user         0.19 sys
            33767424  maximum resident set size
            24445360  peak memory footprint
```

#### 2. Ultra Pure Python Engine (`--engine python`)
```text
$ /usr/bin/time -l python3 aggregator.py --input ad_data.csv --output results/ --engine python
Aggregation complete in 2.200s [engine=python] | Campaigns: 50 | Valid rows: 26,843,544 | Malformed rows skipped: 0
Outputs written to: results/top10_ctr.csv and results/top10_cpa.csv
        2.25 real        15.90 user         0.62 sys
            49283072  maximum resident set size
            18989488  peak memory footprint
```

---

## Output Results

### `results/top10_ctr.csv` (Top 10 Highest CTR)
```csv
campaign_id,total_impressions,total_clicks,total_spend,total_conversions,CTR,CPA
CMP005,13648608306,375627610,394780333.96,20403485,0.0275,19.35
CMP022,13703365735,377109904,395899684.64,20480486,0.0275,19.33
CMP023,13657508417,375769033,394203576.87,20390213,0.0275,19.33
CMP011,13714973625,377330481,396309000.78,20463005,0.0275,19.37
CMP018,13708338704,377076026,396003175.53,20477639,0.0275,19.34
CMP026,13652760288,375491308,394442607.58,20370126,0.0275,19.36
CMP019,13675562649,376096719,395062881.56,20415450,0.0275,19.35
CMP042,13717935895,377260350,396576467.39,20507021,0.0275,19.34
CMP004,13705584584,376906722,395992597.40,20491770,0.0275,19.32
CMP027,13719269461,377233894,396153928.92,20462054,0.0275,19.36
```

### `results/top10_cpa.csv` (Top 10 Lowest CPA, `conversions > 0`)
```csv
campaign_id,total_impressions,total_clicks,total_spend,total_conversions,CTR,CPA
CMP016,13686317849,375883156,393677129.86,20410131,0.0275,19.29
CMP049,13678441909,375790166,393822976.90,20408613,0.0275,19.30
CMP032,13707961325,376277379,395003523.04,20469062,0.0274,19.30
CMP034,13663387190,375490759,394029600.32,20403716,0.0275,19.31
CMP040,13707493063,376364561,395064372.66,20444877,0.0275,19.32
CMP004,13705584584,376906722,395992597.40,20491770,0.0275,19.32
CMP047,13704337167,376533784,395185526.77,20447393,0.0275,19.33
CMP045,13689842314,376256850,394756303.73,20424317,0.0275,19.33
CMP022,13703365735,377109904,395899684.64,20480486,0.0275,19.33
CMP024,13665607452,375614418,394482754.56,20406008,0.0275,19.33
```

---

## 12 Edge Cases Covered & Verified on Both Engines

1. **CRLF (`\r\n`) Byte-Offset Alignment**: Binary `"rb"` seeking & reading ensures zero drift on Windows line endings.
2. **Chunk Split Between `\r` and `\n`**: Tested with tiny chunk sizes (`17`, `25`, `37`, `64` bytes) across multiple workers on both `c` and `python` engines.
3. **UTF-8 BOM (`\xef\xbb\xbf`) & Repeated Headers**: Automatically strips BOM and skips repeated `campaign_id,...` headers mid-file.
4. **Dynamic Zero-Straggler Chunking & Small File Fast-Path**: Balances chunks across cores and bypasses process pool creation for single-chunk files.
5. **Zero Conversions (`total_conversions == 0`)**: Strictly excluded from `top10_cpa.csv` and formatted as `null` in `top10_ctr.csv`.
6. **Zero Impressions (`total_impressions == 0`)**: Handled without `ZeroDivisionError` (`CTR = 0.0`) and excluded from top CTR ranking.
7. **Invalid Domain Values (`NaN`, `Inf`, negative numbers, `clicks > impressions`)**: Safely rejected and counted in `malformed_rows`.
8. **Malformed CSV Rows**: Missing/extra columns, non-numeric strings, and blank lines are skipped gracefully without crashing.
9. **Floating-Point Drift**: Scaled integer accumulation (`SPEND_SCALE = 10_000`) ensures exact currency totals.
10. **Deterministic Tie-Breaking**:
    - `top10_ctr.csv`: `(-ctr, -clicks, -impressions, campaign_id)`
    - `top10_cpa.csv`: `(cpa, -conversions, spend_scaled, campaign_id)`
11. **Fewer Than 10 Campaigns / Empty / Header-Only Files**: Produces valid CSV files with headers without errors.
12. **Missing Trailing Newline at EOF**: Final line is processed completely even if not terminated by `\n`.
