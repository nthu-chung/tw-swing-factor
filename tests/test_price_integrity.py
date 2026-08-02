from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

import price_integrity
import rotation_research


def _prices(closes, opens=None, stock_id="2327", start="2025-08-20"):
    opens = closes if opens is None else opens
    return pd.DataFrame(
        {
            "stock_id": stock_id,
            "date": pd.date_range(start, periods=len(closes), freq="B"),
            "open": opens,
            "close": closes,
        }
    )


class PriceIntegrityTest(unittest.TestCase):
    def test_normal_taiwan_limit_moves_are_not_flagged(self):
        prices = _prices(
            closes=[100.0, 109.5, 99.0, 104.0],
            opens=[100.0, 108.0, 100.0, 102.0],
        )
        audit = price_integrity.detect_price_discontinuities(prices)
        self.assertTrue(audit.empty)
        self.assertEqual(list(audit.columns), price_integrity.AUDIT_COLUMNS)

    def test_yageo_like_discontinuity_is_flagged(self):
        prices = _prices(
            closes=[546.0, 143.0],
            opens=[546.0, 143.0],
        )
        audit = price_integrity.detect_price_discontinuities(prices)
        self.assertEqual(len(audit), 1)
        row = audit.iloc[0]
        self.assertEqual(row["stock_id"], "2327")
        self.assertEqual(row["previous_close"], 546.0)
        self.assertEqual(row["open"], 143.0)
        self.assertEqual(row["close"], 143.0)
        self.assertLess(row["overnight_return"], -0.70)
        self.assertLess(row["close_return"], -0.70)

    def test_future_rows_do_not_change_past_audit(self):
        base = _prices(
            closes=[100.0, 130.0, 128.0],
            opens=[100.0, 130.0, 129.0],
            stock_id="A",
        )
        extended = pd.concat(
            [
                base,
                pd.DataFrame(
                    {
                        "stock_id": ["A"],
                        "date": [pd.Timestamp("2030-01-02")],
                        "open": [10.0],
                        "close": [10.0],
                    }
                ),
            ],
            ignore_index=True,
        )
        before = price_integrity.detect_price_discontinuities(base)
        after = price_integrity.detect_price_discontinuities(extended)
        past = after[after["date"] <= base["date"].max()].reset_index(drop=True)
        pd.testing.assert_frame_equal(before, past)

    def test_guard_blocks_raw_but_not_adjusted_dataset(self):
        audit = price_integrity.detect_price_discontinuities(
            _prices([546.0, 143.0], [546.0, 143.0])
        )
        self.assertTrue(
            price_integrity.should_block_unadjusted_backtest(
                "TaiwanStockPrice", audit
            )
        )
        self.assertFalse(
            price_integrity.should_block_unadjusted_backtest(
                "TaiwanStockPriceAdj", audit
            )
        )

    def test_empty_audit_does_not_certify_unadjusted_prices(self):
        """回歸鎖:審計為空 != 價格乾淨。

        除息缺口約 3~5%，在 ±10% 漲跌停帶內，掃描結構上看不到。舊邏輯用
        「掃描沒命中」當放行條件，等於把盲區當成清白證明。
        """
        # 連續 4% 的隔夜負跳空（典型現金股息除息幅度）——掃描完全看不到。
        dividend_like = _prices(
            closes=[100.0, 96.0, 96.0, 92.16],
            opens=[100.0, 96.0, 96.0, 92.16],
        )
        audit = price_integrity.detect_price_discontinuities(dividend_like)
        self.assertTrue(audit.empty, "4% 缺口本來就在門檻盲區內")

        # 盲區沒訊號 → 仍必須擋下未還原價。
        self.assertTrue(
            price_integrity.should_block_unadjusted_backtest(
                "TaiwanStockPrice", audit
            )
        )
        # 連 audit 都沒傳（純看資料集）也要擋。
        self.assertTrue(
            price_integrity.should_block_unadjusted_backtest("TaiwanStockPrice")
        )
        # 還原價才放行。
        self.assertFalse(
            price_integrity.should_block_unadjusted_backtest(
                "TaiwanStockPriceAdj", audit
            )
        )

    def test_threshold_cannot_be_lowered_without_flagging_real_moves(self):
        """把門檻壓到漲跌停以下,真實漲停會被誤判 —— 所以門檻不是解法。"""
        limit_up = _prices(closes=[100.0, 109.5], opens=[100.0, 108.0])
        # 9%(低於漲跌停)→ 真實漲停被誤標
        self.assertFalse(
            price_integrity.detect_price_discontinuities(
                limit_up, threshold=0.09
            ).empty
        )
        # 11%(高於漲跌停)→ 真實漲停不被誤標,但除息缺口也照樣看不到
        self.assertTrue(
            price_integrity.detect_price_discontinuities(
                limit_up, threshold=0.11
            ).empty
        )

    def test_main_writes_audit_and_stops_before_evaluation(self):
        raw_frame = _prices(
            closes=[546.0, 143.0],
            opens=[546.0, 143.0],
        ).drop(columns="stock_id")
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            with (
                mock.patch.object(rotation_research.uni, "get_universe", return_value=["2327"]),
                mock.patch.object(
                    rotation_research,
                    "_price_cache",
                    return_value={"2327": raw_frame},
                ),
                mock.patch.object(
                    rotation_research,
                    "build_research_panel",
                ) as build_panel,
                mock.patch.object(rotation_research, "evaluate") as evaluate,
                mock.patch.object(rotation_research.config, "OUTPUT_DIR", output_dir),
                mock.patch.object(
                    rotation_research.config,
                    "PRICE_DATASET",
                    "TaiwanStockPrice",
                ),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "adjusted price.*PIT",
                ):
                    rotation_research.main()
            build_panel.assert_not_called()
            evaluate.assert_not_called()
            audit_path = output_dir / "price_integrity_audit.csv"
            self.assertTrue(audit_path.exists())
            audit = pd.read_csv(audit_path)
            self.assertEqual(audit.loc[0, "stock_id"], 2327)
            self.assertLess(audit.loc[0, "close_return"], -0.70)


if __name__ == "__main__":
    unittest.main()
