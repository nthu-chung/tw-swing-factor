# -*- coding: utf-8 -*-
"""價格尺度契約的 golden test(見 `PRICE_SCALE_CONTRACT.md`)。

這些測試**釘住已量測到的供應商行為事實**,不是斷言尚未實作的理想行為 ——
斷言理想行為只會讓測試紅著進版控,擋住所有人。它們的價值在於:FinMind 若改變
還原口徑(改成後復權、或開始調整成交量),這裡會紅,而我們會在把假數字寫進報告
**之前**知道。

fixture 凍結於 2026-08-15、全離線(`tests/fixtures/finmind_price_scale_*.json`)。

被釘住的四個事實:
  1. `TaiwanStockPriceAdj` 是**前復權**(qfq):錨在資料集最新一根 bar。
  2. 還原檔的**量與成交金額完全未調整**(LSEG 分類上這叫 RPO:只調價不調量)。
  3. 因此 `vwap = turnover / volume` 落在**原始價尺度**,與同列的 `close_adj`
     不同尺度 —— 直接相比會差一個因子。
  4. 因子的**絕對值**含該日之後所有事件;因子的**跳幅**才是那一次事件的比例。
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

import pandas as pd

FIXTURE = (Path(__file__).parent / "fixtures"
           / "finmind_price_scale_2026-08-15.json")
# TWSE TWTB8U 停止買賣恢復參考價:2327 國巨 546.00 → 136.50
OFFICIAL_2327_RATIO = 546.00 / 136.50          # = 4.0000


def _frame(key: str) -> pd.DataFrame:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    df = pd.DataFrame(data[key])
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


class AdjustedDatasetPropertiesTest(unittest.TestCase):
    """FinMind 還原資料集的性質(決定我們該存什麼、執行層該讀什麼)。"""

    def test_adjustment_is_anchored_at_the_latest_bar(self):
        """前復權:錨在最新一根 bar,所以**歷史會被新事件重寫**。

        這是 `PRICE_SCALE_CONTRACT.md` §1 的依據,也是「不要把還原值存進快取」
        這個決定的理由:錨會動,存下來的值不是 PIT 不變量。
        (機構庫同樣錨在最後交易日 —— CRSP 官方定義如此 —— 差別在於它們存的是
        原始價 + 帶版本的因子表,還原只是查詢時的 view。)
        """
        for sid in ("2327", "2317"):
            raw, adj = _frame(f"{sid}_raw"), _frame(f"{sid}_adj")
            merged = raw.merge(adj, on="date", suffixes=("", "_adj"))
            last = merged.iloc[-1]
            # fixture 的視窗末端不是資料集末端,所以只驗「愈接近末端、因子愈趨近 1」
            # 這個方向性;錨點本身的驗證見 PRICE_SCALE_CONTRACT.md 的實測紀錄。
            first_factor = merged.iloc[0]["close"] / merged.iloc[0]["close_adj"]
            last_factor = last["close"] / last["close_adj"]
            self.assertLessEqual(
                abs(last_factor - 1.0), abs(first_factor - 1.0) + 1e-9,
                f"{sid}:因子應隨時間趨近 1(錨在較新的 bar)")

    def test_volume_and_turnover_are_not_adjusted(self):
        """還原檔只調價、不調量 —— LSEG 分類上這是 RPO 模式。

        後果:任何用到 volume / turnover 的因子(vwap、amihud、dollar_volume)
        在還原檔裡是未定義行為,除非明確做尺度轉換。
        """
        for sid in ("2327", "2317"):
            raw, adj = _frame(f"{sid}_raw"), _frame(f"{sid}_adj")
            m = raw.merge(adj, on="date", suffixes=("", "_adj"))
            pd.testing.assert_series_equal(
                m["Trading_Volume"], m["Trading_Volume_adj"],
                check_names=False)
            pd.testing.assert_series_equal(
                m["Trading_money"], m["Trading_money_adj"], check_names=False)

    def test_vwap_from_the_adjusted_frame_is_on_the_raw_scale(self):
        """`turnover / volume` 是原始價尺度,不可與同列的 `close_adj` 直接比。

        實測 2327 2025-08-13:vwap = 546.50,同列 close_adj = 135.53,差 4.03 倍。
        `factor_engine/data_fields.py` 正是用 turnover/volume 算 vwap,
        所以切到還原價之後 `close/vwap - 1` 這類因子會直接變成 -75%。
        """
        adj = _frame("2327_adj")
        pre = adj[adj["date"] <= "2025-08-13"]
        vwap = pre["Trading_money"] / pre["Trading_Volume"]
        ratio = (vwap / pre["close"]).median()
        self.assertGreater(ratio, 3.9,
                           "分割前的 vwap 應該仍在原始價尺度(約 4 倍於還原價)")

    def test_factor_step_equals_the_official_event_ratio(self):
        """因子的**跳幅**才是事件比例;絕對值含該日之後所有事件。

        2327:分割前隱含因子 4.028701、分割後 1.007175,兩者相除 = 4.000000,
        正好等於 TWSE TWTB8U 的 546.00 / 136.50。殘留的 1.007175 是分割後到
        錨點之間累積的現金股利。這條讓「因子跳幅要與事件內容相符」成為可實作
        的檢查(比對相鄰兩日因子的比值,不是比對因子本身)。
        """
        raw, adj = _frame("2327_raw"), _frame("2327_adj")
        m = raw.merge(adj, on="date", suffixes=("", "_adj"))
        m["factor"] = m["close"] / m["close_adj"]
        before = m[m["date"] <= "2025-08-13"]["factor"].median()
        after = m[m["date"] >= "2025-08-25"]["factor"].median()
        self.assertAlmostEqual(before / after, OFFICIAL_2327_RATIO, places=4)
        self.assertGreater(before, after,
                           "分割會讓分割前的因子高於分割後(股數變多、價格變低)")

    def test_split_shows_up_as_a_fake_crash_in_raw_but_not_in_adjusted(self):
        """同一件事在兩條序列上的樣子:raw 是 -73.8% 假崩盤,adj 是真實報酬。"""
        raw, adj = _frame("2327_raw"), _frame("2327_adj")
        raw_ret = raw["close"].pct_change().min()
        adj_ret = adj["close"].pct_change().min()
        self.assertLess(raw_ret, -0.70, "未還原序列有一根 -70% 以上的假跳空")
        self.assertGreater(adj_ret, -0.10, "還原序列不應有那根假跳空")


if __name__ == "__main__":
    unittest.main()
