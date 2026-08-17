# -*- coding: utf-8 -*-
"""
傳統多因子計算模組
==============
輸入單檔股票的 bundle（price / inst / margin），輸出一個對齊到「交易日」的
DataFrame，每一列是某一天、每一欄是一個因子值或標準化分數。

為什麼要算「每一天」？
  回測需要在每個歷史日取得「當時」的因子值。只算最後一天無法回測。

防未來函數（point-in-time）：
  - 法人 / 融資資料用 merge_asof 對齊到價格日，且只會用「<= 當日」的最近一筆。
  - 所有 rolling 計算都是因果的（只看過去），pandas rolling 預設即如此。
  - 訊號在第 T 日收盤後產生，回測在 T+1 開盤進場（見 backtest.py）。

每個因子提供兩種輸出：
  - 原始值欄位（給回測 IC 分析、給人看）
  - *_score 欄位：0~1 標準化分數（給多因子加權評分）
"""

from __future__ import annotations

import numpy as np
import pandas as pd

import config


# ── 小工具：把任意值壓到 0~1 ────────────────────────────────────────────
def _clip01(x):
    return float(min(1.0, max(0.0, x)))


def _scale(value, lo, hi):
    """線性映射 [lo,hi] -> [0,1]，超出範圍夾住。"""
    if hi == lo:
        return 0.0
    return _clip01((value - lo) / (hi - lo))


# ── 對齊 ────────────────────────────────────────────────────────────────
def _align(price: pd.DataFrame, inst: pd.DataFrame, margin: pd.DataFrame) -> pd.DataFrame:
    """以 price 的交易日為主軸，把 inst / margin 以 asof（<=當日）對齊。"""
    df = price.copy().sort_values("date").reset_index(drop=True)

    inst_cols = ["foreign_net", "trust_net", "dealer_net", "inst_net"]
    if inst is not None and not inst.empty:
        # 法人買賣超是「流量(flow)」：沒有申報的交易日代表當日淨額 = 0，不可向後
        # 延用舊值（merge_asof backward 會把前一次的買超灌到無申報日，虛增 inst_1d/
        # 6d/12d，並讓 rotation_research 的 inst_6d>0 群組濾網在無申報日假通過）。
        # 改成以交易日為軸精確 left-merge + 缺漏補 0，與 market_flow_monitor 一致。
        cols = [c for c in inst_cols if c in inst.columns]
        inst_s = inst[["date"] + cols].sort_values("date")
        df = df.merge(inst_s, on="date", how="left")
        for c in inst_cols:
            df[c] = df[c].fillna(0.0) if c in df.columns else 0.0
    else:
        for c in inst_cols:
            df[c] = 0.0

    if margin is not None and not margin.empty:
        margin_s = margin.sort_values("date")
        df = pd.merge_asof(df, margin_s, on="date", direction="backward")
    else:
        for c in ["margin_balance", "short_balance", "margin_limit",
                  "margin_change", "short_change"]:
            df[c] = np.nan

    return df


# ── 相對強勢 / 抗跌：滾動下行統計 ───────────────────────────────────────
def _rolling_downside_stats(stock_ret: np.ndarray, mkt_ret: np.ndarray,
                            window: int, min_down: int):
    """
    對每個時點 t，用過去 `window` 天中「大盤下跌日」計算：
      - 下行 beta：cov(個股, 大盤 | 大盤跌) / var(大盤 | 大盤跌)
                   低/負 = 大盤跌時個股跟跌少，抗跌。
      - 下跌日相對報酬：mean(個股日報酬 − 大盤日報酬 | 大盤跌)
                        >0 = 大盤跌時個股相對抗跌。
    全因果（只看 t 之前含 t 的視窗）。下跌日不足 min_down 回 NaN。
    """
    n = len(stock_ret)
    beta = np.full(n, np.nan)
    dd_excess = np.full(n, np.nan)
    s = stock_ret.astype(float)
    mk = mkt_ret.astype(float)
    for t in range(window - 1, n):
        sw = s[t - window + 1:t + 1]
        mw = mk[t - window + 1:t + 1]
        valid = ~(np.isnan(sw) | np.isnan(mw))
        sw = sw[valid]; mw = mw[valid]
        mask = mw < 0
        k = int(mask.sum())
        if k < min_down:
            continue
        sd = sw[mask]; md = mw[mask]
        var = md.var()
        if var > 0:
            beta[t] = float(np.cov(sd, md, ddof=0)[0, 1] / var)
        dd_excess[t] = float((sd - md).mean())
    return beta, dd_excess


def _attach_relative_strength(df: pd.DataFrame, market: pd.DataFrame) -> pd.DataFrame:
    """
    以 df（個股，已對齊交易日）與 market（大盤 TAIEX）算相對強勢 / 抗跌因子。
    把大盤收盤用 merge_asof(backward) 對齊到個股交易日（防未來函數：只用 ≤當日）。
    market 缺失時，相關欄位全部給 NaN（分數階段會轉成 0，不影響既有因子）。
    """
    n = len(df)
    if market is None or market.empty:
        df["mkt_close"] = np.nan
        df["rs_excess"] = np.nan
        df["downside_beta"] = np.nan
        df["down_day_excess"] = np.nan
        return df

    mkt = market[["date", "close"]].rename(columns={"close": "mkt_close"}).copy()
    # 統一 datetime 精度，避免 merge_asof 因 ns/us dtype 不一致而報錯
    mkt["date"] = mkt["date"].astype("datetime64[ns]")
    mkt = mkt.sort_values("date")
    df = df.copy()
    df["date"] = df["date"].astype("datetime64[ns]")
    df = pd.merge_asof(df.sort_values("date"), mkt, on="date", direction="backward")

    close = df["close"]
    mkt_close = df["mkt_close"]

    # (1) 相對強勢：60日「相對大盤」超額報酬 = 個股報酬 − 大盤報酬（比值型，scale-free）
    stock_lb = close / close.shift(config.RS_LOOKBACK)
    mkt_lb = mkt_close / mkt_close.shift(config.RS_LOOKBACK)
    df["rs_excess"] = stock_lb / mkt_lb - 1.0

    # (2)/(3) 下行 beta + 下跌日相對報酬（滾動視窗，只看大盤下跌日）
    stock_ret = close.pct_change().values
    mkt_ret = mkt_close.pct_change().values
    beta, dd_excess = _rolling_downside_stats(
        stock_ret, mkt_ret, config.DOWNSIDE_WINDOW, config.DOWNSIDE_MIN_DOWN_DAYS)
    df["downside_beta"] = beta
    df["down_day_excess"] = dd_excess
    return df


# ── 主函式 ──────────────────────────────────────────────────────────────
def compute_factors(bundle: dict) -> pd.DataFrame:
    """
    回傳含因子值與分數的 DataFrame（每日一列）。
    若 price 不足以計算 MA60，回傳空。
    """
    price = bundle.get("price")
    if price is None or price.empty or len(price) < config.MA_LONG + 5:
        return pd.DataFrame()

    df = _align(price, bundle.get("inst"), bundle.get("margin"))
    df = _attach_relative_strength(df, bundle.get("market"))

    close = df["close"]
    high = df["high"]
    low = df["low"]
    vol = df["volume"]  # 股

    # ── 技術指標基礎 ────────────────────────────────────────────────
    ma_s = close.rolling(config.MA_SHORT).mean()
    ma_l = close.rolling(config.MA_LONG).mean()
    std_s = close.rolling(config.BBANDS_WIN).std(ddof=0)
    bb_mid = close.rolling(config.BBANDS_WIN).mean()
    bb_upper = bb_mid + config.BBANDS_K * std_s
    bb_lower = bb_mid - config.BBANDS_K * std_s

    df["ma_short"] = ma_s
    df["ma_long"] = ma_l
    df["ma_long_slope"] = ma_l.diff(5)  # MA60 5日斜率

    # 布林位階：(close - 中軌) / (K*std)，0=中軌(月線), +1=上軌, -1=下軌
    bb_pos = (close - bb_mid) / (config.BBANDS_K * std_s.replace(0, np.nan))
    df["bb_pos"] = bb_pos

    # 均線糾結：短期 BIAS = |close-MA20|/MA20、中期 = |close-MA60|/MA60
    df["bias_short"] = (close - ma_s).abs() / ma_s
    df["bias_mid"] = (close - ma_l).abs() / ma_l

    # N 日新高（含今日），與距離新高的位置
    roll_high = high.rolling(config.HIGH_LOOKBACK).max()
    df["roll_high"] = roll_high
    df["near_high"] = close / roll_high  # 越接近 1 = 越靠近新高

    # 動能：MOM_LOOKBACK 日報酬（找「下一波成長」的核心：強者恆強）
    df["mom_ret"] = close / close.shift(config.MOM_LOOKBACK) - 1.0

    # 量能：近5日均量 / 前5日均量（窒息量 < 0.5）
    v5 = vol.rolling(5).mean()
    v5_prev = vol.shift(5).rolling(5).mean()
    df["vol_ratio"] = v5 / v5_prev
    df["avg_vol_lots"] = (vol.rolling(20).mean() / 1000.0)  # 近20日均量(張)

    # ── 籌碼指標 ────────────────────────────────────────────────────
    # 正規化分母：近20日均量(股)。法人淨買累積 / 均量 = 佔量比(天數當量)
    norm = vol.rolling(config.INST_NORM_WINDOW).mean().replace(0, np.nan)
    inst_net = df["inst_net"].fillna(0)

    df["inst_1d"] = inst_net.rolling(config.INST_WIN_SHORT).sum() / norm
    df["inst_6d"] = inst_net.rolling(config.INST_WIN_MID).sum() / norm
    df["inst_12d"] = inst_net.rolling(config.INST_WIN_LONG).sum() / norm

    # 下跌日法人仍買：近5日中，收黑(close<前一日)但 inst_net>0 的天數
    down_day = (close < close.shift(1))
    inst_buy = (inst_net > 0)
    dip_buy = (down_day & inst_buy).rolling(5).sum()
    df["inst_dip_buy_days"] = dip_buy  # 0~5

    # 資券比 = 融資餘額 / 融券餘額
    mb = df.get("margin_balance")
    sb = df.get("short_balance")
    if mb is not None and sb is not None:
        df["margin_short_ratio"] = mb / sb.replace(0, np.nan)
    else:
        df["margin_short_ratio"] = np.nan

    # ── 趨勢保護硬門檻 ──────────────────────────────────────────────
    df["trend_ok"] = (
        (df["ma_short"] > df["ma_long"])
        & (df["ma_long_slope"] > 0)
        & (close > df["ma_long"])
    )

    # ── 轉成 0~1 因子分數 ──────────────────────────────────────────
    df["score_inst_mid"] = df["inst_6d"].apply(lambda x: _scale(x, 0.0, 3.0) if pd.notna(x) else 0.0)
    df["score_inst_long"] = df["inst_12d"].apply(lambda x: _scale(x, 0.0, 5.0) if pd.notna(x) else 0.0)
    df["score_inst_dip_buy"] = df["inst_dip_buy_days"].apply(lambda x: _scale(x, 0, 3) if pd.notna(x) else 0.0)

    def _margin_health(r):
        if pd.isna(r):
            return 0.0
        lo, hi = config.MARGIN_OPTIMAL_LOW, config.MARGIN_OPTIMAL_HIGH
        if lo <= r <= hi:
            return 1.0
        if r < lo:
            return _scale(r, 0.0, lo)          # 太低（融券多）逐漸扣分
        return _clip01(1.0 - (r - hi) / hi)    # 太高（融資過熱）逐漸扣分
    df["score_margin_health"] = df["margin_short_ratio"].apply(_margin_health)

    # 均線多頭排列：close>MA20>MA60 給滿分，close>MA20 給半分
    def _ma_align(row):
        c, s, l = row["close"], row["ma_short"], row["ma_long"]
        if pd.isna(s) or pd.isna(l):
            return 0.0
        if c > s > l:
            return 1.0
        if c > s:
            return 0.5
        return 0.0
    df["score_ma_alignment"] = df.apply(_ma_align, axis=1)

    # 回檔到月線附近：布林位階在 [0, 0.5] 之間最佳（拉回但未破月線）
    def _bb_pullback(p):
        if pd.isna(p):
            return 0.0
        if 0.0 <= p <= 0.5:
            return 1.0
        if -0.3 <= p < 0.0:
            return _scale(p, -0.3, 0.0)        # 略破月線，部分分
        if 0.5 < p <= 1.0:
            return _clip01(1.0 - (p - 0.5) / 0.5)  # 太強（接近上軌）扣分
        return 0.0
    df["score_bb_pullback"] = df["bb_pos"].apply(_bb_pullback)

    # 均線糾結：BIAS 越小分數越高（能量壓縮）
    def _squeeze(row):
        bs, bm = row["bias_short"], row["bias_mid"]
        if pd.isna(bs) or pd.isna(bm):
            return 0.0
        s1 = _clip01(1.0 - bs / config.BIAS_SHORT_MAX)
        s2 = _clip01(1.0 - bm / config.BIAS_MID_MAX)
        return (s1 + s2) / 2.0
    df["score_ma_squeeze"] = df.apply(_squeeze, axis=1)

    # 窒息量：vol_ratio 越小（量縮）分數越高，<=0.5 給滿分
    def _dryup(v):
        if pd.isna(v):
            return 0.0
        if v <= config.VOL_DRYUP_RATIO:
            return 1.0
        return _clip01(1.0 - (v - config.VOL_DRYUP_RATIO) / 0.5)
    df["score_vol_dryup"] = df["vol_ratio"].apply(_dryup)

    # 動能：60日報酬越強(到 +30% 滿分) 且 貼近季線高點(>=0.90 滿分)，兩者取平均。
    # 「強勢 + 貼近高點」= 趨勢健康的續強股，這是波段成長股的典型樣貌。
    def _momentum(row):
        r, nh = row.get("mom_ret"), row.get("near_high")
        if pd.isna(r) or pd.isna(nh):
            return 0.0
        s_ret = _scale(r, 0.0, config.MOM_RET_FULL)         # 0% → +30% 映射 0→1
        s_high = _scale(nh, config.MOM_NEAR_HIGH_FULL, 1.0)  # 0.90 → 1.0 映射 0→1
        return (s_ret + s_high) / 2.0
    df["score_momentum"] = df.apply(_momentum, axis=1)

    # ── 相對強勢 / 抗跌因子分數（弱市防禦研究；對稱映射，見 config 註解）──
    # (1) rs：相對大盤超額報酬。±20% 對映 0~1（打平大盤=0.5）。逆勢相對強勢。
    df["score_rs"] = df["rs_excess"].apply(
        lambda x: _scale(x, -config.RS_EXCESS_FULL, config.RS_EXCESS_FULL) if pd.notna(x) else 0.0)

    # (2) downside_resilience：下行 beta 越低（甚至負）越抗跌，分數越高。
    #     beta<=0.4 → 1；beta>=1.8 → 0；線性內插。負 beta（逆勢上漲）夾在 1。
    def _downside(b):
        if pd.isna(b):
            return 0.0
        lo, hi = config.DOWNSIDE_BETA_DEFENSIVE, config.DOWNSIDE_BETA_AGGRESSIVE
        return _clip01((hi - b) / (hi - lo))
    df["score_downside_resilience"] = df["downside_beta"].apply(_downside)

    # (3) down_day_rs：大盤下跌日的平均相對報酬。±0.6%/日 對映 0~1（打平=0.5）。
    df["score_down_day_rs"] = df["down_day_excess"].apply(
        lambda x: _scale(x, -config.DOWNDAY_RS_FULL, config.DOWNDAY_RS_FULL) if pd.notna(x) else 0.0)

    return df


# 因子分數欄位 <-> config 權重 key 對照
SCORE_COLUMNS = {
    "momentum": "score_momentum",
    "inst_mid": "score_inst_mid",
    "inst_long": "score_inst_long",
    "inst_dip_buy": "score_inst_dip_buy",
    "margin_health": "score_margin_health",
    "ma_alignment": "score_ma_alignment",
    "bb_pullback": "score_bb_pullback",
    "ma_squeeze": "score_ma_squeeze",
    "vol_dryup": "score_vol_dryup",
    # 相對強勢 / 抗跌（弱市防禦研究，2026-07-20 加）
    "rs": "score_rs",
    "downside_resilience": "score_downside_resilience",
    "down_day_rs": "score_down_day_rs",
}


def legacy_selection(panel, *, min_composite: float,
                     trend_guard: bool):
    """legacy 九因子策略**自己的**選股規則:綜合分數門檻 ∧ 趨勢硬門檻。

    為什麼這個函式存在(2026-08-17 從引擎搬出來):這兩條規則本來寫在
    `backtest.event_backtest` 的 legacy 分支裡,也就是**引擎內部藏著一支策略**。
    那會造成一個具體的問題 —— `MIN_COMPOSITE` 與 `TREND_GUARD_ENABLED` 都是
    全域 config,所以它們進的是 evaluation_run 身分,不是 strategy_rule 身分:

        兩次 `strategy_rule_hash` 相同的 run,可以買到不同的股票。

    那正是兩層身分制要防的事。引擎該強制的是**市場強制你的事**(T+1、漲跌停、
    處置、整股、現金),而「MA20 要不要在 MA60 之上」是一個**看法**,屬於策略。

    搬過來不改變任何數字 —— 同樣兩條規則、同樣的順序、同樣的 config 來源。
    改變的是它們住在誰名下:現在它們明確屬於九因子這支 legacy 策略,
    引擎只是呼叫它。新的假說策略走 `strategy_kit.signal_builder`,
    在那裡趨勢閘門是 per-strategy 參數 `trend_guard`(進 rules hash)。

    註:這支策略在 `STRATEGY_REGISTRY.md` 是 S01 `rejected`。保留可執行是因為
    它是偏誤對照組,不是因為它是候選。
    """
    sig = panel[panel["composite"] >= float(min_composite)].copy()
    if trend_guard and "trend_ok" in sig.columns:
        sig = sig[sig["trend_ok"] == True]  # noqa: E712
    return sig


def composite_score(row) -> float:
    """把一列的各因子分數依 config.FACTOR_WEIGHTS 加權，正規化成 0~100。"""
    total_w = 0.0
    acc = 0.0
    for key, weight in config.FACTOR_WEIGHTS.items():
        col = SCORE_COLUMNS.get(key)
        if col is None or col not in row:
            continue
        val = row[col]
        if pd.isna(val):
            val = 0.0
        acc += weight * val
        total_w += weight
    if total_w == 0:
        return 0.0
    return round(acc / total_w * 100, 2)


if __name__ == "__main__":
    import data
    b = data.fetch_bundle("2330")
    f = compute_factors(b)
    print(f"factors rows: {len(f)}")
    cols = ["date", "close", "trend_ok", "inst_6d", "inst_12d", "bb_pos",
            "margin_short_ratio", "score_inst_mid", "score_bb_pullback"]
    print(f[cols].tail(5).to_string())
    last = f.iloc[-1]
    print(f"\n綜合分數（最後一日）：{composite_score(last)}")
