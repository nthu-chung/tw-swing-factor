# -*- coding: utf-8 -*-
"""
選股引擎
========
對 universe 中每檔股票：
  1. 抓資料 -> 算因子（factors.compute_factors）
  2. 取「市場參考交易日」那一列（見下方 reference day 說明）
  3. pre-filter：流動性、趨勢保護硬門檻
  4. 綜合評分（factors.composite_score）
  5. 依分數排序，輸出前 TOP_N

輸出：
  - DataFrame（給程式 / 回測用）
  - CSV（outputs/selection_YYYYMMDD.csv）
  - 易讀文字報告（print + outputs/selection_YYYYMMDD.txt）

市場參考交易日（2026-08-15 修的 bug,P2）
----------------------------------------
舊版每檔股票各自取「自己最後一根 <= as_of 的 bar」當今天:
`sub = f[f["date"] <= as_of_ts]; row = sub.iloc[-1]`(`as_of=None` 時是
`f.iloc[-1]`)。實測 AAAA 的資料在 2026-04-01 就斷、BBBB 到 2026-05-20,
`screen(as_of="2026-05-20")` 兩檔都會回傳,而 AAAA 那列的 `date` 是
**2026-04-01** —— 一檔停牌/下市 7 週的股票混在「今日候選」裡,分數還是用
兩個月前的收盤價算的,照著下單根本買不到。同一份資料丟
`dynamic_universe.add_membership` 只會回 `['BBBB']`,因為那邊要求「當日必須有
一根 valid bar」。

現在的規則:
  1. 先用大盤(TAIEX)序列當市場行事曆,解出參考交易日 —— `--date` 落在假日
     就退到「最近一個有效交易日」;沒給 `--date` 就是市場最後一個交易日。
  2. 每檔股票必須在**那一天**有 bar 才算候選(不是 <= 那一天)。
  3. 只有更舊 bar 的(停牌/暫停交易/已下市)排除並標 `stale_bar`,計入
     診斷資訊,不冒充當日候選。
  4. 連那天之前都沒有資料的(尚未上市)記 `no_asof`。

這條規則與 `dynamic_universe.add_membership` 語意一致但**目前是第三份獨立
實作**(screener / live / dynamic_universe 各一份)。收斂成同一份成員規則屬於
更深的重構,尚未做;改動任何一份時請三份一起看。
"""

from __future__ import annotations

from typing import List, Optional

import pandas as pd

import config
import data
import factors
import universe as uni


# 參考日取列的三種結果。`ok` 以外都不得進候選名單。
REFERENCE_OK = "ok"
REFERENCE_STALE_BAR = "stale_bar"     # 停牌/下市:只有更舊的 bar
REFERENCE_NO_ASOF = "no_asof"         # 參考日之前完全沒資料(尚未上市)


def market_trading_days(market: Optional[pd.DataFrame]) -> pd.DatetimeIndex:
    """從大盤序列取出市場交易日曆(去重、排序)。"""
    if market is None or len(market) == 0 or "date" not in market.columns:
        return pd.DatetimeIndex([])
    days = pd.to_datetime(market["date"], errors="coerce").dropna()
    return pd.DatetimeIndex(sorted(set(days)))


def resolve_reference_trading_day(market: Optional[pd.DataFrame],
                                  as_of: Optional[str] = None) -> pd.Timestamp:
    """解出這次選股的市場參考交易日。

    `as_of=None` → 市場最後一個交易日;給了 `as_of` → 最近一個 <= 它的交易日
    (所以 `--date` 給週末或國定假日會退到前一個有效交易日)。

    沒有市場行事曆時 **fail-closed raise**:沒有行事曆就無法判斷「當日」,
    退回「每檔自己的最後一根 bar」正是這個模組原本的 bug。
    """
    days = market_trading_days(market)
    if len(days) == 0:
        raise RuntimeError(
            "[fail-closed] screener 取不到大盤(TAIEX)交易日曆,無法決定市場參考"
            "交易日。不可退回「每檔股票自己的最後一根 bar」—— 那會讓停牌股用幾"
            "個月前的價格冒充今日候選。"
        )
    if as_of is None:
        return days[-1]
    target = pd.to_datetime(as_of)
    usable = days[days <= target]
    if len(usable) == 0:
        raise ValueError(
            f"[fail-closed] --date {as_of} 早於市場行事曆起點 "
            f"{days[0].date()},找不到任何有效交易日"
        )
    return usable[-1]


def reference_bar(frame: Optional[pd.DataFrame], ref_day: pd.Timestamp):
    """取「參考交易日那一根」,回傳 `(row, status, last_bar)`。

    `status` 為 `ok` 時 `row` 是那一列;否則 `row` 是 `None`,呼叫端必須排除
    該股票。`last_bar` 只在 `stale_bar` 時有意義(最後一根舊 bar 的日期),
    用來讓診斷資訊看得出停多久。

    **刻意不做 as-of 回填**:`<= ref_day` 的最後一列在停牌股上就是幾週前的
    收盤,拿它算出來的分數不是當日可交易的訊號。
    """
    if frame is None or len(frame) == 0 or "date" not in frame.columns:
        return None, REFERENCE_NO_ASOF, None
    dates = pd.to_datetime(frame["date"], errors="coerce")
    exact = frame[dates == ref_day]
    if len(exact):
        return exact.iloc[-1], REFERENCE_OK, ref_day
    older = dates[dates < ref_day]
    if len(older) == 0:
        return None, REFERENCE_NO_ASOF, None
    return None, REFERENCE_STALE_BAR, pd.Timestamp(older.max())


def _build_reasons(row) -> str:
    """根據各因子分數，組出人看得懂的「選股理由」。"""
    tags = []
    if row.get("score_inst_mid", 0) >= 0.5 or row.get("score_inst_long", 0) >= 0.5:
        tags.append("法人持續買超")
    if row.get("score_inst_dip_buy", 0) >= 0.5:
        tags.append("跌時法人仍買(洗盤)")
    if row.get("score_bb_pullback", 0) >= 0.6:
        tags.append("回檔到月線買點")
    if row.get("score_ma_alignment", 0) >= 1.0:
        tags.append("均線多頭排列")
    if row.get("score_ma_squeeze", 0) >= 0.6:
        tags.append("均線糾結蓄勢")
    if row.get("score_vol_dryup", 0) >= 0.8:
        tags.append("量縮窒息")
    if row.get("score_margin_health", 0) >= 0.8:
        tags.append("資券結構健康")
    return "、".join(tags) if tags else "—"


def screen(
    symbols: Optional[List[str]] = None,
    as_of: Optional[str] = None,
    sample: bool = True,
    pool: Optional[int] = None,
    verbose: bool = True,
) -> pd.DataFrame:
    """
    執行選股。
    - symbols：指定股票清單；None 則用 universe。
    - as_of：指定選股日 'YYYY-MM-DD'；None = 市場最後一個交易日。非交易日會退到
      最近一個有效交易日（見模組 docstring 的「市場參考交易日」）。
    - pool：用「成交值前 N 大」池（需先跑 build_universe.py N），優先於 sample。
    - sample：pool 未指定時，universe 用小集合（True）或全市場（False）。

    回傳的 DataFrame 帶 `attrs["screen_diagnostics"]`：參考交易日、市場最後交易日、
    被排除的 `stale_bar` 清單與各項略過計數。停牌股不會出現在候選裡，但**看得見**
    它為什麼被排除。
    """
    dynamic_enabled = config.DYNAMIC_UNIVERSE_ENABLED and (pool is not None or not sample)
    if symbols is None:
        if pool:
            if dynamic_enabled:
                symbols = uni.get_research_candidates(
                    universe_top_n=config.DYNAMIC_UNIVERSE_TOP_N,
                    candidate_pool_n=pool,
                )
            else:
                symbols = uni.get_universe(top_n=pool)
        else:
            symbols = uni.get_universe(sample=sample)
    name_map = uni.get_name_map()
    industry_map = uni.get_industry_map()

    # 大盤基準（RS / 抗跌因子用），只抓一次，注入每檔 bundle。
    # 它同時也是**市場行事曆**：參考交易日先從這裡解出來，再要求每檔股票在那天
    # 有 bar；不是讓每檔各自決定自己的「今天」。
    market = data.fetch_market_index()
    ref_day = resolve_reference_trading_day(market, as_of)
    market_days = market_trading_days(market)

    rows = []
    stale_bars: List[dict] = []
    skipped = {"no_data": 0, "liquidity": 0, "trend": 0, "no_asof": 0,
               "excluded": 0, "stale_bar": 0}

    for sid in symbols:
        # pre-filter：排除金融 / ETF / 非普通股（任何 universe 模式都套用）
        industry = industry_map.get(sid, "")
        if config.EXCLUDE_FINANCE and ("金融" in industry or "保險" in industry):
            skipped["excluded"] += 1
            continue
        if "ETF" in industry or "ETN" in industry or sid.startswith("00"):
            skipped["excluded"] += 1
            continue

        bundle = data.fetch_bundle(sid)
        bundle["market"] = market
        price = bundle.get("price")
        if price is None or price.empty:
            skipped["no_data"] += 1
            continue

        # pre-filter：流動性。只看**截至參考交易日**的 20 根;`price.tail(20)` 在
        # `--date` 回看模式下拿的是資料末端的量,等於用未來人氣決定當時能不能選。
        if not dynamic_enabled:
            causal_price = price[pd.to_datetime(price["date"]) <= ref_day]
            if not uni.passes_liquidity(causal_price):
                skipped["liquidity"] += 1
                continue

        f = factors.compute_factors(bundle)
        if f.empty:
            skipped["no_data"] += 1
            continue

        # 取市場參考交易日那一根；只有更舊 bar 的停牌股在這裡被擋掉。
        row, status, last_bar = reference_bar(f, ref_day)
        if row is None:
            skipped[status] += 1
            if status == REFERENCE_STALE_BAR:
                stale_bars.append({
                    "stock_id": sid,
                    "name": name_map.get(sid, ""),
                    "last_bar": last_bar.strftime("%Y-%m-%d"),
                })
            continue

        hist = f[f["date"] <= row["date"]].tail(config.DYNAMIC_UNIVERSE_LOOKBACK)
        valid_hist = hist[
            (hist["close"] > 0) & (hist["volume"] > 0) & (hist["turnover"] > 0)
        ]

        score = factors.composite_score(row)
        rows.append({
            "stock_id": sid,
            "name": name_map.get(sid, ""),
            "industry": industry_map.get(sid, ""),
            "date": row["date"].strftime("%Y-%m-%d"),
            "close": round(float(row["close"]), 2),
            "composite": score,
            "trend_ok": bool(row.get("trend_ok", False)),
            "universe_obs": len(valid_hist),
            "universe_avg_turnover": (
                float(valid_hist["turnover"].mean()) if len(valid_hist) else float("nan")
            ),
            "universe_avg_volume_lots": (
                float(valid_hist["volume"].mean() / 1000.0)
                if len(valid_hist) else float("nan")
            ),
            "inst_6d": round(float(row.get("inst_6d", 0) or 0), 3),
            "inst_12d": round(float(row.get("inst_12d", 0) or 0), 3),
            "bb_pos": round(float(row.get("bb_pos", 0) or 0), 3),
            "near_high": round(float(row.get("near_high", 0) or 0), 3),
            "vol_ratio": round(float(row.get("vol_ratio", 0) or 0), 3),
            "margin_short_ratio": round(float(row.get("margin_short_ratio", 0) or 0), 1),
            # 保留各因子分數供報告
            "score_inst_mid": round(float(row.get("score_inst_mid", 0)), 3),
            "score_inst_long": round(float(row.get("score_inst_long", 0)), 3),
            "score_inst_dip_buy": round(float(row.get("score_inst_dip_buy", 0)), 3),
            "score_margin_health": round(float(row.get("score_margin_health", 0)), 3),
            "score_ma_alignment": round(float(row.get("score_ma_alignment", 0)), 3),
            "score_bb_pullback": round(float(row.get("score_bb_pullback", 0)), 3),
            "score_ma_squeeze": round(float(row.get("score_ma_squeeze", 0)), 3),
            "score_vol_dryup": round(float(row.get("score_vol_dryup", 0)), 3),
        })

    diagnostics = {
        "requested_date": as_of,
        "reference_trading_day": ref_day.strftime("%Y-%m-%d"),
        "market_last_trading_day": market_days[-1].strftime("%Y-%m-%d"),
        "stale_bar": stale_bars,
        "skipped": skipped,
    }

    if not rows:
        if verbose:
            print(f"[screen] 無符合條件標的。參考交易日={diagnostics['reference_trading_day']}"
                  f" skipped={skipped}")
        empty = pd.DataFrame()
        empty.attrs["screen_diagnostics"] = diagnostics
        return empty

    df = pd.DataFrame(rows)
    if dynamic_enabled:
        eligible = (
            (df["universe_obs"] >= config.DYNAMIC_UNIVERSE_MIN_OBS)
            & (df["universe_avg_volume_lots"]
               >= config.DYNAMIC_UNIVERSE_MIN_AVG_VOLUME_LOTS)
            & (df["universe_avg_turnover"]
               >= config.DYNAMIC_UNIVERSE_MIN_AVG_TURNOVER)
        )
        skipped["liquidity"] += int((~eligible).sum())
        df = (
            df[eligible]
            .sort_values(["universe_avg_turnover", "stock_id"],
                         ascending=[False, True])
            .head(config.DYNAMIC_UNIVERSE_TOP_N)
            .copy()
        )
        df["universe_rank"] = range(1, len(df) + 1)

    if config.TREND_GUARD_ENABLED:
        failed_trend = ~df["trend_ok"]
        skipped["trend"] += int(failed_trend.sum())
        df = df[~failed_trend].copy()

    df = df.sort_values("composite", ascending=False).reset_index(drop=True)
    df["reason"] = df.apply(_build_reasons, axis=1)

    # 門檻過濾 + 取前 N
    selected = df[df["composite"] >= config.MIN_COMPOSITE].head(config.TOP_N).reset_index(drop=True)

    selected.attrs["screen_diagnostics"] = diagnostics
    df.attrs["screen_diagnostics"] = diagnostics

    if verbose:
        _print_report(df, selected, diagnostics)
        _save_outputs(df, selected, ref_day)

    return selected


def _print_report(full: pd.DataFrame, selected: pd.DataFrame, diagnostics: dict):
    skipped = diagnostics["skipped"]
    day = diagnostics["reference_trading_day"]
    requested = diagnostics.get("requested_date")
    asked = f"（指定 {requested}）" if requested and requested != day else ""
    print("=" * 72)
    print(f"  台股波段多因子選股  |  市場參考交易日：{day}{asked}")
    print("=" * 72)
    print(f"  掃描 {len(full)} 檔通過pre-filter；"
          f"略過：排除(金融/ETF) {skipped.get('excluded', 0)}、無資料 {skipped['no_data']}、"
          f"流動性 {skipped['liquidity']}、趨勢 {skipped['trend']}、"
          f"參考日無 bar(停牌/下市) {skipped.get('stale_bar', 0)}、"
          f"尚無資料 {skipped.get('no_asof', 0)}")
    stale = diagnostics.get("stale_bar") or []
    if stale:
        preview = "、".join(f"{s['stock_id']}(最後 {s['last_bar']})" for s in stale[:5])
        more = f" 等 {len(stale)} 檔" if len(stale) > 5 else ""
        print(f"  參考日無 bar 而排除：{preview}{more}")
    print(f"  綜合分數 >= {config.MIN_COMPOSITE} 的標的：{len(selected)} 檔")
    print("-" * 72)
    if selected.empty:
        print("  （今日無達標標的）")
        # 仍列出分數最高前5供參考
        print("\n  分數最高前5（未達門檻，僅參考）：")
        for _, r in full.head(5).iterrows():
            print(f"    {r['stock_id']} {r['name']:<6} 分數 {r['composite']:>5}  {r['reason']}")
    else:
        for i, r in selected.iterrows():
            print(f"  {i+1:>2}. {r['stock_id']} {r['name']:<7} "
                  f"分數 {r['composite']:>5}  收 {r['close']:>7}  "
                  f"法人6d {r['inst_6d']:>6}  布林 {r['bb_pos']:>6}")
            print(f"       理由：{r['reason']}")
    print("=" * 72)


def _save_outputs(full: pd.DataFrame, selected: pd.DataFrame, ref_day: pd.Timestamp):
    # 檔名戳的是**市場參考交易日**,不是 `datetime.now()`:週末或盤前跑會產生
    # 一個以非交易日命名的檔案,之後沒人分得出它是哪一天的選股結果。
    stamp = ref_day.strftime("%Y%m%d")
    csv_path = config.OUTPUT_DIR / f"selection_{stamp}.csv"
    full.to_csv(csv_path, index=False, encoding="utf-8-sig")
    print(f"  完整評分已存：{csv_path}")


if __name__ == "__main__":
    screen(sample=True, verbose=True)
