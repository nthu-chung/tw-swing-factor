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
import evaluation_split
import factors
import price_integrity
import universe as uni
# MonthlyPITUniverseProvider 這裡不直接呼叫(候選池一律走 historical_pit_universe),
# 但保留 re-export:它是 provider 的正式類別,測試與外部腳本以 backtest 為錨點取用。
from universes import MonthlyPITUniverseProvider, historical_pit_universe  # noqa: F401
from execution.tradability import detect_limit_lock as _limit_lock
from execution.tradability import load_disposition_days as _load_disposition_days
from execution.costs import OrderSizeMode, TaiwanStockCostModel, size_long_order


# ── 未還原價 fail-closed 閘門（下沉到所有績效/因子路徑的共同咽喉點）─────────
def _assert_price_integrity(symbols: List[str]) -> None:
    """未還原價一律拒跑,避免產出被公司行動污染的假 Sharpe。

    - 還原價資料集（TaiwanStockPriceAdj）→ 直接放行。
    - 顯式 SWING_ALLOW_UNADJUSTED=1 → 印警告後放行（結果會在 summary 戳
      integrity_bypassed=True，不可當已驗證數字）。
    - 否則:未還原價一律 raise,並附上斷點審計檔當診斷資料。

    以前只有 rotation_research.main 有這道閘門;backtest/validate_oos/factor_audit
    /screener 等主路徑都沒接,等於文件宣稱的 fail-closed 對主回測失效。這裡下沉
    到 _prepare_panel,讓所有共用引擎的路徑一律受保護。

    2026-08-02 修:原本是「未還原價 *且* 審計命中」才擋,等於把「掃描沒掃到」
    當成「價格乾淨」的證據。但掃描門檻不可能壓到 ±10% 漲跌停以下(否則真實漲跌停
    全被誤判),而台股現金股息除息缺口約 3~5%,結構上就在掃描的盲區裡。實測:
    top100 命中 11 檔,把那 11 檔拿掉,剩 89 檔審計為空 → 舊邏輯直接放行,但那
    89 檔仍有 1716 筆 3~10% 隔夜跳空無法與真實走勢區分。審計改為純診斷用途。
    """
    dataset = getattr(config, "PRICE_DATASET", "TaiwanStockPrice")
    if price_integrity.is_adjusted_price_dataset(dataset):
        return
    # 自建還原價:除權息已用官方 before/after 參考價回溯還原(price_adjust.py)。
    # 這裡**不直接放行** —— 仍對「還原後」的序列跑斷點掃描,因為自建還原只涵蓋
    # 除權息,分割/減資/面額變更不在 DividendResult 裡。殘留的大跳空正是那一類,
    # 而它們夠大、掃描看得到,所以對這個殘留類別而言掃描是有效的。
    if getattr(config, "SELF_ADJUST_PRICES", False):
        threshold = getattr(config, "PRICE_INTEGRITY_RETURN_THRESHOLD",
                            price_integrity.DEFAULT_DISCONTINUITY_THRESHOLD)
        frames = {}
        for sid in symbols:
            p = data.fetch_price(sid)          # 已還原
            if p is not None and not p.empty:
                frames[sid] = p
        audit = price_integrity.audit_price_frames(frames, threshold=threshold)
        if audit.empty:
            return
        out = config.OUTPUT_DIR / "price_integrity_audit.csv"
        try:
            audit.to_csv(out, index=False, encoding="utf-8-sig")
        except Exception:
            pass
        bad = sorted(audit["stock_id"].unique())
        if getattr(config, "ALLOW_UNADJUSTED_BACKTEST", False):
            print(f"[backtest] ⚠ 自建還原後仍有 {len(audit)} 筆殘留斷點(涉及 {len(bad)} 檔:"
                  f"{bad[:8]}{'…' if len(bad) > 8 else ''}),逃生門開啟故放行。"
                  f"這些多為分割/減資,不在除權息還原範圍內。")
            return
        raise RuntimeError(
            f"[fail-closed] 自建還原價後仍有 {len(audit)} 筆殘留斷點(門檻 {threshold:.0%},"
            f"涉及 {len(bad)} 檔),多為分割/減資/面額變更 —— 不在除權息還原範圍。\n"
            f"  審計明細:{out}\n"
            f"  解法:(a) 從候選池排除這些股票;(b) SWING_ALLOW_UNADJUSTED=1 放行"
            f"(結果標 integrity_bypassed);(c) 改用付費還原價資料集。"
        )
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
            f"[fail-closed] 資料集 {dataset} 是未還原價,主回測拒跑以免產出假 Sharpe。\n"
            f"  斷點審計(門檻 {threshold:.0%})命中 {len(audit)} 筆,已存:{out}\n"
            f"  註:審計只是診斷,不是放行條件。除息缺口約 3~5%,在 ±10% 漲跌停以下,"
            f"掃描結構上看不到 —— 命中 0 筆不代表價格乾淨。\n"
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


# ── PIT 候選池強制點(引擎邊界)────────────────────────────────────────────
# 2026-08-15:舊條件是「`universe_provider is None and dynamic_enabled and not
# sample and symbols is None` 才自動補上月 PIT provider」。問題是所有研究入口都
# 顯式傳 `symbols=`(全部來自 `universe.get_research_candidates()` 的單日靜態池),
# 所以那個「安全預設」一次都沒觸發過 —— 等於預設就是用今天的成交值排名回套歷史
# (AGENTS.md 陷阱 4 的選股 look-ahead),而程式看起來像有保護。
#
# 現在改成:引擎不再從 `symbols is None` 猜呼叫端意圖,呼叫端必須把意圖講清楚:
#   1. 正式歷史回測 → 傳 universe_provider(最短路徑 universes.historical_pit_universe)
#   2. legacy 單日池對照 → 顯式 static_universe_comparator=True(結果標為不可作正式證據)
#   3. smoke test → sample=True
# 三者都沒有就 raise,不再靜默退回靜態池。
def _static_comparator_provenance() -> Dict:
    """legacy 單日靜態池的誠實標籤(進 summary["universe"])。"""
    return {
        "candidate_pool_pit": False,
        "static_universe_comparator": True,
        "formal_evidence_eligible": False,
        "evidence_note": (
            "legacy 單一日期候選池(outputs/universe_top*.json):非 PIT,"
            "含選股 look-ahead,僅供對照,不可作正式證據"
        ),
    }


def _resolve_universe_source(symbols: Optional[List[str]], *,
                             sample: bool,
                             dynamic_enabled: bool,
                             universe_provider,
                             static_universe_comparator: bool,
                             caller: str,
                             external_picks: bool = False):
    """把候選池的來源與誠實標籤一次決定好。

    回傳 `(symbols, universe_provider, provenance)`;`provenance` 會併進
    `summary["universe"]`,讓「這段績效能不能當正式證據」寫在結果裡而不是靠記憶。

    `external_picks=True`(呼叫端自帶 picks_by_date)時引擎不建候選池,無法驗證
    PIT,所以不 raise,但一律標 `formal_evidence_eligible=False`(除非同時傳了
    provider),避免外部 picks 冒充 PIT 正式證據。
    """
    if universe_provider is not None and static_universe_comparator:
        raise ValueError(
            "universe_provider 與 static_universe_comparator 互斥:"
            "PIT 候選池與 legacy 單日池對照不可同時成立"
        )

    if universe_provider is not None:
        if not dynamic_enabled:
            raise ValueError("PIT universe_provider 只能搭配 dynamic_enabled=True")
        union = set(universe_provider.all_symbols)
        if symbols is None:
            symbols = sorted(union)
            n_excluded = 0
        else:
            extra = sorted(set(symbols) - union)
            if extra:
                raise ValueError(
                    f"[fail-closed] {caller}:symbols 有 {len(extra)} 檔不在 PIT 候選池"
                    f"聯集內(例:{extra[:3]})→ 候選池已不是由 PIT 規則決定。"
                    "只允許聯集的子集(唯一正當理由是資料品質黑名單)。"
                )
            n_excluded = len(union - set(symbols))
        provenance = {
            "candidate_pool_pit": True,
            "static_universe_comparator": False,
            "formal_evidence_eligible": True,
            "candidate_symbols_excluded": n_excluded,
        }
        return symbols, universe_provider, provenance

    if static_universe_comparator:
        return symbols, None, _static_comparator_provenance()

    if not sample and not external_picks:
        if dynamic_enabled:
            raise RuntimeError(
                f"[fail-closed] {caller}:dynamic universe 的正式歷史回測必須顯式提供"
                " PIT 候選池 provider。\n"
                "  正式做法:from universes import historical_pit_universe\n"
                "            pit = historical_pit_universe()\n"
                "            backtest.backtest_portfolio(**pit.backtest_kwargs(), ...)\n"
                "  legacy 單日池對照:顯式傳 static_universe_comparator=True"
                "(結果會標 formal_evidence_eligible=False,不可作正式證據)。\n"
                "  smoke test:sample=True。\n"
                "  為什麼會擋:舊版只在 symbols is None 時才自動補 provider,但每個研究"
                "入口都會傳 symbols,那個安全預設從未觸發 —— 預設值其實是把單日排名池"
                "回套歷史(選股 look-ahead)。"
            )
        # 關掉 dynamic universe 又不是 sample = legacy 單日候選池,同樣要顯式宣告
        # 成對照組,否則「靜態池」會不留痕跡地變成正式回測的候選池。
        raise RuntimeError(
            f"[fail-closed] {caller}:dynamic_enabled=False 等於用 legacy 單一日期"
            "候選池,必須顯式 static_universe_comparator=True 宣告成對照組"
            "(結果會標 formal_evidence_eligible=False);正式歷史回測請改走 "
            "universes.historical_pit_universe()。"
        )

    if external_picks and dynamic_enabled and not sample:
        # 候選池由呼叫端決定,引擎沒有辦法驗證它是不是 PIT → 誠實標記,不猜。
        return symbols, None, {
            "candidate_pool_pit": False,
            "static_universe_comparator": False,
            "formal_evidence_eligible": False,
            "evidence_note": (
                "picks_by_date 由呼叫端提供且未附 universe_provider:"
                "引擎無法驗證候選池是否 PIT;要作正式證據請傳 universe_provider"
            ),
        }

    return symbols, None, {
        "candidate_pool_pit": False,
        "static_universe_comparator": False,
        "formal_evidence_eligible": False,
        "evidence_note": (
            "sample smoke test" if sample else "static(非 dynamic universe)模式"
        ),
    }


# ── 預先計算所有股票的因子（含未來報酬）──────────────────────────────
def _prepare_panel(symbols: List[str], min_score_for_trade: float,
                   start_date: Optional[str], end_date: Optional[str],
                   dynamic_enabled: Optional[bool] = None,
                   universe_top_n: Optional[int] = None,
                   keep_non_members: bool = False,
                   universe_provider=None,
                   sample: bool = False,
                   static_universe_comparator: bool = False) -> pd.DataFrame:
    """
    把所有股票每一天的因子 + 綜合分數 + 未來N日報酬，攤平成一個大 panel。
    這個 panel 同時用於 (1) 整體回測選股 (2) 因子 IC 分析。
    """
    dynamic_enabled = (
        config.DYNAMIC_UNIVERSE_ENABLED
        if dynamic_enabled is None else bool(dynamic_enabled)
    )
    # 候選池來源的強制點:panel 就是候選池真正被「套用到歷史」的地方,所以閘門放這裡,
    # 直接呼叫 _prepare_panel 的研究腳本也一樣要講清楚意圖。
    symbols, universe_provider, universe_provenance = _resolve_universe_source(
        symbols, sample=sample, dynamic_enabled=dynamic_enabled,
        universe_provider=universe_provider,
        static_universe_comparator=static_universe_comparator,
        caller="_prepare_panel",
    )
    # 未還原價 fail-closed:任何走這個引擎的路徑（回測/IC/OOS/factor_audit/rotation）
    # 在未還原價且偵測到公司行動斷點時,先擋在這裡,不讓假績效產生。
    _assert_price_integrity(symbols)
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
    # 候選池真實建構日(provenance),非硬編快照日;取不到(舊池無 as_of)才退回快照。
    _pool_asof = _asof
    if dynamic_enabled:
        try:
            import build_universe as _bu
            _pool_asof = _bu.load_asof(universe_top_n) or _asof
        except Exception:
            pass
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
        "industry_asof": _asof,          # = 資料快照日(TaiwanStockInfo 以此戳快取)
        "candidate_pool_asof": _pool_asof,  # 候選池 json 的真實 as_of provenance
    }
    candidate_mask = None
    if universe_provider is not None:
        candidate_mask = universe_provider.candidate_mask(panel)
        universe_meta.update(universe_provider.metadata())
    # provenance 最後蓋上:誠實標籤不可被 provider metadata 或舊欄位覆寫。
    universe_meta.update(universe_provenance)
    if dynamic_enabled:
        ranked = dynamic_universe.add_membership(
            panel,
            top_n=universe_top_n,
            lookback=config.DYNAMIC_UNIVERSE_LOOKBACK,
            min_obs=config.DYNAMIC_UNIVERSE_MIN_OBS,
            min_avg_volume_lots=config.DYNAMIC_UNIVERSE_MIN_AVG_VOLUME_LOTS,
            min_avg_turnover=config.DYNAMIC_UNIVERSE_MIN_AVG_TURNOVER,
            candidate_mask=candidate_mask,
        )
        universe_meta.update({
            "top_n": universe_top_n,
            "lookback": config.DYNAMIC_UNIVERSE_LOOKBACK,
            "min_avg_volume_lots": config.DYNAMIC_UNIVERSE_MIN_AVG_VOLUME_LOTS,
            **dynamic_universe.membership_summary(ranked),
        })
        # keep_non_members:保留非成員列(+in_dynamic_universe 旗標),讓 operator
        # 型因子能在「連續」個股序列上算 ts_(避免只在稀疏成員日 rolling 的失真);
        # IC/選股仍應自行過濾 in_dynamic_universe。預設維持舊行為(只留成員)。
        panel = ranked if keep_non_members else ranked[ranked["in_dynamic_universe"]].copy()
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
                       picks_by_date: Optional[Dict] = None,
                       let_positions_run: bool = False,
                       rebalance_phase: int = 0,
                       universe_provider=None,
                       static_universe_comparator: bool = False) -> Dict:
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
    if rebalance_every <= 0:
        raise ValueError("rebalance_every 必須為正整數")
    if not 0 <= rebalance_phase < rebalance_every:
        raise ValueError(
            f"rebalance_phase 必須在 [0, {rebalance_every - 1}]，目前為 {rebalance_phase}"
        )
    # 候選池來源:不再從 symbols is None 猜意圖(舊版那條安全預設從未觸發過)。
    external_picks = picks_by_date is not None
    symbols, universe_provider, universe_provenance = _resolve_universe_source(
        symbols, sample=sample, dynamic_enabled=dynamic_enabled,
        universe_provider=universe_provider,
        static_universe_comparator=static_universe_comparator,
        caller="backtest_portfolio", external_picks=external_picks,
    )
    if symbols is None:
        symbols = uni.get_universe(sample=sample)

    max_positions = config.BT_MAX_POSITIONS

    # 每檔 price（含 MA_EXIT 供 trend 退場），date -> 列索引
    price_cache: Dict[str, pd.DataFrame] = {}
    date_idx_map: Dict[str, Dict] = {}
    price_limit_source = getattr(config, "BT_PRICE_LIMIT_SOURCE", "derived_prev_close")
    for sid in symbols:
        p = data.fetch_price(sid)
        if p is None or p.empty:
            continue
        p = p.reset_index(drop=True)
        if price_limit_source == "official":
            limits = data.fetch_price_limits(sid)
            if limits is None or limits.empty:
                raise RuntimeError(
                    f"{sid} 指定 official 漲跌停資料，但 TaiwanStockPriceLimit 為空"
                )
            p = p.merge(limits, on="date", how="left", validate="one_to_one")
            # 自建 back-adjust 後，官方原始價格也要乘同一因子才可與 OHLC 比較。
            if "adj_factor" in p.columns:
                for col in ("reference_price", "limit_up", "limit_down"):
                    p[col] = pd.to_numeric(p[col], errors="coerce") * p["adj_factor"]
            elif getattr(config, "PRICE_DATASET", "TaiwanStockPrice") == "TaiwanStockPriceAdj":
                raise RuntimeError(
                    f"{sid} 使用供應商還原價但沒有逐日 adj_factor，無法把官方原始"
                    "漲跌停價對齊到同一尺度"
                )
            required = {"reference_price", "limit_up", "limit_down"}
            missing = required - set(p.columns)
            if missing:
                raise RuntimeError(
                    f"{sid} 指定 official 漲跌停資料，但價格列缺少 {sorted(missing)}；"
                    "拒絕把 derived_prev_close 冒充官方資料"
                )
            exempt = p.get("price_limit_exempt", pd.Series(False, index=p.index)).fillna(False)
            covered = p["reference_price"].notna() & (
                (p["limit_up"].notna() & p["limit_down"].notna()) | exempt.astype(bool)
            )
            if not bool(covered.all()):
                bad_dates = p.loc[~covered, "date"].astype(str).str[:10].head(3).tolist()
                raise RuntimeError(
                    f"{sid} official 漲跌停資料覆蓋不完整，例：{bad_dates}；"
                    "拒絕靜默退回昨日收盤推導"
                )
        p["ma_exit"] = p["close"].rolling(config.BT_MA_EXIT).mean()
        price_cache[sid] = p
        date_idx_map[sid] = {d: i for i, d in enumerate(p["date"])}

    # picks_by_date 可由外部注入（例如 sector_rotation：族群輪動選股）。外部注入時
    # 跳過 composite/趨勢過濾（呼叫端自理），並用價格快取的交易日曆當 all_dates，
    # 避免重建整個 panel（省時、且不受 FACTOR_WEIGHTS 影響）。
    if not external_picks:
        panel = _prepare_panel(
            symbols, config.MIN_COMPOSITE, start_date, end_date,
            dynamic_enabled=dynamic_enabled,
            universe_top_n=universe_top_n,
            universe_provider=universe_provider,
            sample=sample,
            static_universe_comparator=static_universe_comparator,
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
        # ── 評估窗上界:預設截到最後一個訊號日 ─────────────────────────
        # 2026-08-03 修:過去沒給 end_date 就一路跑到「價格快取的末端」,而快取
        # 涵蓋全部凍結資料。做 IS 評估時只限制 picks 的日期是**不夠的** ——
        # 訊號用完後既有部位仍持續持有並 MTM,等於把 IS 之後的行情算進 IS。
        # 實測 S19:IS 權益曲線超出切點 144 天,把 OS 段的 +87.2% 算進「IS Sharpe」,
        # 讓 1.607 看起來成立(真實 IS 只有 0.306)。而且用它選出的參數也連帶失效。
        #
        # 安全預設 = min(end_date 或最後訊號日)。要看完整交易生命週期(讓部位
        # 自然出場)請顯式傳 let_positions_run=True。
        picks_end = max(picks_by_date)
        hard_end = pd.to_datetime(end_date) if end_date else None
        if not let_positions_run:
            bound = picks_end if hard_end is None else min(hard_end, picks_end)
            if hard_end is None and any(d > picks_end for d in all_dates):
                print(f"[backtest] 評估窗截到最後訊號日 {str(picks_end)[:10]}"
                      f"(未指定 end_date)。要讓部位跑到自然出場請設 let_positions_run=True。")
            all_dates = [d for d in all_dates if d <= bound]
        elif hard_end is not None:
            all_dates = [d for d in all_dates if d <= hard_end]
    # universe 資訊:external picks 路徑沒有 panel,給一個安全的 metadata（避免
    # summary 讀 panel.attrs 時 UnboundLocalError）。誠實標籤沿用同一組。
    _asof = getattr(config, "SNAPSHOT_END_DATE", "") or "live"
    if external_picks:
        universe_info = {
            "enabled": dynamic_enabled, "direction": "long_only",
            "candidate_source": "external_picks_by_date",
            "survivorship_free": False, "industry_pit": False,
            "industry_asof": _asof, "candidate_pool_asof": _asof,
        }
        # 呼叫端自建 panel/picks 但仍傳了真正的 PIT provider(S19 就是這樣)時,
        # summary 必須保留 provider 的真實 metadata —— 否則正式策略的候選池規則、
        # pool as-of 會在結果裡被寫成 "external_picks_by_date" 這種空白標籤,
        # 之後沒人能從 summary 判斷這段績效的候選池到底是不是 PIT。
        if universe_provider is not None:
            universe_info["picks_source"] = "external_picks_by_date"
            universe_info.update(universe_provider.metadata())
        universe_info.update(universe_provenance)
    else:
        universe_info = panel.attrs.get("universe", {
            "enabled": dynamic_enabled, "direction": "long_only",
            "top_n": universe_top_n if dynamic_enabled else None,
            **universe_provenance,
        })

    # ── 市場濾網 overlay 狀態（預設關；開啟才作用，不影響 FACTOR_WEIGHTS）──
    filter_on = bool(getattr(config, "MARKET_FILTER_ENABLED", False))
    riskoff_map = market_riskoff_map() if filter_on else {}
    riskoff_weight = float(getattr(config, "MARKET_FILTER_RISKOFF_WEIGHT", 0.0))
    n_filter_exits = 0
    n_regime_switches = 0
    n_limit_skip = 0            # 因一字漲停買不到而跳過的進場數
    n_disp_skip = 0            # 因處置期間禁新倉而跳過的進場數
    n_stale_exits = 0          # 顯式 recovery 假設下的疑似下市/長停牌結算數
    n_lot_skip = 0             # 分配資金不足一個合法交易單位
    disp_days = _load_disposition_days(all_dates)   # {sid -> set(處置交易日)}
    _prev_riskoff = False

    # 投組狀態
    initial_capital = float(getattr(config, "BT_INITIAL_CAPITAL", 1_000_000.0))
    order_size_mode = OrderSizeMode(
        getattr(config, "BT_ORDER_SIZE_MODE", OrderSizeMode.RESEARCH_FRACTIONAL.value))
    cost_model = TaiwanStockCostModel(
        commission_rate=config.BT_FEE,
        minimum_commission=getattr(config, "BT_MIN_COMMISSION", 0.0),
        sell_tax_rate=config.BT_TAX,
    )
    equity = initial_capital
    cash = initial_capital
    positions: Dict[str, dict] = {}   # sid -> 部位
    equity_curve = []                 # (date, equity)
    trades = []

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
                # 缺 bar：短期停牌續抱;但長期缺 bar(下市/長停牌)= 殭屍部位,不能
                # 永遠凍結在 last_close、佔住部位槽、逃過所有出場判定(_check_exit 只在
                # 有 bar 時才呼叫)。超過 BT_STALE_EXIT_DAYS 個交易日沒 bar → 視為下市,
                # 以最後已知收盤強制平倉(survivorship-free 重跑時才不會忽略下市虧損)。
                stale = di - pos.get("last_bar_di", pos.get("entry_di", di))
                if stale >= config.BT_STALE_EXIT_DAYS:
                    recovery = getattr(config, "BT_DELIST_RECOVERY", None)
                    if recovery is None:
                        raise RuntimeError(
                            f"{sid} 已連續 {stale} 個市場交易日無 bar（疑似長停牌/下市）；"
                            "沒有正式清算資料，拒絕假設可用最後收盤賣出。"
                            "可用 SWING_DELIST_RECOVERY=0~1 做明確敏感度測試"
                        )
                    exit_price = pos["last_close"] * float(recovery)
                    proceeds = float(cost_model.sell_proceeds(pos["shares"], exit_price))
                    cash += proceeds
                    gross = (exit_price - pos["entry_price"]) / pos["entry_price"]
                    net = proceeds / pos["cost"] - 1.0
                    trades.append({
                        "stock_id": sid, "name": pos["name"],
                        "signal_date": pos["signal_date"],
                        "entry_date": pos["entry_date"], "exit_date": d,
                        "entry_price": round(pos["entry_price"], 2),
                        "exit_price": round(exit_price, 2),
                        "shares": pos["shares"],
                        "entry_cost": round(pos["cost"], 2),
                        "exit_proceeds": round(proceeds, 2),
                        "hold_bars": di - pos.get("entry_di", di),
                        "gross_ret": round(gross, 4),
                        "ret": round(net, 4),
                        "exit_reason": "stale_delisted",
                        "composite": round(pos["composite"], 2),
                    })
                    del positions[sid]
                    n_stale_exits += 1
                continue  # 當天該股沒資料(未達門檻)→ 續抱
            if idx <= pos["entry_idx"]:
                continue  # 進場當天不在這裡出（出場判定從進場日的 _check_exit 已含）
            # 一字跌停：賣不掉,今日不成交,順延到下一個能成交日(被迫續抱,虧損擴大)。
            if config.BT_MODEL_LIMIT_LOCK and idx > 0:
                pc = price_cache[sid]["close"].iloc[idx - 1]
                if _limit_lock(bar, float(pc)) == "down":
                    continue
            pos["ma_exit_today"] = float(bar["ma_exit"]) if "ma_exit" in bar else np.nan
            days_held = idx - pos["entry_idx"]
            ex = _check_exit(bar, pos, days_held)
            if ex is not None:
                exit_price, reason = ex
                proceeds = float(cost_model.sell_proceeds(pos["shares"], exit_price))
                cash += proceeds
                gross = (exit_price - pos["entry_price"]) / pos["entry_price"]
                net = proceeds / pos["cost"] - 1.0
                trades.append({
                    "stock_id": sid, "name": pos["name"],
                    "signal_date": pos["signal_date"],
                    "entry_date": pos["entry_date"], "exit_date": d,
                    "entry_price": round(pos["entry_price"], 2),
                    "exit_price": round(exit_price, 2),
                    "shares": pos["shares"],
                    "entry_cost": round(pos["cost"], 2),
                    "exit_proceeds": round(proceeds, 2),
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
                proceeds = float(cost_model.sell_proceeds(pos["shares"], exit_price))
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
                    "shares": pos["shares"],
                    "entry_cost": round(pos["cost"], 2),
                    "exit_proceeds": round(proceeds, 2),
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
        if di > 0 and (di - 1 - rebalance_phase) % rebalance_every == 0:
            signal_date = all_dates[di - 1]
            candidates = picks_by_date.get(signal_date, [])[:top_n]
            entry_cap = target_positions if filter_on else max_positions
            for sid, comp, name in candidates:
                if len(positions) >= entry_cap:
                    break
                if sid in positions:
                    continue  # 已持有不重複買
                if disp_days and d in disp_days.get(sid, ()):
                    n_disp_skip += 1
                    continue  # 處置期間(分盤+預收款券)→ 禁新倉
                bar, idx = _price_row(sid, d)
                if bar is None or idx == 0:
                    continue
                # 一字漲停:委買遠大於委賣,實務買不到 → 跳過此候選(不佔幻想成交)。
                if config.BT_MODEL_LIMIT_LOCK:
                    if _limit_lock(bar, float(price_cache[sid]["close"].iloc[idx - 1])) == "up":
                        n_limit_skip += 1
                        continue
                entry_price = float(bar["open"])
                if not np.isfinite(entry_price) or entry_price <= 0:
                    continue
                alloc = equity / max_positions
                if cash < alloc * 0.5:
                    break  # 現金不足
                alloc = min(alloc, cash)
                shares, total_cost = size_long_order(
                    alloc,
                    entry_price,
                    mode=order_size_mode,
                    costs=cost_model,
                    regular_lot_shares=getattr(config, "BT_REGULAR_LOT_SHARES", 1000),
                )
                if shares <= 0:
                    n_lot_skip += 1
                    continue
                cash -= total_cost
                positions[sid] = {
                    "name": name, "composite": float(comp),
                    "signal_date": signal_date, "entry_date": d,
                    "entry_idx": idx, "entry_price": entry_price,
                    "cost": total_cost, "shares": shares,
                    "ma_exit_today": np.nan,
                    "pending_ma_exit": False,
                    "last_close": entry_price,      # MTM 缺 bar 時延用最後收盤(見下)
                    "entry_di": di, "last_bar_di": di,  # 全域日索引,供缺bar殭屍出場
                }

        # ── 3) 收盤 mark-to-market：投組淨值 = 現金 + 各部位市值 ──────
        # 缺 bar(停牌/被清理列)時延用「最後一次已知收盤」,不回退成本價——回退成本
        # 會讓權益曲線在缺 bar 日假跳到成本、隔日跳回,灌大波動/回撤;且下市股不會
        # 被凍結在成本價(那在 survivorship-free 重跑時會變成忽略下市虧損的樂觀偏誤)。
        mtm = cash
        for sid, pos in positions.items():
            bar, _ = _price_row(sid, d)
            if bar is not None:
                pos["last_close"] = float(bar["close"])
                pos["last_bar_di"] = di        # 記最後有 bar 的日,供缺bar殭屍出場計齡
            mtm += pos["shares"] * pos["last_close"]
        equity = mtm
        equity_curve.append((d, equity))

    # ── 結算：用每日淨值算正確的績效指標 ────────────────────────────
    if not trades and len(positions) == 0:
        return {"error": "回測期間無任何交易（可能門檻太高或樣本太少）", "n_trades": 0}

    eq = pd.DataFrame(equity_curve, columns=["date", "equity"]).set_index("date")
    daily_ret = eq["equity"].pct_change().dropna()

    cum_ret = float(eq["equity"].iloc[-1] / initial_capital - 1.0)
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
        "limit_lock": {
            "modeled": config.BT_MODEL_LIMIT_LOCK,
            "n_entries_skipped_limit_up": n_limit_skip,
        },
        "disposition": {
            "modeled": bool(disp_days),
            "n_entries_skipped_disposition": n_disp_skip,
        },
        "execution": {
            "rules_version": "tw-stock-2015-06-01",
            "order_size_mode": order_size_mode.value,
            "lot_aware": order_size_mode == OrderSizeMode.REGULAR_LOT,
            "price_limit_source": getattr(
                config, "BT_PRICE_LIMIT_SOURCE", "derived_prev_close"),
            "price_and_lot_realistic": (
                order_size_mode == OrderSizeMode.REGULAR_LOT
                and getattr(config, "BT_PRICE_LIMIT_SOURCE", "derived_prev_close") == "official"
            ),
            # 日線仍無法重建排隊、盤中穩定措施、處置分盤與完整交割帳本；在這些
            # 元件完成前，不得因股數和漲跌停正確就宣稱 execution 已完整真實。
            "execution_realistic": False,
            "unmodeled_components": [
                "intraday_queue_and_price_stabilization",
                "disposition_batch_fill_probability",
                "full_delivery_cash_precollection",
                "t_plus_2_settlement_ledger",
            ],
            "initial_capital": initial_capital,
            "regular_lot_shares": getattr(config, "BT_REGULAR_LOT_SHARES", 1000),
            "commission_rate": float(cost_model.commission_rate),
            "minimum_commission": float(cost_model.minimum_commission),
            "sell_tax_rate": float(cost_model.sell_tax_rate),
            "n_entries_skipped_lot_size": n_lot_skip,
            "odd_lot_fill_warning": (
                "使用普通交易日線開盤價代理零股成交價"
                if order_size_mode == OrderSizeMode.ODD_LOT_PROXY else None
            ),
        },
        "delisting": {
            "recovery_assumption": getattr(config, "BT_DELIST_RECOVERY", None),
            "n_stale_exits": n_stale_exits,
        },
        "period": [str(all_dates[0])[:10], str(all_dates[-1])[:10]],
        # 評估窗稽核:讓「回測實際跑了哪段」可被檢查,而不是只能從 Sharpe 猜。
        # picks_window 是訊號涵蓋的期間;eval_window 是引擎實際 MTM 的期間。
        # 兩者的右界不一致 = 訊號用完後仍在計績效(見 external picks 分支的說明)。
        "eval_audit": {
            "picks_window": ([str(min(picks_by_date))[:10], str(max(picks_by_date))[:10]]
                             if picks_by_date else None),
            "eval_window": [str(all_dates[0])[:10], str(all_dates[-1])[:10]],
            "days_beyond_last_pick": (
                int(sum(1 for d in all_dates if d > max(picks_by_date)))
                if picks_by_date else 0
            ),
            "let_positions_run": bool(let_positions_run),
        },
        "market_filter": {
            "enabled": filter_on,
            "rule": config.MARKET_FILTER_RULE if filter_on else None,
            "riskoff_weight": riskoff_weight if filter_on else None,
            "n_filter_exits": n_filter_exits,
            "n_regime_switches": n_regime_switches,
        },
        "universe": universe_info,
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
            "rebalance_phase": rebalance_phase,
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
              universe_top_n: Optional[int] = None,
              universe_provider=None,
              static_universe_comparator: bool = False) -> pd.DataFrame:
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
    symbols, universe_provider, _ = _resolve_universe_source(
        symbols, sample=sample, dynamic_enabled=dynamic_enabled,
        universe_provider=universe_provider,
        static_universe_comparator=static_universe_comparator,
        caller="factor_ic",
    )
    if symbols is None:
        symbols = uni.get_universe(sample=sample)
    panel = _prepare_panel(
        symbols, 0.0, start_date, end_date,
        dynamic_enabled=dynamic_enabled,
        universe_top_n=universe_top_n,
        universe_provider=universe_provider,
        sample=sample,
        static_universe_comparator=static_universe_comparator,
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
            if u.get("candidate_membership_survivorship_free", False):
                print("  ⚠ 候選名單已 PIT；但下市股完整還原價格覆蓋尚未證明，"
                      "整體仍不標 survivorship-free")
            elif str(u.get("candidate_source", "")).startswith("saved_current_"):
                print("  ⚠ 候選池仍是 current-pool bootstrap；不可作正式歷史結論")
            else:
                print("  ⚠ universe／價格歷史尚未證明完整 survivorship-free")
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
             universe_top_n: Optional[int] = None,
             static_comparator: bool = False):
    """一次跑完整體回測 + 因子IC，並印報告。

    `static_comparator=True` = 關掉 dynamic universe、用 legacy 單日靜態池跑對照組。
    這條路徑刻意保留(它是偏誤對照組),但結果會在 summary 標
    `formal_evidence_eligible=False`,不可當正式證據。
    """
    dynamic_enabled = (
        config.DYNAMIC_UNIVERSE_ENABLED
        if dynamic_enabled is None else bool(dynamic_enabled)
    )
    # 小型 sample 是 smoke test，不冒充動態全市場研究。
    effective_dynamic = dynamic_enabled and not sample
    universe_top_n = universe_top_n or config.DYNAMIC_UNIVERSE_TOP_N
    universe_provider = None
    # 非 sample 又關掉 dynamic universe = legacy 單日池;必須顯式宣告成對照組,
    # 否則單日排名池會被當成正式歷史候選池(選股 look-ahead)。
    static_comparator = bool(static_comparator) or (
        not dynamic_enabled and not sample
    )
    if effective_dynamic:
        # 正式歷史回測的最短路徑:月頻 PIT 候選池。
        pit = historical_pit_universe(candidate_pool_n=pool)
        universe_provider = pit.provider
        symbols = pit.symbols
    elif pool:
        symbols = uni.get_universe(top_n=pool)
    else:
        symbols = uni.get_universe(sample=sample)
    print(f"\n[backtest] universe = {len(symbols)} 檔，建立統一 IS/OS 交易日曆...\n")
    if static_comparator and not sample:
        print("[backtest] ⚠ static comparator 模式:legacy 單一日期候選池,非 PIT,"
              "含選股 look-ahead —— 僅供對照,不可作正式證據。\n")

    static_flag = static_comparator and not effective_dynamic
    # 全期只用來取得可交易日曆，不展示或拿來選參數。
    calendar_res = backtest_portfolio(
        symbols=symbols, sample=sample,
        rebalance_every=rebalance_every, top_n=top_n,
        dynamic_enabled=effective_dynamic,
        universe_top_n=universe_top_n,
        universe_provider=universe_provider,
        static_universe_comparator=static_flag,
    )
    if "equity_curve" not in calendar_res:
        _print_bt_summary(calendar_res)
        return {"error": calendar_res.get("error", "無法建立交易日曆")}, {}
    split = evaluation_split.build_evaluation_split(
        calendar_res["equity_curve"]["date"],
        minimum_embargo_days=config.BT_IC_HORIZON,
    )
    print(f"[backtest] split={split.mode}｜IS {split.is_window[0]}~{split.is_window[1]} "
          f"({split.n_is}日)｜embargo {split.n_embargo}日｜"
          f"OS {split.os_window[0]}~{split.os_window[1]} ({split.n_os}日)")
    print(f"[backtest] 每段跑滿 {rebalance_every} 個等價再平衡相位；決策看中位數與最小值。\n")

    phase_rows = []
    trade_frames = []
    results = {}
    for segment, (start, end) in {"IS": split.is_window,
                                  "OS": split.os_window}.items():
        for phase in range(rebalance_every):
            res = backtest_portfolio(
                symbols=symbols, sample=sample,
                start_date=start, end_date=end,
                rebalance_every=rebalance_every,
                rebalance_phase=phase,
                top_n=top_n,
                dynamic_enabled=effective_dynamic,
                universe_top_n=universe_top_n,
                universe_provider=universe_provider,
                static_universe_comparator=static_flag,
            )
            results[(segment, phase)] = res
            if "summary" not in res:
                phase_rows.append({"segment": segment, "phase": phase,
                                   "error": res.get("error", "?")})
                continue
            summary = res["summary"]
            actual_end = pd.Timestamp(summary["eval_audit"]["eval_window"][1])
            if actual_end > pd.Timestamp(end):
                raise RuntimeError(
                    f"{segment} phase={phase} 評估窗溢出 {actual_end.date()} > {end}"
                )
            phase_rows.append({
                "segment": segment, "phase": phase,
                "n_trades": summary["n_trades"],
                "ann_ret": summary["ann_ret"], "sharpe": summary["sharpe"],
                "max_drawdown": summary["max_drawdown"],
                "cum_ret": summary["cum_ret"],
                "integrity_bypassed": summary["data"]["integrity_bypassed"],
                "survivorship_free": summary["universe"].get("survivorship_free", False),
                "eval_end": summary["eval_audit"]["eval_window"][1],
            })
            if "trades" in res and not res["trades"].empty:
                trades = res["trades"].copy()
                trades["segment"] = segment
                trades["rebalance_phase"] = phase
                trade_frames.append(trades)

    phase_df = pd.DataFrame(phase_rows)
    print("=" * 94)
    print(f"  {'段':<4}{'相位':>5}{'交易':>7}{'年化':>10}{'Sharpe':>10}{'MaxDD':>10}{'累積':>10}")
    print("-" * 94)
    valid = phase_df[phase_df.get("error").isna()] if "error" in phase_df else phase_df
    for _, row in valid.iterrows():
        print(f"  {row['segment']:<4}{int(row['phase']):>5}{int(row['n_trades']):>7}"
              f"{row['ann_ret']:>10.1%}{row['sharpe']:>10.2f}"
              f"{row['max_drawdown']:>10.1%}{row['cum_ret']:>10.1%}")
    print("-" * 94)
    for segment, group in valid.groupby("segment", sort=False):
        print(f"  {segment} 相位摘要：Sharpe 中位 {group['sharpe'].median():.2f} / "
              f"最小 {group['sharpe'].min():.2f}；MaxDD 最差 {group['max_drawdown'].min():.1%}")
    print("=" * 94)

    ic_results = {}
    for segment, (start, end) in {"IS": split.is_window,
                                  "OS": split.os_window}.items():
        print(f"\n[{segment}] 因子 IC {start}~{end}")
        ic = factor_ic(
            symbols=symbols, sample=sample,
            start_date=start, end_date=end,
            dynamic_enabled=effective_dynamic,
            universe_top_n=universe_top_n,
            universe_provider=universe_provider,
            static_universe_comparator=static_flag,
        )
        ic_results[segment] = ic
        _print_ic(ic)

    phase_path = config.OUTPUT_DIR / "backtest_phase_summary.csv"
    phase_df.to_csv(phase_path, index=False, encoding="utf-8-sig")
    print(f"\n  相位摘要已存：{phase_path}")
    if trade_frames:
        path = config.OUTPUT_DIR / "backtest_trades.csv"
        pd.concat(trade_frames, ignore_index=True).to_csv(
            path, index=False, encoding="utf-8-sig"
        )
        print(f"  交易明細已存：{path}")
    for segment, ic in ic_results.items():
        if not ic.empty:
            path = config.OUTPUT_DIR / f"factor_ic_{segment.lower()}.csv"
            ic.to_csv(path, index=False, encoding="utf-8-sig")
            print(f"  {segment} 因子IC已存：{path}")

    return {"split": split.to_dict(), "phases": phase_df,
            "results": results}, ic_results


if __name__ == "__main__":
    run_full(sample=True, top_n=3, rebalance_every=5)
