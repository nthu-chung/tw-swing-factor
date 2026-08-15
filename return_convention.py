# -*- coding: utf-8 -*-
"""
報酬口徑（含息 / 不含息）的單一判定入口
======================================

**這個模組存在的理由是一個實測到的假 alpha。**

個股序列在 `PRICE_DATASET=TaiwanStockPriceAdj`(官方還原價)或
`SELF_ADJUST_PRICES=1`(自建還原價,預設開)下是**含息**的 —— `price_adjust` 用
除權息前後參考價的比值回溯,數學上等同「除息日把現金股利再投入」,產出的就是總報酬
序列。而基準長年用 FinMind 的 TAIEX(`TaiwanStockPrice / data_id=TAIEX`),那是
**價格指數,不含息**。兩邊口徑不同 → 差額全部被算成策略的超額報酬。

實測(2026-08-15,FinMind level 2,repo 回測窗 2024-06-03~2026-06-20,495 個交易日,
算術年化慣例與引擎一致):

```
TAIEX 價格指數(舊基準)   年化 42.38%  波動 25.26%  Sharpe 1.677
TAIEX 含息報酬指數        年化 45.23%  波動 25.28%  Sharpe 1.790
                          差 2.86pp/年              差 0.113
```

逐年(2015~2026)差 2.41~4.81pp,**沒有一年為負** —— 系統性偏誤,不是雜訊。
個股側同期樣本(20 檔等權)也證實還原價序列確實含息:還原 61.83% vs 未還原 55.87%
(差 5.96pp/年;剔除 2327 分割污染後仍有 3.52pp/年)。

## 設計原則

1. **判定只有一份**。口徑是從 `config.PRICE_DATASET` + `config.SELF_ADJUST_PRICES`
   推導的,不讓每個研究腳本自己記「我這條序列含不含息」。
2. **fail-closed,不 fallback**。口徑對不上就 raise;含息指數抓不到也 raise,
   **不會**默默退回價格指數。AGENTS.md 的研究紀律是「和基準比,不是和零比」——
   但口徑不一致的比較比不比更糟:它看起來像 alpha,還帶著小數點。
3. **只管報酬比較**。市場濾網/regime(MA200 穿越)與 RS 因子走的是
   `data.fetch_market_index()` 那條價格指數,見下面「已知殘留」。

## 已知殘留（誠實聲明,不是逃生門）

`factor_engine.legacy_factors._attach_relative_strength` 的 `rs_excess` 是
「個股 60 日報酬 − 大盤 60 日報酬」,而注入的大盤是價格指數 → 同一個口徑不一致
存在於因子層,方向是系統性把 `score_rs` 灌高(它是門檻型分數,不是純橫斷面排名,
所以整體平移確實會改變分數)。這裡刻意**不**一起改:因子層一改,每個 panel 都會
需要含息指數(付費層權限),整個 repo 在免費 token 下會無法建 panel。因此把它記進
`summary["return_convention"]["known_residuals"]`,由後續研究決定。
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

import config

# 報酬口徑的兩種值。字串刻意寫死在這裡,summary 欄位的值就是它們。
TOTAL_RETURN = "total_return"      # 含息（股利再投入）
PRICE_RETURN = "price_return"      # 不含息（純價格）

# 基準指數:口徑 → FinMind 資料集（data_id 一律 TAIEX）。
BENCHMARK_INDEX_BY_CONVENTION = {
    TOTAL_RETURN: "TaiwanStockTotalReturnIndex",
    PRICE_RETURN: "TaiwanStockPrice",
}
CONVENTION_BY_BENCHMARK_INDEX = {v: k for k, v in BENCHMARK_INDEX_BY_CONVENTION.items()}

BENCHMARK_DATA_ID = "TAIEX"

# 實測到的偏誤量級（寫進 summary,讓讀結果的人不必回頭翻文件）。
MEASURED_GAP = {
    "window": "2024-06-03~2026-06-20",
    "annualized_pp": 2.86,           # 含息 45.23% − 價格 42.38%
    "sharpe": 0.113,                 # 1.790 − 1.677
    "yearly_gap_pp_range": [2.41, 4.81],
    "note": "2015~2026 逐年含息減價格皆為正;方向是系統性灌高超額報酬",
}


class ReturnConventionMismatch(RuntimeError):
    """個股序列與基準序列的報酬口徑不一致 —— 這種比較不得產出數字。"""


class BenchmarkUnavailable(RuntimeError):
    """口徑正確的基準序列取不到；不得退回另一種口徑冒充。"""


def stock_series_convention(price_dataset: Optional[str] = None,
                            self_adjust: Optional[bool] = None) -> str:
    """個股報酬序列是含息還是不含息。

    含息的兩條路徑:
      - `TaiwanStockPriceAdj`:FinMind 官方還原價（現金股利已還原回價格）。
      - `SELF_ADJUST_PRICES=1`:`price_adjust` 用除權息前後參考價比值自建還原,
        等同除息日股利再投入。
    兩者都不成立 = 純原始價 = 不含息。
    """
    dataset = (price_dataset if price_dataset is not None
               else getattr(config, "PRICE_DATASET", "TaiwanStockPrice"))
    if dataset == "TaiwanStockPriceAdj":
        return TOTAL_RETURN
    self_adj = (self_adjust if self_adjust is not None
                else bool(getattr(config, "SELF_ADJUST_PRICES", False)))
    return TOTAL_RETURN if self_adj else PRICE_RETURN


def benchmark_index_convention(dataset: str) -> str:
    """基準指數資料集的口徑。沒登記過的資料集一律 raise（不猜）。"""
    try:
        return CONVENTION_BY_BENCHMARK_INDEX[dataset]
    except KeyError:
        raise ValueError(
            f"[fail-closed] 不認得基準指數資料集 {dataset!r};"
            f"已登記:{sorted(CONVENTION_BY_BENCHMARK_INDEX)}。"
            "新增基準前必須先聲明它含不含息,否則口徑無從比對"
        ) from None


def resolve_benchmark_dataset(stock_convention: Optional[str] = None,
                              configured: Optional[str] = None) -> str:
    """決定要用哪一個基準指數資料集。

    `configured`(預設讀 `config.BENCHMARK_INDEX_DATASET`)為 "auto" 時由個股
    口徑推導;顯式指定時**照做但立刻驗口徑** —— 顯式選了不一致的基準,是要 raise
    的錯誤,不是可以靜默接受的設定。
    """
    stock_conv = stock_convention or stock_series_convention()
    if stock_conv not in BENCHMARK_INDEX_BY_CONVENTION:
        raise ValueError(f"未知的個股報酬口徑:{stock_conv!r}")
    configured = (configured if configured is not None
                  else getattr(config, "BENCHMARK_INDEX_DATASET", "auto"))
    configured = (configured or "auto").strip()
    if configured == "auto":
        return BENCHMARK_INDEX_BY_CONVENTION[stock_conv]
    bench_conv = benchmark_index_convention(configured)
    assert_consistent(stock_conv, bench_conv, benchmark_dataset=configured,
                      context="config.BENCHMARK_INDEX_DATASET")
    return configured


def assert_consistent(stock_convention: str, benchmark_convention: str, *,
                      benchmark_dataset: str = "", context: str = "") -> None:
    """兩邊口徑不一致就 raise。

    為什麼 fail-closed 而不是警告:實測差 2.86pp/年、Sharpe 0.113,而這正好落在
    「看起來像小 alpha」的量級 —— 印一行 warning 只會被捲過去,數字照樣進報告。
    """
    if stock_convention == benchmark_convention:
        return
    where = f"（{context}）" if context else ""
    raise ReturnConventionMismatch(
        f"[fail-closed] 報酬口徑不一致{where}:個股序列={stock_convention}、"
        f"基準序列={benchmark_convention}"
        + (f"（{benchmark_dataset}）" if benchmark_dataset else "")
        + f"。實測這種比較每年憑空生出 {MEASURED_GAP['annualized_pp']}pp 超額、"
          f"Sharpe 差 {MEASURED_GAP['sharpe']}（{MEASURED_GAP['window']}）。"
          "含息個股序列要配含息報酬指數(TaiwanStockTotalReturnIndex),"
          "不含息個股序列才配價格指數(TaiwanStockPrice/TAIEX)"
    )


def _includes_dividends(convention: str) -> bool:
    return convention == TOTAL_RETURN


def summary_block(price_dataset: Optional[str] = None,
                  self_adjust: Optional[bool] = None,
                  configured: Optional[str] = None) -> dict:
    """`summary["return_convention"]`:兩條序列各自的口徑 + 一致性斷言。

    這是 provenance 欄位,不是可選的裝飾:沒有它,一份「超額報酬 +X%」的結果
    事後無從判斷分子分母是不是同一把尺。口徑不一致時這個函式直接 raise,
    所以任何**存在**的 summary 都保證是同口徑比較。
    """
    stock_conv = stock_series_convention(price_dataset, self_adjust)
    dataset = resolve_benchmark_dataset(stock_conv, configured)
    bench_conv = benchmark_index_convention(dataset)
    assert_consistent(stock_conv, bench_conv, benchmark_dataset=dataset,
                      context="summary")
    configured_value = (configured if configured is not None
                        else getattr(config, "BENCHMARK_INDEX_DATASET", "auto"))
    return {
        "stock_series": {
            "convention": stock_conv,
            "includes_cash_dividends": _includes_dividends(stock_conv),
            "price_dataset": (price_dataset if price_dataset is not None
                              else getattr(config, "PRICE_DATASET", "TaiwanStockPrice")),
            "self_adjust_prices": (bool(self_adjust) if self_adjust is not None
                                   else bool(getattr(config, "SELF_ADJUST_PRICES", False))),
        },
        "benchmark_series": {
            "convention": bench_conv,
            "includes_cash_dividends": _includes_dividends(bench_conv),
            "dataset": dataset,
            "data_id": BENCHMARK_DATA_ID,
            "selection": ("auto_from_stock_series"
                          if str(configured_value).strip() == "auto"
                          else "explicit_config"),
        },
        "consistent": True,          # 不一致的話上面已經 raise,不會有這份 summary
        "measured_gap_if_inconsistent": dict(MEASURED_GAP),
        # 誠實聲明:同一個口徑問題還留在因子層(見模組 docstring)。
        "known_residuals": {
            "relative_strength_factor_benchmark": {
                "where": "factor_engine.legacy_factors._attach_relative_strength",
                "benchmark_dataset": "TaiwanStockPrice",
                "convention": PRICE_RETURN,
                "consistent_with_stock_series": stock_conv == PRICE_RETURN,
                "note": "rs_excess / down_day_excess 仍拿價格指數當基準;"
                        "含息個股序列下 score_rs 被系統性灌高,待後續研究",
            },
            "market_filter_regime_index": {
                "where": "backtest.market_riskoff_map / defensive_rs.market_regime",
                "benchmark_dataset": "TaiwanStockPrice",
                "convention": PRICE_RETURN,
                "note": "MA/波動的 regime 判定是水準值規則,不是報酬比較;"
                        "沿用價格指數是刻意的",
            },
        },
    }


def fetch_benchmark_index(history_days: Optional[int] = None) -> pd.DataFrame:
    """取回**與個股同口徑**的大盤基準序列（欄位 date, close）。

    所有「和大盤比」的研究路徑都要走這裡,不要直接呼叫 `data.fetch_market_index()`
    —— 那條是價格指數,和含息個股序列不同尺。

    抓不到就 raise(`BenchmarkUnavailable`):靜默退回價格指數正是這次要修的 bug。
    含息指數需要 FinMind level 2 權限,免費層會在資料層先 fail-closed。
    """
    import data      # 延遲 import:資料層會 import config,避免模組載入期繞圈

    stock_conv = stock_series_convention()
    dataset = resolve_benchmark_dataset(stock_conv)
    bench_conv = benchmark_index_convention(dataset)
    assert_consistent(stock_conv, bench_conv, benchmark_dataset=dataset,
                      context="fetch_benchmark_index")
    if dataset == "TaiwanStockTotalReturnIndex":
        df = data.fetch_market_total_return_index(history_days)
    else:
        df = data.fetch_market_index(history_days)
    if df is None or df.empty or "close" not in getattr(df, "columns", []):
        raise BenchmarkUnavailable(
            f"[fail-closed] 取不到 {dataset}({BENCHMARK_DATA_ID}) 基準序列;"
            "拒絕改用另一種口徑的指數頂替(那會讓比較結果多出"
            f"{MEASURED_GAP['annualized_pp']}pp/年的假超額)"
        )
    out = df[["date", "close"]].copy()
    out["date"] = pd.to_datetime(out["date"])
    out = out.sort_values("date").reset_index(drop=True)
    # attrs 跟著序列走:下游把它併進報表時,口徑不必再猜一次。
    out.attrs["return_convention"] = bench_conv
    out.attrs["benchmark_dataset"] = dataset
    out.attrs["benchmark_data_id"] = BENCHMARK_DATA_ID
    return out
