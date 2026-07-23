# -*- coding: utf-8 -*-
"""
快速原型回測 + 因子驗證
========================
回答兩個問題：
  (1) 整體回測：歷史上每天用「綜合分數」選股，隔日開盤進場、持有 N 天
      （停利/停損/到期），這套選股到底賺不賺？勝率、平均報酬、回撤多少？
  (2) 逐因子 IC：每個因子對「未來 N 日報酬」的資訊係數（Spearman rank
      correlation）。IC 顯著為正 = 這因子真的有預測力；接近 0 = 沒用。

防未來函數
----------
  - 訊號在第 T 日收盤後產生 -> 第 T+1 日「開盤」進場（config.BT_ENTRY_NEXT_OPEN）。
  - 因子全部因果計算（見 factors.py）。
  - 出場用持有期內的 high/low 判定停利停損，最後一天用收盤結算。

這是「快速原型」：先把流程跑通、先看有沒有方向。
結構預留 in_sample / out_sample 切分接口，之後可上 IS/OS + Embargo 嚴格驗證。
"""

from __future__ import annotations

from typing import List, Optional, Dict

import numpy as np
import pandas as pd

import config
import data
import dynamic_universe
import factors
import price_integrity
import universe as uni


# ── 未還原價 fail-closed 閘門（下沉到所有績效/因子路徑的共同咽喉點）─────────
def _assert_price_integrity(symbols: List[str]) -> None:
    """未還原價下,偵測到公司行動斷點就拒跑,避免產出被污染的假 Sharpe。

    - 還原價資料集（TaiwanStockPriceAdj）→ 直接放行。
    - 顯式 SWING_ALLOW_UNADJUSTED=1 → 印警告後放行（結果會在 summary 戳
      integrity_bypassed=True，不可當已驗證數字）。
    - 否則:對候選股票的價格序列跑斷點審計,只要命中就寫審計檔並 raise。

    以前只有 rotation_research.main 有這道閘門;backtest/validate_oos/factor_audit
    /screener 等主路徑都沒接,等於文件宣稱的 fail-closed 對主回測失效。這裡下沉
    到 _prepare_panel,讓所有共用引擎的路徑一律受保護。
    """
    dataset = getattr(config, "PRICE_DATASET", "TaiwanStockPrice")
    if price_integrity.is_adjusted_price_dataset(dataset):
        return
    if getattr(config, "ALLOW_UNADJUSTED_BACKTEST", False):
        print("[backtest] ⚠ 未還原價逃生門開啟(SWING_ALLOW_UNADJUSTED=1):結果含公司"
              "行動污染(除權息/分割/減資跳空)、非真實績效,請勿當已驗證數字引用。")
        return
    threshold = getattr(config, "PRICE_INTEGRITY_RETURN_THRESHOLD",
                        price_integrity.DEFAULT_DISCONTINUITY_THRESHOLD)
    frames = {}
    for sid in symbols:
        p = data.fetch_price(sid)
        if p is not None and not p.empty:
            frames[sid] = p
    audit = price_integrity.audit_price_frames(frames, threshold=threshold)
    if price_integrity.should_block_unadjusted_backtest(dataset, audit):
        out = config.OUTPUT_DIR / "price_integrity_audit.csv"
        try:
            audit.to_csv(out, index=False, encoding="utf-8-sig")
        except Exception:
            pass
        raise RuntimeError(
            f"[fail-closed] 未還原價資料集({dataset})偵測到 {len(audit)} 筆異常價格斷點"
            f"(分割/減資/除權息/壞列,門檻 {threshold:.0%})，主回測拒跑以免產出假 Sharpe。\n"
            f"  審計明細已存:{out}\n"
            f"  解法:(a) 改用還原價 SWING_PRICE_DATASET=TaiwanStockPriceAdj + survivorship-free PIT；"
            f"或 (b) 顯式 SWING_ALLOW_UNADJUSTED=1 跑污染 smoke test(結果不可當已驗證)。"
        )


# ── 單筆部位的「當日出場判定」────────────────────────────────────────────
def _check_exit(bar: pd.Series, pos: dict, days_held: int) -> Optional[tuple]:
    """
    給定某部位「今天的 K 棒」，判斷是否出場。回傳 (exit_price, reason) 或 None。
    重點：處理跳空——若開盤已穿價，成交在開盤價（更不利），不是理論價，避免高估績效。
    """
    o = float(bar["open"]); hi = float(bar["high"])
    lo = float(bar["low"]); c = float(bar["close"])
    entry = pos["entry_price"]

    if config.BT_EXIT_MODE == "fixed":
        tp_price = entry * (1 + config.BT_TAKE_PROFIT)
        sl_price = entry * (1 - config.BT_STOP_LOSS)
        # 先判停損（保守）；跳空時用更不利的價
        if lo <= sl_price:
            return (min(sl_price, o), "stop_loss")
        if hi >= tp_price:
            return (max(tp_price, o), "take_profit")
        if days_held >= config.BT_HOLD_DAYS:
            return (c, "time_exit")
        return None

    # ── trend 模式（真波段：讓獲利奔跑）──────────────────────────────
    # 前一交易日「收盤」已確認跌破 MA → 這一根「開盤」成交（T+1，與進場同慣例）。
    # 收盤跌破的訊號只有在收盤才知道，當根收盤無法回頭成交，故不可用當根收盤出場
    # （那是前視 leak）。改成標記 pending、下一交易日開盤實現。
    if pos.get("pending_ma_exit"):
        return (o, "ma_exit")
    sl_price = entry * (1 - config.BT_TREND_STOP_LOSS)
    if lo <= sl_price:                          # 硬停損（保命線），跳空取更不利價
        return (min(sl_price, o), "stop_loss")
    ma_exit = pos.get("ma_exit_today")          # 今日 MA_EXIT 值（收盤跌破則掛下一根開盤出）
    if ma_exit is not None and not np.isnan(ma_exit) and c < ma_exit:
        pos["pending_ma_exit"] = True           # 收盤確認跌破 → 掛單，下一交易日開盤出場
    if days_held >= config.BT_MAX_HOLD_DAYS:    # 殭屍部位上限（時間到期＝MOC，非前視）
        return (c, "max_hold")
    return None


# ── 預先計算所有股票的因子（含未來報酬）──────────────────────────────
def _prepare_panel(symbols: List[str], min_score_for_trade: float,
                   start_date: Optional[str], end_date: Optional[str],
                   dynamic_enabled: Optional[bool] = None,
                   universe_top_n: Optional[int] = None) -> pd.DataFrame:
    """
    把所有股票每一天的因子 + 綜合分數 + 未來N日報酬，攤平成一個大 panel。
    這個 panel 同時用於 (1) 整體回測選股 (2) 因子 IC 分析。
    """
    # 未還原價 fail-closed:任何走這個引擎的路徑（回測/IC/OOS/factor_audit/rotation）
    # 在未還原價且偵測到公司行動斷點時,先擋在這裡,不讓假績效產生。
    _assert_price_integrity(symbols)
    dynamic_enabled = (
        config.DYNAMIC_UNIVERSE_ENABLED
        if dynamic_enabled is None else bool(dynamic_enabled)
    )
    universe_top_n = universe_top_n or config.DYNAMIC_UNIVERSE_TOP_N

    name_map = uni.get_name_map()
    industry_map = uni.get_industry_map()

    # 大盤基準（RS / 抗跌因子用），只抓一次，注入每檔 bundle。
    market = data.fetch_market_index()

    score_cols = list(factors.SCORE_COLUMNS.values())
    records = []

    for sid in symbols:
        industry = industry_map.get(sid, "")
        if config.EXCLUDE_FINANCE and ("金融" in industry or "保險" in industry):
            continue
        if "ETF" in industry or "ETN" in industry or sid.startswith("00"):
            continue

        bundle = data.fetch_bundle(sid)
        bundle["market"] = market
        price = bundle.get("price")
        # Static mode keeps the legacy end-of-sample liquidity pre-filter for
        # comparison only. Dynamic mode evaluates liquidity point-in-time below.
        if price is None or price.empty:
            continue
        if not dynamic_enabled and not uni.passes_liquidity(price):
            continue
        f = factors.compute_factors(bundle)
        if f.empty:
            continue

        f = f.reset_index(drop=True)
        # 綜合分數（逐列）
        f["composite"] = f.apply(factors.composite_score, axis=1)

        # 未來 N 日報酬（用收盤對收盤，僅供 IC 分析；不含停利停損）
        # 用 BT_IC_HORIZON（波段尺度，約一個月），與固定持有天數脫鉤。
        close = f["close"].values
        fwd = np.full(len(close), np.nan)
        h = config.BT_IC_HORIZON
        for i in range(len(close) - h):
            if close[i] > 0:
                fwd[i] = (close[i + h] - close[i]) / close[i]
        f["fwd_ret"] = fwd
        f["stock_id"] = sid
        f["name"] = name_map.get(sid, "")

        # Keep the raw, causal factor fields as well as normalized scores.
        # They are useful for attribution and for research strategies that
        # separate sector/flow pre-filters from price-volume entry triggers.
        raw_research_cols = [
            "ma_short", "ma_long", "ma_long_slope",
            "roll_high", "near_high", "mom_ret", "vol_ratio",
            "inst_1d", "inst_6d", "inst_12d",
            "rs_excess", "downside_beta", "down_day_excess",
        ]
        keep = ["stock_id", "name", "date", "close", "open", "high", "low",
                "volume", "turnover", "avg_vol_lots",
                "composite", "trend_ok", "fwd_ret"] + raw_research_cols + score_cols
        keep = [c for c in keep if c in f.columns]
        records.append(f[keep])

    if not records:
        return pd.DataFrame()

    panel = pd.concat(records, ignore_index=True)
    _asof = getattr(config, "SNAPSHOT_END_DATE", "") or "live"
    universe_meta = {
        "enabled": dynamic_enabled,
        "direction": "long_only",
        "candidate_source": (
            f"saved_current_top{len(symbols)}_bootstrap"
            if dynamic_enabled else f"static_{len(symbols)}_symbols"
        ),
        "survivorship_free": False,
        # 產業分類非 PIT:用「當前」TaiwanStockInfo 套整段歷史(族群/濾金融用),
        # 缺歷史當時的產業標籤。族群輪動研究(S07/S08/S15)須把此標記納入解讀。
        "industry_pit": False,
        "industry_asof": _asof,
        "candidate_pool_asof": _asof,
    }
    if dynamic_enabled:
        ranked = dynamic_universe.add_membership(
            panel,
            top_n=universe_top_n,
            lookback=config.DYNAMIC_UNIVERSE_LOOKBACK,
            min_obs=config.DYNAMIC_UNIVERSE_MIN_OBS,
            min_avg_volume_lots=config.DYNAMIC_UNIVERSE_MIN_AVG_VOLUME_LOTS,
            min_avg_turnover=config.DYNAMIC_UNIVERSE_MIN_AVG_TURNOVER,
        )
        universe_meta.update({
            "top_n": universe_top_n,
            "lookback": config.DYNAMIC_UNIVERSE_LOOKBACK,
            "min_avg_volume_lots": config.DYNAMIC_UNIVERSE_MIN_AVG_VOLUME_LOTS,
            **dynamic_universe.membership_summary(ranked),
        })
        panel = ranked[ranked["in_dynamic_universe"]].copy()
    else:
        panel["in_dynamic_universe"] = True

    if start_date:
        panel = panel[panel["date"] >= pd.to_datetime(start_date)]
    if end_date:
        panel = panel[panel["date"] <= pd.to_datetime(end_date)]
    panel = panel.reset_index(drop=True)
    panel.attrs["universe"] = universe_meta
    return panel


# ── 市場濾網 / 擇時 overlay：大盤(TAIEX) risk-off 判定（全因果）──────────
def market_riskoff_map(rule: Optional[str] = None) -> Dict:
    """
    回傳 {date -> bool}，True = risk-off（大盤走弱、該降曝險）。
    全因果：只用到「當日收盤」算 MA / 波動（回測在 T 訊號、T+1 開盤動作）。
    暖身期（MA/vol 尚為 NaN）一律視為 risk-on（無法判斷時不亂空手）。
    規則見 config.MARKET_FILTER_RULE：ma200 / ma60 / ma20 / vol。
    """
    rule = rule or config.MARKET_FILTER_RULE
    m = data.fetch_market_index()
    if m is None or m.empty:
        return {}
    m = m.sort_values("date").reset_index(drop=True)
    c = m["close"]
    if rule in config.MARKET_FILTER_MA:
        win = config.MARKET_FILTER_MA[rule]
        ma = c.rolling(win).mean()
        ro = (c < ma).where(ma.notna(), False)
    elif rule == "vol":
        vol = c.pct_change().rolling(config.MARKET_FILTER_VOL_WINDOW).std() * np.sqrt(252)
        ro = (vol > config.MARKET_FILTER_VOL_THRESHOLD).where(vol.notna(), False)
    else:
        return {}
    return {d: bool(x) for d, x in zip(m["date"], ro)}


# ── (1) 整體回測：事件驅動 + 每日權益曲線 ───────────────────────────────
def backtest_portfolio(symbols: Optional[List[str]] = None,
                       sample: bool = True,
                       start_date: Optional[str] = None,
                       end_date: Optional[str] = None,
                       rebalance_every: int = 5,
                       top_n: int = 3,
                       dynamic_enabled: Optional[bool] = None,
                       universe_top_n: Optional[int] = None,
                       picks_by_date: Optional[Dict] = None) -> Dict:
    """
    事件驅動投組回測（修正版）。

    與舊版的關鍵差異（修掉會「被假數據騙」的數學錯誤）：
      1. **真正的每日權益曲線**：等權重最多持有 BT_MAX_POSITIONS 檔，逐日
         mark-to-market 加總成投組淨值。舊版把並行持倉當「一筆接一筆」連乘，
         導致累積報酬與 MaxDD 全部失真——這裡徹底重寫。
      2. **MaxDD / Sharpe 由每日淨值算**，不是由「交易池」亂算。
      3. **退場含跳空**：開盤穿價就用開盤價成交（見 _check_exit）。
      4. **持倉去重 + 上限**：同一檔不重複買、滿倉不再進場。
      5. **trend 退場**：跌破 MA 或硬停損才出，讓波段獲利奔跑（符合目標）。

    流程：走訪全市場交易日，每天先處理出場、再（逢 rebalance 日）用空位進場。
    進場一律 T+1 開盤（訊號在 T 日收盤後產生）。
    """
    dynamic_enabled = (
        config.DYNAMIC_UNIVERSE_ENABLED
        if dynamic_enabled is None else bool(dynamic_enabled)
    )
    universe_top_n = universe_top_n or config.DYNAMIC_UNIVERSE_TOP_N
    if symbols is None:
        if dynamic_enabled and not sample:
            symbols = uni.get_research_candidates(universe_top_n)
        else:
            symbols = uni.get_universe(sample=sample)

    max_positions = config.BT_MAX_POSITIONS

    # 每檔 price（含 MA_EXIT 供 trend 退場），date -> 列索引
    price_cache: Dict[str, pd.DataFrame] = {}
    date_idx_map: Dict[str, Dict] = {}
    for sid in symbols:
        p = data.fetch_price(sid)
        if p is None or p.empty:
            continue
        p = p.reset_index(drop=True)
        p["ma_exit"] = p["close"].rolling(config.BT_MA_EXIT).mean()
        price_cache[sid] = p
        date_idx_map[sid] = {d: i for i, d in enumerate(p["date"])}

    # picks_by_date 可由外部注入（例如 sector_rotation：族群輪動選股）。外部注入時
    # 跳過 composite/趨勢過濾（呼叫端自理），並用價格快取的交易日曆當 all_dates，
    # 避免重建整個 panel（省時、且不受 FACTOR_WEIGHTS 影響）。
    external_picks = picks_by_date is not None
    if not external_picks:
        panel = _prepare_panel(
            symbols, config.MIN_COMPOSITE, start_date, end_date,
            dynamic_enabled=dynamic_enabled,
            universe_top_n=universe_top_n,
        )
        if panel.empty:
            return {"error": "panel 為空，無法回測"}
        # 訊號查表：date -> 已過濾且排序的候選（stock_id, composite, name）
        sig = panel[panel["composite"] >= config.MIN_COMPOSITE].copy()
        if config.TREND_GUARD_ENABLED and "trend_ok" in sig.columns:
            sig = sig[sig["trend_ok"] == True]  # noqa: E712
        picks_by_date = {}
        for d, grp in sig.groupby("date"):
            g = grp.sort_values("composite", ascending=False)
            picks_by_date[d] = list(zip(g["stock_id"], g["composite"], g["name"]))
        all_dates = sorted(panel["date"].unique())
    else:
        # 外部注入 picks 時 _prepare_panel 不會被呼叫 → 這裡補上同一道未還原價
        # fail-closed 閘門,讓 sector_rotation 等外部路徑也受保護。
        _assert_price_integrity(symbols)
        if not picks_by_date:
            return {"error": "picks_by_date 為空，無法回測"}
        cal = sorted(set().union(*[set(p["date"]) for p in price_cache.values()])) if price_cache else []
        lo = min(picks_by_date)  # 從第一個有訊號的日子開始（含當日，隔日才進場）
        all_dates = [d for d in cal if d >= lo]
        if start_date:
            all_dates = [d for d in all_dates if d >= pd.to_datetime(start_date)]
        if end_date:
            all_dates = [d for d in all_dates if d <= pd.to_datetime(end_date)]
    date_pos = {d: i for i, d in enumerate(all_dates)}

    # ── 市場濾網 overlay 狀態（預設關；開啟才作用，不影響 FACTOR_WEIGHTS）──
    filter_on = bool(getattr(config, "MARKET_FILTER_ENABLED", False))
    riskoff_map = market_riskoff_map() if filter_on else {}
    riskoff_weight = float(getattr(config, "MARKET_FILTER_RISKOFF_WEIGHT", 0.0))
    n_filter_exits = 0
    n_regime_switches = 0
    _prev_riskoff = False

    # 投組狀態
    equity = 1.0
    cash = 1.0
    positions: Dict[str, dict] = {}   # sid -> 部位
    equity_curve = []                 # (date, equity)
    trades = []

    fee = config.BT_FEE
    sell_cost = config.BT_FEE + config.BT_TAX

    def _price_row(sid, d):
        idx = date_idx_map.get(sid, {}).get(d)
        if idx is None:
            return None, None
        return price_cache[sid].iloc[idx], idx

    for di, d in enumerate(all_dates):
        # ── 0) 市場濾網：用「訊號日(前一日)收盤」的 regime 決定今日目標曝險 ──
        riskoff = False
        target_positions = max_positions
        if filter_on and di > 0:
            riskoff = riskoff_map.get(all_dates[di - 1], False)
            if riskoff != _prev_riskoff:
                n_regime_switches += 1
                _prev_riskoff = riskoff
            if riskoff:
                target_positions = int(round(max_positions * riskoff_weight))

        # ── 1) 先處理當日出場（用今天的 K 棒）────────────────────────
        for sid in list(positions.keys()):
            pos = positions[sid]
            bar, idx = _price_row(sid, d)
            if bar is None:
                continue  # 當天該股沒資料，續抱
            if idx <= pos["entry_idx"]:
                continue  # 進場當天不在這裡出（出場判定從進場日的 _check_exit 已含）
            pos["ma_exit_today"] = float(bar["ma_exit"]) if "ma_exit" in bar else np.nan
            days_held = idx - pos["entry_idx"]
            ex = _check_exit(bar, pos, days_held)
            if ex is not None:
                exit_price, reason = ex
                proceeds = pos["shares"] * exit_price * (1 - sell_cost)
                cash += proceeds
                gross = (exit_price - pos["entry_price"]) / pos["entry_price"]
                net = proceeds / pos["cost"] - 1.0
                trades.append({
                    "stock_id": sid, "name": pos["name"],
                    "signal_date": pos["signal_date"],
                    "entry_date": pos["entry_date"], "exit_date": d,
                    "entry_price": round(pos["entry_price"], 2),
                    "exit_price": round(exit_price, 2),
                    "hold_bars": days_held,
                    "gross_ret": round(gross, 4),
                    "ret": round(net, 4),
                    "exit_reason": reason,
                    "composite": round(pos["composite"], 2),
                })
                del positions[sid]

        # ── 1.5) 市場濾網：risk-off 時把曝險降到目標，超額部位以今日開盤出場 ──
        #   （T+1 開盤動作，與進場同慣例；先出綜合分最弱者、留最強動能股）
        if filter_on and riskoff and len(positions) > target_positions:
            ordered = sorted(positions.items(), key=lambda kv: kv[1]["composite"])
            n_to_exit = len(positions) - target_positions
            for sid, pos in ordered:
                if n_to_exit <= 0:
                    break
                bar, idx = _price_row(sid, d)
                if bar is None or idx <= pos["entry_idx"]:
                    continue  # 無資料 / 進場當天不出
                exit_price = float(bar["open"])
                if not np.isfinite(exit_price) or exit_price <= 0:
                    continue
                proceeds = pos["shares"] * exit_price * (1 - sell_cost)
                cash += proceeds
                gross = (exit_price - pos["entry_price"]) / pos["entry_price"]
                net = proceeds / pos["cost"] - 1.0
                days_held = idx - pos["entry_idx"]
                trades.append({
                    "stock_id": sid, "name": pos["name"],
                    "signal_date": pos["signal_date"],
                    "entry_date": pos["entry_date"], "exit_date": d,
                    "entry_price": round(pos["entry_price"], 2),
                    "exit_price": round(exit_price, 2),
                    "hold_bars": days_held,
                    "gross_ret": round(gross, 4),
                    "ret": round(net, 4),
                    "exit_reason": "market_filter",
                    "composite": round(pos["composite"], 2),
                })
                del positions[sid]
                n_filter_exits += 1
                n_to_exit -= 1

        # ── 2) 逢 rebalance 日，用空位進場（T+1 開盤＝今天的 open）──────
        # 訊號日是「昨天」(d-1)，今天開盤進場。
        if di > 0 and (di - 1) % rebalance_every == 0:
            signal_date = all_dates[di - 1]
            candidates = picks_by_date.get(signal_date, [])[:top_n]
            entry_cap = target_positions if filter_on else max_positions
            for sid, comp, name in candidates:
                if len(positions) >= entry_cap:
                    break
                if sid in positions:
                    continue  # 已持有不重複買
                bar, idx = _price_row(sid, d)
                if bar is None or idx == 0:
                    continue
                entry_price = float(bar["open"])
                if not np.isfinite(entry_price) or entry_price <= 0:
                    continue
                alloc = equity / max_positions
                if cash < alloc * 0.5:
                    break  # 現金不足
                alloc = min(alloc, cash)
                shares = alloc * (1 - fee) / entry_price  # 扣買進手續費
                cash -= alloc
                positions[sid] = {
                    "name": name, "composite": float(comp),
                    "signal_date": signal_date, "entry_date": d,
                    "entry_idx": idx, "entry_price": entry_price,
                    "cost": alloc, "shares": shares,
                    "ma_exit_today": np.nan,
                    "pending_ma_exit": False,
                }

        # ── 3) 收盤 mark-to-market：投組淨值 = 現金 + 各部位市值 ──────
        mtm = cash
        for sid, pos in positions.items():
            bar, _ = _price_row(sid, d)
            px = float(bar["close"]) if bar is not None else pos["entry_price"]
            mtm += pos["shares"] * px
        equity = mtm
        equity_curve.append((d, equity))

    # ── 結算：用每日淨值算正確的績效指標 ────────────────────────────
    if not trades and len(positions) == 0:
        return {"error": "回測期間無任何交易（可能門檻太高或樣本太少）", "n_trades": 0}

    eq = pd.DataFrame(equity_curve, columns=["date", "equity"]).set_index("date")
    daily_ret = eq["equity"].pct_change().dropna()

    cum_ret = float(eq["equity"].iloc[-1] - 1.0)
    peak = eq["equity"].cummax()
    max_dd = float(((eq["equity"] - peak) / peak).min())
    ann_ret = float(daily_ret.mean() * 252)
    ann_vol = float(daily_ret.std(ddof=1) * np.sqrt(252)) if len(daily_ret) > 1 else 0.0
    sharpe = (ann_ret / ann_vol) if ann_vol > 0 else 0.0

    # Sortino：只用「下跌波動」當分母（負報酬的均方根，年化）。
    # 對「低勝率、靠少數大贏家」的趨勢策略更公允——Sharpe 會把大漲也當風險扣分，
    # Sortino 不懲罰上漲波動，只在意虧損端。
    downside = daily_ret[daily_ret < 0]
    downside_dev = float(np.sqrt((downside ** 2).mean()) * np.sqrt(252)) if len(downside) > 0 else 0.0
    sortino = (ann_ret / downside_dev) if downside_dev > 0 else float("nan")

    # Calmar：年化報酬 / 最大回撤絕對值。直接衡量「賺的相對於最痛回撤」值不值得。
    calmar = (ann_ret / abs(max_dd)) if max_dd < 0 else float("nan")

    tdf = pd.DataFrame(trades) if trades else pd.DataFrame()
    if not tdf.empty:
        trade_rets = tdf["ret"].values
        win_rate = float((trade_rets > 0).mean())
        avg_ret = float(trade_rets.mean())
        median_ret = float(np.median(trade_rets))
        avg_hold = float(tdf["hold_bars"].mean())
        exit_breakdown = tdf["exit_reason"].value_counts().to_dict()
        # 期望值 / 賺賠比
        wins = trade_rets[trade_rets > 0]; losses = trade_rets[trade_rets <= 0]
        payoff = float(wins.mean() / abs(losses.mean())) if len(wins) and len(losses) else float("nan")
    else:
        win_rate = avg_ret = median_ret = avg_hold = payoff = float("nan")
        exit_breakdown = {}

    summary = {
        "n_trades": len(tdf),
        "open_positions_end": len(positions),
        "win_rate": round(win_rate, 4),
        "avg_ret": round(avg_ret, 4),
        "median_ret": round(median_ret, 4),
        "payoff_ratio": round(payoff, 3),
        "avg_hold_bars": round(avg_hold, 1),
        "cum_ret": round(cum_ret, 4),
        "ann_ret": round(ann_ret, 4),
        "ann_vol": round(ann_vol, 4),
        "sharpe": round(sharpe, 3),
        "sortino": round(sortino, 3) if sortino == sortino else float("nan"),
        "calmar": round(calmar, 3) if calmar == calmar else float("nan"),
        "max_drawdown": round(max_dd, 4),
        "exit_breakdown": exit_breakdown,
        "period": [str(all_dates[0])[:10], str(all_dates[-1])[:10]],
        "market_filter": {
            "enabled": filter_on,
            "rule": config.MARKET_FILTER_RULE if filter_on else None,
            "riskoff_weight": riskoff_weight if filter_on else None,
            "n_filter_exits": n_filter_exits,
            "n_regime_switches": n_regime_switches,
        },
        "universe": panel.attrs.get("universe", {
            "enabled": dynamic_enabled,
            "direction": "long_only",
            "top_n": universe_top_n if dynamic_enabled else None,
        }),
        "data": {
            "price_dataset": getattr(config, "PRICE_DATASET", "TaiwanStockPrice"),
            "adjusted_price": price_integrity.is_adjusted_price_dataset(
                getattr(config, "PRICE_DATASET", "TaiwanStockPrice")
            ),
            "snapshot_end": getattr(config, "SNAPSHOT_END_DATE", ""),
            # 未還原價 + 逃生門開啟時為 True:此結果含公司行動污染,非已驗證績效。
            "integrity_bypassed": (
                not price_integrity.is_adjusted_price_dataset(
                    getattr(config, "PRICE_DATASET", "TaiwanStockPrice"))
                and bool(getattr(config, "ALLOW_UNADJUSTED_BACKTEST", False))
            ),
        },
        "params": {
            "exit_mode": config.BT_EXIT_MODE,
            "ma_exit": config.BT_MA_EXIT,
            "trend_stop": config.BT_TREND_STOP_LOSS,
            "max_hold": config.BT_MAX_HOLD_DAYS,
            "min_composite": config.MIN_COMPOSITE,
            "rebalance_every": rebalance_every,
            "max_positions": max_positions,
        },
    }
    return {"summary": summary, "trades": tdf, "equity_curve": eq.reset_index()}


# ── (2) 逐因子 IC 分析 ──────────────────────────────────────────────────
def factor_ic(symbols: Optional[List[str]] = None,
              sample: bool = True,
              start_date: Optional[str] = None,
              end_date: Optional[str] = None,
              dynamic_enabled: Optional[bool] = None,
              universe_top_n: Optional[int] = None) -> pd.DataFrame:
    """
    每個因子分數對「未來 BT_IC_HORIZON 日報酬」的 Spearman rank IC。

    統計嚴謹度（修正版）：
      - 用「每日橫斷面 IC」序列，回報 mean_ic、ic_std、ic_ir。
      - **重疊校正的 t 值**：fwd_ret 視窗重疊 h 天，相鄰每日 IC 高度自相關，
        會灌水顯著性。用 Newey-West 風格的有效樣本數
        n_eff = n_days / h 來算 t_stat = ic_ir * sqrt(n_eff)，保守反映真實顯著性。
      - **不再靜默 pool**：橫斷面樣本不足時 mode 標 "insufficient"，數字標 NaN，
        誠實告訴使用者「這個 universe 太小、IC 不可信」，而不是偷偷換一種算法。

    判讀（保守）：|mean_ic|>0.03 且 |t_stat|>2 才算有方向性證據；
    小 universe（橫斷面 < 5 檔）一律視為 insufficient。
    """
    dynamic_enabled = (
        config.DYNAMIC_UNIVERSE_ENABLED
        if dynamic_enabled is None else bool(dynamic_enabled)
    )
    universe_top_n = universe_top_n or config.DYNAMIC_UNIVERSE_TOP_N
    if symbols is None:
        if dynamic_enabled and not sample:
            symbols = uni.get_research_candidates(universe_top_n)
        else:
            symbols = uni.get_universe(sample=sample)
    panel = _prepare_panel(
        symbols, 0.0, start_date, end_date,
        dynamic_enabled=dynamic_enabled,
        universe_top_n=universe_top_n,
    )
    if panel.empty:
        return pd.DataFrame()

    score_cols = [c for c in factors.SCORE_COLUMNS.values() if c in panel.columns]
    panel = panel.dropna(subset=["fwd_ret"])
    h = max(1, config.BT_IC_HORIZON)
    MIN_CROSS = 5  # 每日橫斷面至少要 5 檔才算數

    results = []
    for col in score_cols:
        daily_ics = []
        for d, grp in panel.groupby("date"):
            sub = grp[[col, "fwd_ret"]].dropna()
            if len(sub) < MIN_CROSS or sub[col].nunique() < 2:
                continue
            ic = sub[col].corr(sub["fwd_ret"], method="spearman")
            if pd.notna(ic):
                daily_ics.append(ic)

        if len(daily_ics) < 2:
            # 橫斷面不足 → 誠實標記 insufficient，不偷換成 pooled
            results.append({
                "factor": col.replace("score_", ""),
                "mean_ic": np.nan, "ic_std": np.nan, "ic_ir": np.nan,
                "t_stat": np.nan, "n_days": len(daily_ics), "mode": "insufficient",
            })
            continue

        arr = np.array(daily_ics)
        mean_ic = float(arr.mean())
        ic_std = float(arr.std(ddof=1))
        ic_ir = (mean_ic / ic_std) if ic_std > 0 else np.nan
        # 重疊校正：有效獨立樣本數 ≈ 天數 / 視窗
        n_eff = max(1.0, len(arr) / h)
        t_stat = (ic_ir * np.sqrt(n_eff)) if pd.notna(ic_ir) else np.nan
        results.append({
            "factor": col.replace("score_", ""),
            "mean_ic": round(mean_ic, 4),
            "ic_std": round(ic_std, 4),
            "ic_ir": round(float(ic_ir), 3) if pd.notna(ic_ir) else np.nan,
            "t_stat": round(float(t_stat), 2) if pd.notna(t_stat) else np.nan,
            "n_days": len(arr),
            "mode": "cross_sectional",
        })

    out = pd.DataFrame(results).sort_values(
        "mean_ic", ascending=False, key=lambda s: s.abs(), na_position="last"
    ).reset_index(drop=True)
    return out


# ── 報告 ────────────────────────────────────────────────────────────────
def _print_bt_summary(res: dict):
    if "error" in res and "summary" not in res:
        print(f"  [回測] {res['error']}")
        return
    s = res["summary"]
    p = s["params"]
    u = s.get("universe", {})
    print("=" * 72)
    print("  整體回測結果（多因子選股 + 每日權益曲線）")
    print("=" * 72)
    print(f"  期間：{s['period'][0]} ~ {s['period'][1]}")
    if p.get("exit_mode") == "trend":
        print(f"  退場：trend（跌破MA{p['ma_exit']} 或 硬停損 -{p['trend_stop']:.0%}"
              f" 或 抱滿{p['max_hold']}天）")
    else:
        print(f"  退場：fixed（持有{config.BT_HOLD_DAYS}天 / 停利+{config.BT_TAKE_PROFIT:.0%}"
              f" / 停損-{config.BT_STOP_LOSS:.0%}）")
    print(f"  參數：每{p['rebalance_every']}日選股 / 最多持有{p['max_positions']}檔"
          f" / 綜合分數門檻 {p['min_composite']}")
    if u.get("enabled"):
        print(f"  Universe：long-only 動態 top{u.get('top_n')} / "
              f"候選 {u.get('n_candidate_symbols', '—')} 檔 / "
              f"{u.get('lookback')}日平均成交值排名")
        if not u.get("survivorship_free", False):
            print("  ⚠ 候選池仍是 current-pool bootstrap；尚非完整 survivorship-free PIT")
    else:
        print("  Universe：static（legacy comparison）")
    print("-" * 72)
    print(f"  交易筆數      ：{s['n_trades']}（期末未平倉 {s['open_positions_end']}）")
    print(f"  勝率          ：{s['win_rate']:.1%}")
    print(f"  平均報酬/筆   ：{s['avg_ret']:+.2%}")
    print(f"  中位數報酬/筆 ：{s['median_ret']:+.2%}")
    print(f"  賺賠比(payoff)：{s['payoff_ratio']}")
    print(f"  平均持有天數  ：{s['avg_hold_bars']}")
    print("-" * 72)
    print(f"  累積報酬      ：{s['cum_ret']:+.2%}")
    print(f"  年化報酬      ：{s['ann_ret']:+.2%}")
    print(f"  年化波動      ：{s['ann_vol']:.2%}")
    print(f"  Sharpe(年化)  ：{s['sharpe']:.2f}   (報酬/總波動)")
    print(f"  Sortino(年化) ：{s.get('sortino', float('nan')):.2f}   (報酬/下跌波動，對奔跑型策略較公允)")
    print(f"  Calmar        ：{s.get('calmar', float('nan')):.2f}   (年化報酬/最大回撤)")
    print(f"  最大回撤      ：{s['max_drawdown']:.2%}")
    print(f"  出場原因      ：{s['exit_breakdown']}")
    print("=" * 72)


def _print_ic(ic_df: pd.DataFrame):
    print("=" * 72)
    print("  逐因子 IC 分析（與未來報酬的 Spearman 相關；越正越有預測力）")
    print("=" * 72)
    if ic_df.empty:
        print("  （無足夠資料計算 IC）")
        print("=" * 72)
        return
    print(f"  {'因子':<16}{'mean_IC':>10}{'IC_IR':>8}{'t_stat':>8}{'n_days':>8}  判讀")
    print("-" * 72)
    for _, r in ic_df.iterrows():
        ic = r["mean_ic"]
        t = r.get("t_stat")
        if r.get("mode") == "insufficient":
            verdict = "資料不足(universe太小)"
        elif pd.isna(ic):
            verdict = "—"
        else:
            sig = pd.notna(t) and abs(t) > 2          # 重疊校正後仍顯著
            if ic > 0.03 and sig:
                verdict = "★ 有正向預測力"
            elif ic < -0.03 and sig:
                verdict = "✗ 反向(可考慮反著用)"
            elif abs(ic) > 0.02:
                verdict = "弱訊號(未達顯著)"
            else:
                verdict = "無明顯預測力"
        ic_s = f"{ic:+.4f}" if pd.notna(ic) else "n/a"
        ir_s = f"{r['ic_ir']:+.2f}" if pd.notna(r.get("ic_ir")) else "n/a"
        t_s = f"{t:+.2f}" if pd.notna(t) else "n/a"
        print(f"  {r['factor']:<16}{ic_s:>10}{ir_s:>8}{t_s:>8}{int(r['n_days']):>8}  {verdict}")
    print("=" * 72)
    print("  註：t_stat 已對 fwd_ret 重疊做保守校正(有效樣本=天數/視窗)。")
    print("      |t|>2 才算顯著；小集合常 insufficient，需擴大 universe 才算數。")


def run_full(sample: bool = True, top_n: int = 3, rebalance_every: int = 5,
             pool: Optional[int] = None,
             dynamic_enabled: Optional[bool] = None,
             universe_top_n: Optional[int] = None):
    """一次跑完整體回測 + 因子IC，並印報告。"""
    dynamic_enabled = (
        config.DYNAMIC_UNIVERSE_ENABLED
        if dynamic_enabled is None else bool(dynamic_enabled)
    )
    # 小型 sample 是 smoke test，不冒充動態全市場研究。
    effective_dynamic = dynamic_enabled and not sample
    universe_top_n = universe_top_n or config.DYNAMIC_UNIVERSE_TOP_N
    if effective_dynamic:
        symbols = uni.get_research_candidates(
            universe_top_n=universe_top_n,
            candidate_pool_n=pool,
        )
    elif pool:
        symbols = uni.get_universe(top_n=pool)
    else:
        symbols = uni.get_universe(sample=sample)
    print(f"\n[backtest] universe = {len(symbols)} 檔，開始回測...\n")

    res = backtest_portfolio(symbols=symbols, sample=sample,
                             rebalance_every=rebalance_every, top_n=top_n,
                             dynamic_enabled=effective_dynamic,
                             universe_top_n=universe_top_n)
    _print_bt_summary(res)

    ic_df = factor_ic(
        symbols=symbols, sample=sample,
        dynamic_enabled=effective_dynamic,
        universe_top_n=universe_top_n,
    )
    print()
    _print_ic(ic_df)

    # 存檔
    if "trades" in res:
        path = config.OUTPUT_DIR / "backtest_trades.csv"
        res["trades"].to_csv(path, index=False, encoding="utf-8-sig")
        print(f"\n  交易明細已存：{path}")
    if not ic_df.empty:
        path = config.OUTPUT_DIR / "factor_ic.csv"
        ic_df.to_csv(path, index=False, encoding="utf-8-sig")
        print(f"  因子IC已存：{path}")

    return res, ic_df


if __name__ == "__main__":
    run_full(sample=True, top_n=3, rebalance_every=5)
