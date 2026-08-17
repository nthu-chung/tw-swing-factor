# -*- coding: utf-8 -*-
"""向量化近似引擎 —— **只能用於搜尋,不能作正式證據**。

為什麼需要它
------------
發掘階段要評估幾十到幾百個候選,而事件引擎跑一支要 ~40 秒(五相位、真實成交
模擬)。43 個因子就是半小時,GA 一輪幾百次評估根本不可能。

先前的替代方案是 **IC 掃描**(1 分鐘掃 43 個),但那被實測證明是**誤導**訊號:
IC 衡量整條排序線的相關性,而策略只吃前 N 名。實測六支從 IC 挑出的假說,
全部輸給一支「IC 略為負」的對照組。

這支的定位是中間層:**快到能當篩子,但量的是策略真正在做的事**(只買前 N 名、
週頻換股、扣成本),而不是整條排序線。

    IC 掃描            1 分鐘   整條排序線        ← 誤導
    vec_backtest       秒級     前 N 名 + 週轉    ← 篩選用
    event_backtest     40 秒    + T+1/漲跌停/處置/整股/現金帳  ← 決選與正式證據

保留了什麼、丟掉了什麼
----------------------
**保留**(丟了就會選錯方向):

- 跑滿所有等價 weekly 相位,報中位數與最小值 —— 只報一個相位等於挑路徑。
- entry/exit 的 hysteresis 帶(進 top `entry_rank`、掉出 `exit_rank` 才賣),
  這是 H4 最主要的出場來源(實測 79.4% 的出場是 `rank_decay`)。
- 排名母體與 eligible 遮罩。
- 週轉成本(手續費雙邊 + 賣出證交稅)。
- **T+1**:T 日收盤的訊號,權重從 T+1 才生效。

**丟掉**(這些才是 100 倍慢的來源,而且不影響「哪個因子比較好」的排序):

- 逐日成交模擬、漲跌停與一字鎖停、處置股禁倉、整股張數、精確現金帳、
  單檔上限的動態減碼、災難停損。

因此它的絕對數字**不可引用**,只能用來比較候選之間的相對優劣;而且相對優劣
也必須用 `tests/test_engine_parity.py` 量出來的差距分布來解讀,不是假設一致。
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from evaluation.phases import sweep_phases

ENGINE_TAG = "vectorized_approximate"
TRADING_DAYS = 252

#: 這些近似會出現在每一份回傳裡,讓下游沒辦法假裝它是事件引擎的結果。
APPROXIMATIONS = (
    "no_t1_fill_simulation", "no_price_limit", "no_disposition_ban",
    "no_lot_rounding", "no_cash_ledger", "no_catastrophic_stop",
    "no_concentration_trim",
)


def _weekly_phase_days(days: Sequence[pd.Timestamp], phase: int) -> List[pd.Timestamp]:
    """每個 ISO 週的第 `phase` 個交易日;該週不足時取最後一個。

    與 `event_backtest.select_decision_snapshots` 同語意 —— 兩邊的相位定義必須
    一致,否則 parity 比較的是兩件不同的事。
    """
    idx = pd.DatetimeIndex(sorted(days))
    iso = idx.isocalendar()
    out = []
    for _, grp in pd.Series(idx, index=idx).groupby([iso.year, iso.week]):
        vals = list(grp.values)
        out.append(pd.Timestamp(vals[min(phase, len(vals) - 1)]))
    return sorted(out)


def _metrics(equity: pd.Series, initial: float) -> Dict[str, float]:
    r = equity.pct_change().dropna()
    ann = float(r.mean() * TRADING_DAYS) if len(r) else 0.0
    vol = float(r.std(ddof=1) * math.sqrt(TRADING_DAYS)) if len(r) > 1 else 0.0
    peak = equity.cummax()
    return {
        "cum_return": float(equity.iloc[-1] / initial - 1.0),
        "ann_return": ann, "ann_volatility": vol,
        "sharpe": (ann / vol) if vol > 0 else 0.0,
        "max_drawdown": float(((equity - peak) / peak).min()) if len(equity) else 0.0,
    }


def run_phase(*, rank_arr: np.ndarray, ret_arr: np.ndarray,
              decide_idx: np.ndarray, entry_rank: int, exit_rank: int,
              max_slots: int, initial_capital: float,
              cost_one_way: float, sell_tax: float) -> Dict[str, Any]:
    """跑單一相位。**全 numpy,而且只在決策日迴圈。**

    第一版對「每一天」跑 pandas 迴圈(5 相位 × 330 天 = 1650 次,每次在 700 欄的
    Series 上運算),實測 40 秒 —— 和事件引擎一樣慢,等於這支完全沒有存在價值。
    現在:決策日之間的權重漂移用累積乘積一次算完,迴圈只跑決策日(~66 次)。

    `rank_arr` / `ret_arr` 都是 (n_days, n_stocks) 的 float ndarray,
    rank 缺值為 NaN。
    """
    n_days, n_stocks = ret_arr.shape
    w = np.zeros(n_stocks, dtype=float)
    held = np.zeros(n_stocks, dtype=bool)
    eq = np.empty(n_days, dtype=float)
    eq[0] = initial_capital
    turnover = cost_paid = 0.0
    rebal = set(int(i) for i in decide_idx)

    for i in range(1, n_days):
        r = ret_arr[i]
        gross = float(np.nansum(w * r))
        if w.any():
            w = w * (1.0 + np.nan_to_num(r))
        # T+1:前一日是決策日,今天才換股
        if (i - 1) in rebal:
            rk = rank_arr[i - 1]
            ok = np.isfinite(rk)
            if ok.any():
                keep = held & ok & (rk <= exit_rank)
                room = max_slots - int(keep.sum())
                if room > 0:
                    cand = ok & (rk <= entry_rank) & ~keep
                    if cand.any():
                        idx = np.flatnonzero(cand)
                        idx = idx[np.argsort(rk[idx], kind="stable")][:room]
                        keep = keep.copy()
                        keep[idx] = True
                held = keep
                k = int(held.sum())
                new = np.zeros(n_stocks, dtype=float)
                if k:
                    new[held] = 1.0 / k
                t = float(np.abs(new - w).sum()) / 2.0
                sells = float(np.clip(w - new, 0.0, None).sum())
                c = t * cost_one_way * 2.0 + sells * sell_tax
                turnover += t
                cost_paid += c
                gross -= c
                w = new
        eq[i] = eq[i - 1] * (1.0 + gross)
    return {"equity": eq, "turnover": turnover, "cost_paid_pct": cost_paid,
            "n_decision_days": len(rebal)}


def vec_backtest(*, signal_frame: pd.DataFrame, panel: pd.DataFrame,
                 entry_rank: int = 10, exit_rank: int = 20, max_slots: int = 10,
                 initial_capital: float = 1_000_000.0,
                 cost_one_way: float = 0.000399, sell_tax: float = 0.003,
                 n_phases: int = 5, price_col: str = "close") -> Dict[str, Any]:
    """向量化近似回測。**回傳一律帶 `engine="vectorized_approximate"`。**

    `signal_frame` 需含 `date / stock_id / rank`(通過 validator 的那份)。
    `panel` 需含 `date / stock_id / <price_col>`。
    """
    sig = signal_frame[["date", "stock_id", "rank"]].copy()
    sig["date"] = pd.to_datetime(sig["date"])
    sig["stock_id"] = sig["stock_id"].astype(str)

    p = panel[["date", "stock_id", price_col]].copy()
    p["date"] = pd.to_datetime(p["date"])
    p["stock_id"] = p["stock_id"].astype(str)
    lo, hi = sig["date"].min(), sig["date"].max()
    p = p[(p["date"] >= lo) & (p["date"] <= hi)]

    # 只保留有訊號的股票 —— 其餘欄位永遠是 0 權重,留著只是讓矩陣變大
    keep_ids = sorted(set(sig["stock_id"]))
    close = p[p["stock_id"].isin(keep_ids)].pivot_table(
        index="date", columns="stock_id", values=price_col).sort_index()
    if close.empty or close.shape[0] < 5:
        raise RuntimeError("[fail-closed] 價格資料不足,無法回測")
    rank_w = sig.pivot_table(index="date", columns="stock_id", values="rank",
                             aggfunc="min").reindex(index=close.index,
                                                    columns=close.columns)
    # **一次轉成 numpy**;後面完全不再碰 pandas 索引
    ret_arr = np.array(close.pct_change().to_numpy(dtype=float), copy=True)
    ret_arr[0, :] = 0.0
    rank_arr = rank_w.to_numpy(dtype=float)
    days = list(close.index)
    day_pos = {d: i for i, d in enumerate(days)}

    # **相位掃描交給 `evaluation.phases.sweep_phases`** —— repo 裡唯一的實作。
    # 第一版在這裡自己寫了 `for ph in range(...)`,被 AST 守衛抓到(而且抓得對:
    # 那就是第二份手寫相位掃描)。共用之後,兩個引擎的 median/min/worst 口徑
    # 保證一致 —— 否則對拍時分不出差距來自近似還是來自統計口徑。
    curves: Dict[int, pd.Series] = {}

    def _one(phase: int) -> Optional[Dict[str, Any]]:
        dec = _weekly_phase_days(days, phase)
        didx = np.array([day_pos[d] for d in dec if d in day_pos], dtype=int)
        if len(didx) < 3:
            return None
        out = run_phase(rank_arr=rank_arr, ret_arr=ret_arr, decide_idx=didx,
                        entry_rank=entry_rank, exit_rank=exit_rank,
                        max_slots=max_slots, initial_capital=initial_capital,
                        cost_one_way=cost_one_way, sell_tax=sell_tax)
        curve = pd.Series(out["equity"], index=days)
        curves[phase] = curve
        return {"phase": phase, "n_decision_days": out["n_decision_days"],
                "turnover": out["turnover"],
                "cost_paid_pct": out["cost_paid_pct"],
                **_metrics(curve, initial_capital)}

    sweep = sweep_phases(_one, n_phases=int(n_phases), single_phase_debug=False)
    rows = sweep.rows.to_dict("records")

    if not rows:
        raise RuntimeError("[fail-closed] 沒有任何相位產生結果")

    df = sweep.rows.sort_values("phase").reset_index(drop=True)
    ordered = df.sort_values(["sharpe", "phase"], kind="mergesort")
    rep = int(ordered.iloc[len(ordered) // 2]["phase"])
    return {
        "engine": ENGINE_TAG,
        "formal_evidence_eligible": False,      # 結構上就不是正式證據
        "approximations": list(APPROXIMATIONS),
        "phase_results": df.drop(columns=[]),
        # 統計也走共用實作,與事件引擎同口徑
        "phase_stats": {**sweep.stats(), "n_phases": int(len(df))},
        "representative_phase": rep,
        "equity_curve": pd.DataFrame({"date": curves[rep].index,
                                      "equity": curves[rep].to_numpy()}),
        "cum_return": float(df.loc[df["phase"].eq(rep), "cum_return"].iloc[0]),
        "turnover": float(df.loc[df["phase"].eq(rep), "turnover"].iloc[0]),
        "claim_boundary": (
            "向量化近似:丟掉 T+1 成交模擬、漲跌停、處置、整股與現金帳。"
            "絕對數字不可引用,只能用於候選之間的相對比較,而且要參考 "
            "tests/test_engine_parity.py 量出的差距分布。"),
    }
