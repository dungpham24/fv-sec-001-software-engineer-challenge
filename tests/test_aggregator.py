"""Comprehensive automated test suite for the FV-SEC001 Ad Performance Aggregator.

Covers all 12 architectural, numerical, domain, and I/O edge cases specified in the
implementation plan against BOTH the Ultra Pure Python engine (`engine="python"`) and
the Native C pread+pthread engine (`engine="c"`), plus cross-engine equivalence:
  1. Official README 5-row sample dataset accuracy (CTR, CPA, totals, ranking order)
  2. Windows CRLF (\\r\\n) byte-offset alignment & tiny chunk boundary splits (middle of \\r\\n)
  3. Zero conversions (conversions = 0) excluded from top10_cpa.csv & formatted as 'null' in top10_ctr.csv
  4. Zero impressions (impressions = 0) safe CTR = 0.0 calculation & exclusion from top10_ctr.csv
  5. IEEE-754 special values (NaN, inf, -inf), negative metrics, and clicks > impressions rejection
  6. Malformed rows (missing/extra columns, non-numeric strings, empty fields, blank lines, repeated headers)
  7. UTF-8 BOM (\\xef\\xbb\\xbf) header/data handling and missing trailing newline at EOF
  8. Exact integer-scaled spend accumulation (SPEND_SCALE = 10,000) preventing float drift
  9. Deterministic tie-breaking for identical CTR and identical CPA campaigns
 10. Fewer than 10 campaigns, header-only files, and 0-byte empty files
 11. End-to-end CLI execution (aggregator.main) with --engine python|c|auto and error handling
 12. Model merge semantics, chunk validation guards, and 100% C vs Python engine equivalence
"""

from __future__ import annotations

import contextlib
import io
from pathlib import Path
import tempfile
import unittest

import aggregator
from src.exporter import (
    CSV_HEADER,
    format_csv_row,
    get_top10_cpa,
    get_top10_ctr,
    write_results,
)
from src.models import AggregationResult, CampaignStats, SPEND_SCALE
from src.processor import aggregate_file, calculate_byte_chunks

ENGINES: tuple[str, ...] = ("python", "c")


class TestAdAggregatorSuite(unittest.TestCase):
    """Comprehensive unit and integration tests for Dual-Engine Ad Performance Aggregator."""

    def setUp(self) -> None:
        self._tmp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp_dir.name)

    def tearDown(self) -> None:
        self._tmp_dir.cleanup()

    def _write_bytes(self, name: str, data: bytes) -> Path:
        path = self.tmp_path / name
        path.write_bytes(data)
        return path

    def _write_text(self, name: str, content: str) -> Path:
        path = self.tmp_path / name
        path.write_bytes(content.encode("utf-8"))
        return path

    # ------------------------------------------------------------------ #
    # 1. Official README 5-Row Sample Dataset (Both Engines)
    # ------------------------------------------------------------------ #
    def test_readme_sample_data(self) -> None:
        """Verify exact totals, CTR, CPA, and CSV formatting on the 5-row README sample across both engines."""
        sample_csv = (
            "campaign_id,date,impressions,clicks,spend,conversions\n"
            "CMP001,2025-01-01,12000,300,45.50,12\n"
            "CMP002,2025-01-01,8000,120,28.00,4\n"
            "CMP001,2025-01-02,14000,340,48.20,15\n"
            "CMP003,2025-01-01,5000,60,15.00,3\n"
            "CMP002,2025-01-02,8500,150,31.00,5\n"
        )
        csv_path = self._write_text("readme_sample.csv", sample_csv)

        for engine in ENGINES:
            with self.subTest(engine=engine):
                result = aggregate_file(csv_path, workers=2, engine=engine)
                self.assertEqual(result.valid_rows, 5)
                self.assertEqual(result.malformed_rows, 0)
                self.assertEqual(set(result.campaigns.keys()), {"CMP001", "CMP002", "CMP003"})

                cmp1 = result.campaigns["CMP001"]
                self.assertEqual(cmp1.impressions, 26000)
                self.assertEqual(cmp1.clicks, 640)
                self.assertAlmostEqual(cmp1.total_spend, 93.70, places=4)
                self.assertEqual(cmp1.conversions, 27)
                self.assertAlmostEqual(cmp1.ctr, 640 / 26000, places=6)
                self.assertIsNotNone(cmp1.cpa)
                self.assertAlmostEqual(cmp1.cpa or 0.0, 93.70 / 27, places=6)

                cmp2 = result.campaigns["CMP002"]
                self.assertEqual(cmp2.impressions, 16500)
                self.assertEqual(cmp2.clicks, 270)
                self.assertAlmostEqual(cmp2.total_spend, 59.00, places=4)
                self.assertEqual(cmp2.conversions, 9)
                self.assertAlmostEqual(cmp2.ctr, 270 / 16500, places=6)
                self.assertAlmostEqual(cmp2.cpa or 0.0, 59.00 / 9, places=6)

                cmp3 = result.campaigns["CMP003"]
                self.assertEqual(cmp3.impressions, 5000)
                self.assertEqual(cmp3.clicks, 60)
                self.assertAlmostEqual(cmp3.total_spend, 15.00, places=4)
                self.assertEqual(cmp3.conversions, 3)
                self.assertAlmostEqual(cmp3.ctr, 0.0120, places=6)
                self.assertAlmostEqual(cmp3.cpa or 0.0, 5.00, places=6)

                out_dir = self.tmp_path / f"readme_out_{engine}"
                ctr_path, cpa_path = write_results(out_dir, result.campaigns)

                ctr_lines = ctr_path.read_text(encoding="utf-8").splitlines()
                self.assertEqual(
                    ctr_lines,
                    [
                        CSV_HEADER,
                        "CMP001,26000,640,93.70,27,0.0246,3.47",
                        "CMP002,16500,270,59.00,9,0.0164,6.56",
                        "CMP003,5000,60,15.00,3,0.0120,5.00",
                    ],
                )

                cpa_lines = cpa_path.read_text(encoding="utf-8").splitlines()
                self.assertEqual(
                    cpa_lines,
                    [
                        CSV_HEADER,
                        "CMP001,26000,640,93.70,27,0.0246,3.47",
                        "CMP003,5000,60,15.00,3,0.0120,5.00",
                        "CMP002,16500,270,59.00,9,0.0164,6.56",
                    ],
                )

    # ------------------------------------------------------------------ #
    # 2. CRLF (\r\n) Line Endings & Tiny Chunk Boundary Alignment
    # ------------------------------------------------------------------ #
    def test_crlf_and_tiny_chunk_boundaries(self) -> None:
        """Verify binary CRLF chunking across tiny chunk sizes (17, 25, 37, 64 bytes) with 4 workers on both engines."""
        rows = ["campaign_id,date,impressions,clicks,spend,conversions"]
        for i in range(300):
            cid = f"CMP{(i % 15) + 1:03d}"
            day = (i % 28) + 1
            imp = 1000 + (i * 7)
            clk = 10 + (i % 50)
            spend = f"{12.34 + (i % 10) * 0.25:.2f}"
            conv = i % 6
            rows.append(f"{cid},2025-03-{day:02d},{imp},{clk},{spend},{conv}")

        crlf_bytes = ("\r\n".join(rows) + "\r\n").encode("utf-8")
        csv_path = self._write_bytes("crlf_dataset.csv", crlf_bytes)

        baseline = aggregate_file(csv_path, workers=1, chunk_size=1024 * 1024, engine="python")
        self.assertEqual(baseline.valid_rows, 300)
        self.assertEqual(baseline.malformed_rows, 0)

        for engine in ENGINES:
            for chunk_size in (17, 25, 37, 64):
                with self.subTest(engine=engine, chunk_size=chunk_size):
                    chunks = calculate_byte_chunks(csv_path, chunk_size=chunk_size)
                    self.assertGreater(len(chunks), 50)
                    self.assertEqual(chunks[0][0], 0)
                    self.assertEqual(chunks[-1][1], len(crlf_bytes))
                    for idx in range(len(chunks) - 1):
                        self.assertEqual(chunks[idx][1], chunks[idx + 1][0])

                    parallel_res = aggregate_file(
                        csv_path, workers=4, chunk_size=chunk_size, engine=engine
                    )
                    self.assertEqual(
                        parallel_res.valid_rows,
                        baseline.valid_rows,
                        f"Row count mismatch for engine={engine}, chunk_size={chunk_size}",
                    )
                    self.assertEqual(parallel_res.malformed_rows, 0)
                    self.assertEqual(
                        set(parallel_res.campaigns.keys()), set(baseline.campaigns.keys())
                    )

                    for cid, expected_stats in baseline.campaigns.items():
                        actual_stats = parallel_res.campaigns[cid]
                        self.assertEqual(actual_stats.impressions, expected_stats.impressions)
                        self.assertEqual(actual_stats.clicks, expected_stats.clicks)
                        self.assertEqual(actual_stats.spend_scaled, expected_stats.spend_scaled)
                        self.assertEqual(actual_stats.conversions, expected_stats.conversions)

    # ------------------------------------------------------------------ #
    # 3. Zero Conversions Handling (Division by Zero Protection)
    # ------------------------------------------------------------------ #
    def test_zero_conversions_handling(self) -> None:
        """Verify campaigns with conversions=0 are excluded from top10_cpa.csv and show 'null' CPA in top10_ctr.csv."""
        csv_content = (
            "campaign_id,date,impressions,clicks,spend,conversions\n"
            "CMP_ZERO_CONV,2025-01-01,10000,900,150.00,0\n"
            "CMP_WITH_CONV,2025-01-01,10000,200,50.00,10\n"
        )
        csv_path = self._write_text("zero_conv.csv", csv_content)

        for engine in ENGINES:
            with self.subTest(engine=engine):
                result = aggregate_file(csv_path, workers=1, engine=engine)
                zero_conv_stats = result.campaigns["CMP_ZERO_CONV"]
                self.assertIsNone(zero_conv_stats.cpa)
                self.assertEqual(
                    format_csv_row("CMP_ZERO_CONV", zero_conv_stats),
                    "CMP_ZERO_CONV,10000,900,150.00,0,0.0900,null",
                )

                top_cpa = get_top10_cpa(result.campaigns)
                self.assertEqual([cid for cid, _ in top_cpa], ["CMP_WITH_CONV"])

                top_ctr = get_top10_ctr(result.campaigns)
                self.assertEqual(
                    [cid for cid, _ in top_ctr], ["CMP_ZERO_CONV", "CMP_WITH_CONV"]
                )

                out_dir = self.tmp_path / f"zero_conv_out_{engine}"
                ctr_path, cpa_path = write_results(out_dir, result.campaigns)
                ctr_text = ctr_path.read_text(encoding="utf-8")
                cpa_text = cpa_path.read_text(encoding="utf-8")

                self.assertIn("CMP_ZERO_CONV,10000,900,150.00,0,0.0900,null", ctr_text)
                self.assertNotIn("CMP_ZERO_CONV", cpa_text)

    # ------------------------------------------------------------------ #
    # 4. Zero Impressions Handling (Division by Zero Protection)
    # ------------------------------------------------------------------ #
    def test_zero_impressions_handling(self) -> None:
        """Verify campaigns with impressions=0 have ctr=0.0, do not raise ZeroDivisionError, and are excluded from top10_ctr.csv."""
        csv_content = (
            "campaign_id,date,impressions,clicks,spend,conversions\n"
            "CMP_ZERO_IMP,2025-01-01,0,0,10.00,2\n"
            "CMP_NORMAL,2025-01-01,5000,100,25.00,5\n"
        )
        csv_path = self._write_text("zero_imp.csv", csv_content)

        for engine in ENGINES:
            with self.subTest(engine=engine):
                result = aggregate_file(csv_path, workers=1, engine=engine)
                zero_imp_stats = result.campaigns["CMP_ZERO_IMP"]
                self.assertEqual(zero_imp_stats.impressions, 0)
                self.assertEqual(zero_imp_stats.ctr, 0.0)
                self.assertAlmostEqual(zero_imp_stats.cpa or 0.0, 5.00, places=4)

                top_ctr = get_top10_ctr(result.campaigns)
                self.assertEqual([cid for cid, _ in top_ctr], ["CMP_NORMAL"])

                top_cpa = get_top10_cpa(result.campaigns)
                self.assertEqual(
                    set(cid for cid, _ in top_cpa), {"CMP_ZERO_IMP", "CMP_NORMAL"}
                )

    # ------------------------------------------------------------------ #
    # 5. NaN, Inf, Negative Values, and clicks > impressions
    # ------------------------------------------------------------------ #
    def test_nan_inf_and_negative_values(self) -> None:
        """Verify rows with NaN/Inf spend, negative values, or clicks > impressions are rejected across both engines."""
        csv_content = (
            "campaign_id,date,impressions,clicks,spend,conversions\n"
            "CMP001,2025-01-01,1000,50,20.00,5\n"
            "CMP001,2025-01-02,1000,50,NaN,5\n"
            "CMP001,2025-01-03,1000,50,inf,5\n"
            "CMP001,2025-01-04,1000,50,-inf,5\n"
            "CMP001,2025-01-05,-100,10,20.00,1\n"
            "CMP001,2025-01-06,1000,-5,20.00,1\n"
            "CMP001,2025-01-07,1000,50,-0.01,1\n"
            "CMP001,2025-01-08,1000,50,20.00,-2\n"
            "CMP001,2025-01-09,100,101,20.00,1\n"
        )
        csv_path = self._write_text("nan_inf_neg.csv", csv_content)

        for engine in ENGINES:
            with self.subTest(engine=engine):
                result = aggregate_file(csv_path, workers=2, engine=engine)
                self.assertEqual(result.valid_rows, 1)
                self.assertEqual(result.malformed_rows, 8)
                stats = result.campaigns["CMP001"]
                self.assertEqual(stats.impressions, 1000)
                self.assertEqual(stats.clicks, 50)
                self.assertAlmostEqual(stats.total_spend, 20.00, places=4)
                self.assertEqual(stats.conversions, 5)

    # ------------------------------------------------------------------ #
    # 6. Malformed Rows, Blank Lines & Repeated Header Rows
    # ------------------------------------------------------------------ #
    def test_malformed_rows_and_repeated_headers(self) -> None:
        """Verify malformed columns, non-numeric values, empty IDs/dates, blank lines, and repeated headers across both engines."""
        csv_content = (
            "campaign_id,date,impressions,clicks,spend,conversions\n"
            "\n"
            "   \r\n"
            "CMP001,2025-01-01,1000,100,50.00,10\n"
            "campaign_id,date,impressions,clicks,spend,conversions\n"
            "CMP001,2025-01-02,1000,100\n"
            "CMP001,2025-01-03,1000,100,50.00,10,EXTRA_COL\n"
            "CMP001,2025-01-04,abc,100,50.00,10\n"
            "CMP001,2025-01-05,1000,xyz,50.00,10\n"
            "CMP001,2025-01-06,1000,100,not_a_float,10\n"
            "CMP001,2025-01-07,1000,100,50.00,1.5\n"
            ",2025-01-08,1000,100,50.00,10\n"
            "CMP001,,1000,100,50.00,10\n"
            "  CMP001  , 2025-01-09 , 2000 , 200 , 75.25 , 15 \n"
        )
        csv_path = self._write_text("malformed_and_headers.csv", csv_content)

        for engine in ENGINES:
            with self.subTest(engine=engine):
                result = aggregate_file(csv_path, workers=2, engine=engine)
                self.assertEqual(result.valid_rows, 2)
                self.assertEqual(result.malformed_rows, 8)
                self.assertIn("CMP001", result.campaigns)
                stats = result.campaigns["CMP001"]
                self.assertEqual(stats.impressions, 3000)
                self.assertEqual(stats.clicks, 300)
                self.assertAlmostEqual(stats.total_spend, 125.25, places=4)
                self.assertEqual(stats.conversions, 25)

    # ------------------------------------------------------------------ #
    # 7. UTF-8 BOM (\xef\xbb\xbf) Header & Missing Trailing Newline at EOF
    # ------------------------------------------------------------------ #
    def test_utf8_bom_header_and_no_trailing_newline(self) -> None:
        """Verify files starting with UTF-8 BOM and missing a final newline at EOF are parsed accurately across both engines."""
        raw_bytes = (
            b"\xef\xbb\xbfcampaign_id,date,impressions,clicks,spend,conversions\r\n"
            b"CMP001,2025-01-01,4000,80,16.00,4\r\n"
            b"CMP002,2025-01-01,6000,120,24.00,6"  # No trailing \r\n at EOF
        )
        csv_path = self._write_bytes("bom_no_eof_nl.csv", raw_bytes)

        headerless_bom = b"\xef\xbb\xbfCMP099,2025-01-01,1000,10,5.00,1"
        headerless_path = self._write_bytes("headerless_bom.csv", headerless_bom)

        for engine in ENGINES:
            with self.subTest(engine=engine):
                result = aggregate_file(csv_path, workers=2, chunk_size=40, engine=engine)
                self.assertEqual(result.valid_rows, 2)
                self.assertEqual(result.malformed_rows, 0)
                self.assertEqual(set(result.campaigns.keys()), {"CMP001", "CMP002"})
                self.assertEqual(result.campaigns["CMP001"].impressions, 4000)
                self.assertEqual(result.campaigns["CMP002"].impressions, 6000)

                headerless_res = aggregate_file(headerless_path, workers=1, engine=engine)
                self.assertEqual(headerless_res.valid_rows, 1)
                self.assertIn("CMP099", headerless_res.campaigns)

    # ------------------------------------------------------------------ #
    # 8. IEEE-754 Floating-Point Precision (Exact Integer Scaling)
    # ------------------------------------------------------------------ #
    def test_floating_point_precision(self) -> None:
        """Sum 10,000 rows of spend=0.10 and spend=0.20 across both engines, verifying exact total_spend == 3000.00."""
        lines = ["campaign_id,date,impressions,clicks,spend,conversions"]
        for _ in range(10_000):
            lines.append("CMP_FLOAT,2025-01-01,100,10,0.10,1")
            lines.append("CMP_FLOAT,2025-01-01,100,10,0.20,1")

        csv_path = self._write_text("float_precision.csv", "\n".join(lines) + "\n")

        for engine in ENGINES:
            with self.subTest(engine=engine):
                result = aggregate_file(
                    csv_path, workers=4, chunk_size=64 * 1024, engine=engine
                )
                self.assertEqual(result.valid_rows, 20_000)
                stats = result.campaigns["CMP_FLOAT"]
                self.assertEqual(stats.spend_scaled, 3000 * SPEND_SCALE)
                self.assertEqual(stats.total_spend, 3000.0)
                self.assertEqual(f"{stats.total_spend:.2f}", "3000.00")
                self.assertEqual(stats.cpa, 0.15)

    # ------------------------------------------------------------------ #
    # 9. Deterministic Tie-Breaking for Identical CTR and CPA
    # ------------------------------------------------------------------ #
    def test_deterministic_tie_breaking(self) -> None:
        """Verify tie-breaking rules for identical CTR and identical CPA."""
        ctr_campaigns = {
            "CMP_CTR_C": CampaignStats(
                impressions=1000, clicks=100, spend_scaled=10 * SPEND_SCALE, conversions=1
            ),
            "CMP_CTR_A": CampaignStats(
                impressions=1000, clicks=100, spend_scaled=10 * SPEND_SCALE, conversions=1
            ),
            "CMP_CTR_B": CampaignStats(
                impressions=2000, clicks=200, spend_scaled=10 * SPEND_SCALE, conversions=1
            ),
        }
        ranked_ctr = get_top10_ctr(ctr_campaigns)
        self.assertEqual([cid for cid, _ in ranked_ctr], ["CMP_CTR_B", "CMP_CTR_A", "CMP_CTR_C"])

        cpa_campaigns = {
            "CMP019": CampaignStats(
                impressions=1000, clicks=50, spend_scaled=10 * SPEND_SCALE, conversions=1
            ),
            "CMP007": CampaignStats(
                impressions=1000, clicks=50, spend_scaled=10 * SPEND_SCALE, conversions=1
            ),
            "CMP_CPA_HIGH_CONV": CampaignStats(
                impressions=1000, clicks=50, spend_scaled=20 * SPEND_SCALE, conversions=2
            ),
        }
        ranked_cpa = get_top10_cpa(cpa_campaigns)
        self.assertEqual(
            [cid for cid, _ in ranked_cpa],
            ["CMP_CPA_HIGH_CONV", "CMP007", "CMP019"],
        )

    # ------------------------------------------------------------------ #
    # 10. Fewer Than 10 Campaigns, Header-Only Files & 0-Byte Empty Files
    # ------------------------------------------------------------------ #
    def test_fewer_than_10_campaigns_and_empty_files(self) -> None:
        """Verify < 10 campaigns, header-only files, and 0-byte empty files produce valid CSVs across both engines."""
        empty_path = self._write_bytes("empty.csv", b"")
        header_only_path = self._write_text(
            "header_only.csv",
            "campaign_id,date,impressions,clicks,spend,conversions\n",
        )
        two_path = self._write_text(
            "two_campaigns.csv",
            "campaign_id,date,impressions,clicks,spend,conversions\n"
            "CMP001,2025-01-01,1000,100,10.00,2\n"
            "CMP002,2025-01-01,2000,100,20.00,1\n",
        )

        for engine in ENGINES:
            with self.subTest(engine=engine):
                empty_res = aggregate_file(empty_path, workers=2, engine=engine)
                self.assertEqual(empty_res.valid_rows, 0)
                self.assertEqual(empty_res.malformed_rows, 0)
                self.assertEqual(empty_res.campaigns, {})

                empty_out = self.tmp_path / f"empty_out_{engine}"
                ctr_p, cpa_p = write_results(empty_out, empty_res.campaigns)
                self.assertEqual(ctr_p.read_text(encoding="utf-8"), CSV_HEADER + "\n")
                self.assertEqual(cpa_p.read_text(encoding="utf-8"), CSV_HEADER + "\n")

                header_res = aggregate_file(header_only_path, workers=2, engine=engine)
                self.assertEqual(header_res.valid_rows, 0)
                self.assertEqual(header_res.campaigns, {})

                two_res = aggregate_file(two_path, workers=2, engine=engine)
                self.assertEqual(len(get_top10_ctr(two_res.campaigns, limit=10)), 2)
                self.assertEqual(len(get_top10_cpa(two_res.campaigns, limit=10)), 2)
                self.assertEqual(get_top10_ctr(two_res.campaigns, limit=0), [])
                self.assertEqual(get_top10_cpa(two_res.campaigns, limit=0), [])

    # ------------------------------------------------------------------ #
    # 11. CLI End-to-End Execution & Missing/Invalid Input Handling
    # ------------------------------------------------------------------ #
    def test_cli_end_to_end_and_missing_file(self) -> None:
        """Test aggregator.main() end-to-end with --engine python|c|auto and non-zero exit codes on invalid inputs."""
        csv_path = self._write_text(
            "cli_input.csv",
            "campaign_id,date,impressions,clicks,spend,conversions\n"
            "CMP001,2025-01-01,5000,250,40.00,8\n",
        )

        for engine in ("python", "c", "auto"):
            with self.subTest(engine=engine):
                out_dir = self.tmp_path / f"cli_output_{engine}"
                stdout_buf = io.StringIO()
                with contextlib.redirect_stdout(stdout_buf):
                    exit_code = aggregator.main(
                        [
                            "--input",
                            str(csv_path),
                            "--output",
                            str(out_dir),
                            "--workers",
                            "2",
                            "--chunk-size-mb",
                            "1",
                            "--engine",
                            engine,
                        ]
                    )
                self.assertEqual(exit_code, 0)
                self.assertTrue((out_dir / "top10_ctr.csv").is_file())
                self.assertTrue((out_dir / "top10_cpa.csv").is_file())

        out_dir = self.tmp_path / "cli_output_err"
        stderr_buf = io.StringIO()
        with contextlib.redirect_stderr(stderr_buf):
            missing_code = aggregator.main(
                [
                    "--input",
                    str(self.tmp_path / "non_existent.csv"),
                    "--output",
                    str(out_dir),
                ]
            )
        self.assertEqual(missing_code, 1)

        with contextlib.redirect_stderr(stderr_buf):
            bad_chunk_code = aggregator.main(
                [
                    "--input",
                    str(csv_path),
                    "--output",
                    str(out_dir),
                    "--chunk-size-mb",
                    "0",
                ]
            )
        self.assertEqual(bad_chunk_code, 1)

    # ------------------------------------------------------------------ #
    # 12. C Engine vs Pure Python Engine Exact Equivalence Stress Test
    # ------------------------------------------------------------------ #
    def test_c_and_python_engine_exact_equivalence(self) -> None:
        """Verify 100% identical AggregationResult and CSV outputs between C, Python, and Auto engines on a complex dataset."""
        rows = ["\ufeffcampaign_id,date,impressions,clicks,spend,conversions"]
        for i in range(500):
            cid = f"CMP{(i % 25) + 1:03d}"
            imp = (i * 37) % 10000
            clk = min(imp, (i * 13) % 500)
            spend = f"{(i * 1.37) % 250:.2f}"
            conv = 0 if (i % 7 == 0) else (i % 15)
            rows.append(f" {cid} , 2025-05-{(i % 28) + 1:02d} , {imp} , {clk} , {spend} , {conv} ")
            if i == 150:
                rows.append("campaign_id,date,impressions,clicks,spend,conversions")
            elif i == 250:
                rows.append("CMP_BAD,2025-05-01,100,200,10.00,1")  # clicks > impressions
            elif i == 350:
                rows.append("CMP_BAD,2025-05-01,100,10,NaN,1")  # NaN spend
            elif i == 400:
                rows.append("   ")  # Blank line

        # Intentionally omit trailing newline at EOF and use CRLF
        complex_bytes = "\r\n".join(rows).encode("utf-8")
        csv_path = self._write_bytes("complex_equivalence.csv", complex_bytes)

        py_res = aggregate_file(csv_path, workers=4, chunk_size=64, engine="python")
        c_res = aggregate_file(csv_path, workers=4, chunk_size=64, engine="c")
        auto_res = aggregate_file(csv_path, workers=4, chunk_size=64, engine="auto")

        self.assertEqual(py_res.valid_rows, 500)
        self.assertEqual(py_res.malformed_rows, 2)
        self.assertEqual(c_res.valid_rows, py_res.valid_rows)
        self.assertEqual(c_res.malformed_rows, py_res.malformed_rows)
        self.assertEqual(auto_res.valid_rows, py_res.valid_rows)
        self.assertEqual(auto_res.malformed_rows, py_res.malformed_rows)

        self.assertEqual(set(c_res.campaigns.keys()), set(py_res.campaigns.keys()))
        for cid, py_stats in py_res.campaigns.items():
            c_stats = c_res.campaigns[cid]
            self.assertEqual(c_stats.impressions, py_stats.impressions, f"impressions mismatch on {cid}")
            self.assertEqual(c_stats.clicks, py_stats.clicks, f"clicks mismatch on {cid}")
            self.assertEqual(c_stats.spend_scaled, py_stats.spend_scaled, f"spend_scaled mismatch on {cid}")
            self.assertEqual(c_stats.conversions, py_stats.conversions, f"conversions mismatch on {cid}")

        py_out = self.tmp_path / "equiv_py"
        c_out = self.tmp_path / "equiv_c"
        py_ctr, py_cpa = write_results(py_out, py_res.campaigns)
        c_ctr, c_cpa = write_results(c_out, c_res.campaigns)

        self.assertEqual(py_ctr.read_text(encoding="utf-8"), c_ctr.read_text(encoding="utf-8"))
        self.assertEqual(py_cpa.read_text(encoding="utf-8"), c_cpa.read_text(encoding="utf-8"))

    # ------------------------------------------------------------------ #
    # 13. Model Merge Semantics & Chunk Validation Guards
    # ------------------------------------------------------------------ #
    def test_model_merge_and_chunk_validation_guards(self) -> None:
        """Verify CampaignStats.merge() and calculate_byte_chunks / aggregate_file validation errors."""
        s1 = CampaignStats(impressions=100, clicks=10, spend_scaled=50_000, conversions=2)
        s2 = CampaignStats(impressions=300, clicks=30, spend_scaled=150_000, conversions=6)
        s1.merge(s2)
        self.assertEqual(s1.impressions, 400)
        self.assertEqual(s1.clicks, 40)
        self.assertEqual(s1.total_spend, 20.0)
        self.assertEqual(s1.conversions, 8)
        self.assertEqual(s1.ctr, 0.10)
        self.assertEqual(s1.cpa, 2.50)

        with self.assertRaises(ValueError):
            calculate_byte_chunks(self.tmp_path / "any.csv", chunk_size=0)

        with self.assertRaises(FileNotFoundError):
            calculate_byte_chunks(self.tmp_path / "does_not_exist.csv", chunk_size=1024)


if __name__ == "__main__":
    unittest.main()
