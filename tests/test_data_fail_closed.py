# -*- coding: utf-8 -*-
"""資料/API 失敗不得靜默變成空資料或未還原價。"""

from __future__ import annotations

import unittest
from unittest import mock
from pathlib import Path
import tempfile

import pandas as pd
import requests

import config
import backtest
import data
import universe


class _Response:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {"status": 200, "data": []}

    def raise_for_status(self):
        if self.status_code >= 400:
            err = requests.HTTPError(f"HTTP {self.status_code}")
            err.response = self
            raise err

    def json(self):
        return self._payload


class DataFailClosedTest(unittest.TestCase):
    def test_missing_token_raises_instead_of_empty_frame(self):
        with mock.patch.object(config, "FINMIND_TOKEN", ""):
            with self.assertRaises(data.FinMindAPIError):
                data.fetch_finmind_dataset("TaiwanStockPrice", "2330", "2025-01-01", "2025-02-01")

    def test_transport_failure_retries_then_raises(self):
        with (
            mock.patch.object(config, "FINMIND_TOKEN", "secret"),
            mock.patch.object(config, "FINMIND_MAX_RETRIES", 3),
            mock.patch.object(config, "FINMIND_SLEEP", 0),
            mock.patch.object(config, "FINMIND_RETRY_BACKOFF", 0),
            mock.patch.object(data._SESSION, "get",
                              side_effect=requests.ConnectionError()) as get,
        ):
            with self.assertRaisesRegex(data.FinMindAPIError, "拒絕回空表"):
                data.fetch_finmind_dataset("TaiwanStockPrice", "2330", "2025-01-01", "2025-02-01")
            self.assertEqual(get.call_count, 3)

    def test_auth_or_quota_4xx_does_not_retry(self):
        with (
            mock.patch.object(config, "FINMIND_TOKEN", "secret"),
            mock.patch.object(config, "FINMIND_SLEEP", 0),
            mock.patch.object(data._SESSION, "get", return_value=_Response(status=402)) as get,
        ):
            with self.assertRaisesRegex(data.FinMindAPIError, "HTTP 402"):
                data.fetch_finmind_dataset("TaiwanStockPrice", "2330", "2025-01-01", "2025-02-01")
            self.assertEqual(get.call_count, 1)

    def test_bad_snapshot_never_falls_back_to_today(self):
        with mock.patch.object(config, "SNAPSHOT_END_DATE", "2025/01/01"):
            with self.assertRaisesRegex(ValueError, "拒絕退回 now"):
                data._date_range()

    def test_self_adjust_failure_never_returns_raw_prices(self):
        raw = pd.DataFrame({"date": [pd.Timestamp("2025-01-01")], "close": [100.0]})
        with (
            mock.patch.object(config, "SELF_ADJUST_PRICES", True),
            mock.patch("price_adjust.adjust_price_frame", side_effect=RuntimeError("boom")),
        ):
            with self.assertRaisesRegex(RuntimeError, "拒絕退回未還原價"):
                data._maybe_self_adjust("2330", raw, "TaiwanStockPrice")

    def test_price_limit_schema_missing_columns_fails_closed(self):
        bad = pd.DataFrame({"date": ["2025-01-02"], "reference_price": [100.0]})
        with (
            mock.patch.object(data, "_load_cache", return_value=None),
            mock.patch.object(data, "_finmind_get", return_value=bad),
            mock.patch.object(data, "_save_cache"),
        ):
            with self.assertRaisesRegex(data.FinMindAPIError, "schema 缺少"):
                data.fetch_price_limits("2330")

    def test_price_limit_columns_are_normalized(self):
        raw = pd.DataFrame({
            "date": ["2025-01-02"], "stock_id": ["2330"],
            "reference_price": ["100"], "limit_up": ["110"],
            "limit_down": ["90"],
        })
        with (
            mock.patch.object(data, "_load_cache", return_value=None),
            mock.patch.object(data, "_finmind_get", return_value=raw),
            mock.patch.object(data, "_save_cache"),
        ):
            out = data.fetch_price_limits("2330")
        self.assertEqual(
            list(out.columns), ["date", "reference_price", "limit_up", "limit_down"])
        self.assertTrue(pd.api.types.is_datetime64_any_dtype(out["date"]))


class UniverseFailClosedTest(unittest.TestCase):
    def test_missing_named_pool_never_falls_back_to_sample(self):
        with mock.patch("build_universe.load", return_value=[]):
            with self.assertRaises(FileNotFoundError):
                universe.get_universe(top_n=300)

    def test_full_market_failure_never_falls_back_to_sample(self):
        with mock.patch.object(universe.data, "fetch_stock_info", return_value=pd.DataFrame()):
            with self.assertRaises(RuntimeError):
                universe.get_universe(sample=False)


class ExecutionDataFailClosedTest(unittest.TestCase):
    def test_enabled_disposition_model_requires_both_market_caches(self):
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.object(config, "BT_MODEL_DISPOSITION", True),
                mock.patch.object(config, "CACHE_DIR", Path(tmp)),
            ):
                with self.assertRaisesRegex(RuntimeError, "無處置快取"):
                    backtest._load_disposition_days(pd.bdate_range("2025-01-01", periods=5))


if __name__ == "__main__":
    unittest.main()
