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

import re
from typing import Any, List, Mapping, Optional, Dict

import numpy as np
import pandas as pd

import config
import data
import dynamic_universe
import evaluation_split
import factors
import price_integrity
import provenance
import return_convention
import security_type
import universe as uni
# 相位掃描只有一份實作(evaluation/phases.py)。正式 IS/OS、S19 的 evaluate 與
# forward_test 都走它;這裡不再自己寫 `for phase in range(...)`。
from evaluation import holdout as holdout_ledger
from evaluation.phases import combine as phase_combine
from evaluation.phases import sweep_phases
from factor_engine import panel_density
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

    `external_picks=True`(呼叫端自帶 picks_by_date)時引擎不建候選池,所以不
    raise,但一律標 `formal_evidence_eligible=False`。同時傳了 provider 時,這裡
    給的 PIT 標籤只是**待驗**的:真正的驗證在 `backtest_portfolio` 的
    `_verify_external_picks_are_pit`(逐日比對候選遮罩)與 `strategy_spec` 閘門,
    兩者都可能把這組樂觀標籤蓋回 False。這裡不驗是因為此時還沒有 picks 與
    評估窗;但**不可以**把「有 provider 物件」直接當成驗過了。
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


def _downgrade_formal_evidence(universe_meta: Dict[str, Any], reason: str) -> None:
    """就地降級 `formal_evidence_eligible`,理由**累加**進 `evidence_note`。

    降級理由只能加不能蓋:同一段績效可能同時踩到未來池、非 PIT picks、缺策略
    規格,只留最後一條會讓 summary 讀起來像只有一個問題。
    """
    universe_meta["formal_evidence_eligible"] = False
    note = str(universe_meta.get("evidence_note") or "").strip()
    universe_meta["evidence_note"] = f"{note}｜{reason}" if note else reason


def _verify_external_picks_are_pit(picks_by_date: Dict, universe_provider, *,
                                   dates_in_scope, caller: str) -> Dict[str, Any]:
    """external picks 必須逐日落在 provider 的候選遮罩內,才算 PIT 候選池。

    原 bug(2026-08-15 修):`_resolve_universe_source` 只要「傳了 provider 物件」
    就蓋上 `candidate_pool_pit=True` / `formal_evidence_eligible=True`,而
    `candidate_mask()` 只在 `_prepare_panel` 裡被呼叫 —— external picks 分支
    (S19 唯一實際走的路徑)一次都不驗。唯一的檢查是 `symbols ⊆ all_symbols`,
    那是**跨全期的聯集**,不是逐月 PIT 成員。實測:provider 的 2 月池=[A]、
    3 月池=[B],傳 `symbols=['A']` + 全部落在三月的 'A' picks(A 在三月池外),
    summary 仍得到 `candidate_pool_pit=True`、`formal_evidence_eligible=True`、
    `candidate_pool_asof='2026-03-31'`。P0-3 想根除的「閘門靠呼叫端記得傳對參數」
    因此原封復發,只是從『傳 symbols 就退回靜態池』變成『傳 provider 就蓋 PIT 章』。

    回傳要蓋在 `summary["universe"]` 上的欄位:
      - 全部 picks 都在當日候選池內 → 空 dict(維持 provider 給的 PIT 標籤)。
      - provider 對某些日期沒有池(例如早於 PIT 歷史起點)→ 不 raise,但把
        `candidate_pool_pit` 降為 False 並寫理由(不知道就說不知道)。
      - 有 pick 落在當日候選池外 → fail-closed raise。
    """
    scope = None if dates_in_scope is None else set(dates_in_scope)
    rows = [(d, str(sid))
            for d, picks in (picks_by_date or {}).items()
            if scope is None or d in scope
            for sid, *_rest in (picks or ())]
    if not rows:
        # 評估窗內一筆 picks 都沒用到 → 什麼都沒驗過,不能沿用樂觀標籤。
        return {
            "candidate_pool_pit": False,
            "candidate_pool_pit_verified": False,
            "formal_evidence_eligible": False,
            "evidence_note": (
                f"{caller}:評估窗內沒有任何 picks,候選池的 PIT 未被驗證"),
        }

    mask_fn = getattr(universe_provider, "candidate_mask", None)
    if mask_fn is None:
        return {
            "candidate_pool_pit": False,
            "candidate_pool_pit_verified": False,
            "formal_evidence_eligible": False,
            "evidence_note": (
                f"{caller}:universe_provider 沒有 candidate_mask(),"
                "無法逐日驗證 external picks 是否為 PIT 候選"),
        }

    frame = pd.DataFrame(rows, columns=["date", "stock_id"])
    mask = np.asarray(mask_fn(frame)).astype(bool)

    # 「當日根本沒有池」與「pick 不在池裡」在遮罩上都是 False,必須分開處理:
    # 前者是引擎不知道(降級),後者是候選池被違反(raise)。
    members_on = getattr(universe_provider, "members_on", None)
    uncovered = set()
    if members_on is not None:
        for day in frame["date"].unique():
            if not list(members_on(day)):
                uncovered.add(day)
    outside = frame[~mask]
    violations = outside[~outside["date"].isin(uncovered)]
    if not violations.empty:
        sample_rows = [
            f"{str(pd.Timestamp(d).date())}/{s}"
            for d, s in violations.head(3).itertuples(index=False)
        ]
        raise ValueError(
            f"[fail-closed] {caller}:picks_by_date 有 {len(violations)} 筆"
            f"(共 {violations['stock_id'].nunique()} 檔)落在當日 PIT 候選池外"
            f"(例:{sample_rows})→ 候選池已不是由 PIT 規則決定,"
            "拒絕在 summary 蓋上 candidate_pool_pit=True。"
        )
    if uncovered:
        days = sorted(str(pd.Timestamp(d).date()) for d in uncovered)
        return {
            "candidate_pool_pit": False,
            "candidate_pool_pit_verified": False,
            "formal_evidence_eligible": False,
            "evidence_note": (
                f"{caller}:有 {len(days)} 個訊號日(例 {days[:3]})落在 PIT "
                "候選池的生效範圍外,引擎無法驗證這些日子的 picks 是否 PIT"),
        }
    return {"candidate_pool_pit_verified": True,
            "candidate_pool_picks_checked": int(len(frame))}


# ── 候選池 provenance:pool as-of 必須來自「真的被用到的那份池」────────────
_POOL_FILE_RE = re.compile(r"^universe_top(\d+)\.json$")


def _available_pool_sizes() -> List[int]:
    """`outputs/` 底下現存的 legacy 候選池檔(top-N)。"""
    try:
        names = [p.name for p in config.OUTPUT_DIR.glob("universe_top*.json")]
    except Exception:
        return []
    sizes = [int(m.group(1)) for m in map(_POOL_FILE_RE.match, names) if m]
    return sorted(set(sizes))


def _legacy_pool_provenance(symbols: Optional[List[str]], *,
                            dynamic_enabled: bool,
                            universe_top_n: int) -> Dict[str, Any]:
    """legacy 單日候選池的**真實** provenance(讀哪一份檔、它的 as_of 是哪天)。

    原 bug(2026-08-15 修):`_prepare_panel` 寫的是

        _pool_asof = build_universe.load_asof(universe_top_n)

    但 `universe_top_n` 是**每日 dynamic universe 的 top-N(100)**,不是候選池。
    真正被套進歷史的候選池是 `universe.get_research_candidates()` 讀的
    `outputs/universe_top{DYNAMIC_UNIVERSE_CANDIDATE_POOL}.json`(300)。實測:
    top100 的 `as_of=2026-06-20`(<= 快照 2026-06-22,看起來完全合規),top300 的
    `as_of=2026-08-03`(> 快照 = 未來池 look-ahead),而同一份 metadata 的
    `candidate_source` 卻誠實寫著 top300。summary 因此自我矛盾,而且是往
    「看起來合規」的方向錯 —— 最危險的那個方向:未來池的績效會被當成乾淨結果。

    修法是不再用「每日 top-N」推測,而是用**實際的 symbols** 去比對現存的池檔:
    只有當 symbols 是某份池的子集(子集是正當的:資料品質黑名單會扣掉幾檔)才
    採用該檔的 as_of。比對不到就誠實回報 `candidate_pool_asof=None`,不拿快照日
    或別份池的日期頂替 —— 頂替出來的戳只會讓人誤以為 PIT 已被驗證過。

    第二個 bug(2026-08-15 同日修):第一版的比對迴圈是
    `for n in sorted({expected, *_available_pool_sizes()})`,由**小到大**找第一個
    superset,理由寫成「最小的 superset 就是實際用的那一份」。那個推論只在
    `symbols == 整份池`(頂多扣黑名單)時成立;對手挑清單或縮小樣本一律不成立。
    實測:daily top3(as_of 2026-01-05)、真候選池 top5(as_of 2026-08-03)、快照
    2026-03-31,傳 `symbols=ids[:2]`(兩份池的共同子集)會解析到 top3,summary 戳
    上一個 <= 快照的**看起來合規**的日期,`future_pool_bypassed` 連帶變成 False
    —— 也就是同一類錯(未來池冒充乾淨池)只是換了觸發條件。

    所以順序改成:**先試 `expected`**(config 宣告的候選池,唯一有理由相信的那
    一份);只有 expected 不涵蓋時才往其他池找,而且此時解析結果本身就是猜的
    → `candidate_pool_asof` 回 None、標 `candidate_pool_asof_ambiguous=True` 並
    降級。所有被考慮過的池的 as_of 都記進 `candidate_pool_asof_candidates`,
    未來池偵測會逐一比對(解析歪掉不該讓偵測整條失效)。
    """
    expected = int(
        getattr(config, "DYNAMIC_UNIVERSE_CANDIDATE_POOL", universe_top_n)
        if dynamic_enabled else universe_top_n
    )
    wanted = set(symbols or ())
    import build_universe as _bu

    def _ids(n: int) -> Optional[set]:
        """某份池檔的成員;檔案壞掉/不存在就當作沒有(不放棄整個比對)。"""
        try:
            ids = set(_bu.load(n))
        except Exception:
            return None
        return ids or None

    def _asof_of(n: int) -> Optional[str]:
        try:
            return _bu.load_asof(n)
        except Exception:
            return None

    resolved: Optional[int] = None
    resolved_by = "unresolved"
    ambiguous = False
    # 被考慮過的池的 as_of:未來池偵測要用(即使解析失敗也要能比對到快照)。
    candidates: Dict[str, Any] = {}
    expected_ids = _ids(expected)
    if expected_ids is not None:
        candidates[str(expected)] = _asof_of(expected)
    if wanted:
        if expected_ids is not None and wanted <= expected_ids:
            resolved, resolved_by = expected, "expected_candidate_pool"
        else:
            # expected 不涵蓋 → 只能猜。由小到大取第一個 superset,但這個結果
            # 不足以支撐一個 as_of 戳(見上面第二個 bug)。
            for n in sorted(set(_available_pool_sizes()) - {expected}):
                ids = _ids(n)
                if ids is None:
                    continue
                if wanted <= ids:
                    candidates[str(n)] = _asof_of(n)
                    if resolved is None:
                        resolved, resolved_by = n, "symbol_subset_of_other_pool"
                        ambiguous = True

    asof = None if (resolved is None or ambiguous) else _asof_of(resolved)
    if asof:
        asof_source = f"universe_top{resolved}.json"
    elif ambiguous:
        asof_source = "ambiguous_not_expected_pool"
    else:
        asof_source = "unresolved"
    return {
        "candidate_pool_top_n": resolved,
        "candidate_pool_file": (None if resolved is None
                                else f"outputs/universe_top{resolved}.json"),
        "candidate_pool_asof": asof,
        "candidate_pool_asof_source": asof_source,
        "candidate_pool_resolved_by": resolved_by,
        "candidate_pool_asof_ambiguous": ambiguous,
        "candidate_pool_asof_candidates": candidates,
        "candidate_pool_expected_top_n": expected,
        # 每日 dynamic universe 的 top-N 跟候選池是兩件事,分開記,不再混用。
        "dynamic_universe_top_n": int(universe_top_n),
    }


def _future_pool_provenance(pool_asof: Optional[str], snapshot: str, *,
                            pool_top_n: Optional[int] = None,
                            other_pool_asofs: Optional[Mapping[str, Any]] = None
                            ) -> Dict[str, Any]:
    """未來池逃生門(`SWING_ALLOW_FUTURE_POOL`)的 summary 欄位。

    原 bug:那道逃生門只 print 一行就放行,summary 沒有任何欄位 —— 對比價格
    逃生門至少有 `data.integrity_bypassed`。結果存進 `outputs/` 之後,含選股前視
    的績效跟乾淨績效長得一模一樣。

    三個來源都算數:
      1. `universe._assert_universe_pit` 放行時記下的事件(呼叫端載入池時觸發);
      2. 引擎自己比對「實際用到的池檔 as_of」與資料快照 —— 有些路徑直接傳
         `symbols=` 進來,根本沒經過 `get_universe`,那道檢查一次都不會跑;
      3. `other_pool_asofs`:所有**可能**被用到的池檔(尤其 config 宣告的
         expected 池)。2026-08-15 補:第 2 項綁在「解析出來的那份池」上,
         所以解析一歪(symbols 剛好是另一份較舊的池的子集),整條未來池偵測
         就跟著失效 —— 偵測不該依賴一個本來就可能猜錯的解析結果。
    """
    events = [dict(e) for e in uni.future_pool_bypass_log()]
    checks = [(pool_top_n, pool_asof)]
    for n, other in (other_pool_asofs or {}).items():
        checks.append((n, other))
    for n, asof in checks:
        if not (snapshot and asof and str(asof) > str(snapshot)):
            continue
        if any(str(e.get("pool_asof")) == str(asof) for e in events):
            continue
        events.append({
            "pool_top_n": (None if n is None else int(n)),
            "pool_asof": str(asof),
            "snapshot_end": str(snapshot),
            "detected_by": "backtest_summary",
        })
    return {
        "future_pool_bypass_allowed": bool(getattr(config, "ALLOW_FUTURE_POOL", False)),
        "future_pool_bypassed": bool(events),
        "future_pool_bypass_events": events,
    }


def _security_type_provenance(
        collector: Optional[security_type.ExclusionCollector] = None
        ) -> Dict[str, Any]:
    """證券別過濾在**這一次 request** 中擋掉了什麼(進 `summary["universe"]`)。

    為什麼一定要進 summary:2026-08-15 之前 `universe._is_normal_stock` 收了
    `market_type` 卻沒用,興櫃(381 檔通過)、DR、創新板一路混進候選池;興櫃沒有
    ±10% 漲跌停(2026-05 單日 |ret|>10.5% 佔比是上市的 ~100 倍、最大 +57.17%),
    偏誤方向是系統性灌高動能策略的 Sharpe。修掉之後候選池組成會變 —— 兩份結果
    如果沒有任何欄位分得出「用的是哪一種池」,舊數字就會被誤當成同一件事的重跑。

    為什麼**不**讀 `security_type.exclusion_summary()`(原 bug,同日第二輪修):
    那是 process 級全域紀錄簿,同一 process 連續跑兩次回測時,第二次的 summary
    會含第一次擋掉的證券(實測重現);平行 GA 搜尋則是每個 candidate 互相污染。
    數字只能來自這一次 request 自己的 collector(規格 §5.7 的 immutable request
    原則、§14 攻擊 16)。沒有 collector 時給空統計 —— 「沒量到」不可以借用別人的
    數字來填。
    """
    coll = (security_type.active_collector() if collector is None else collector)
    if coll is None:
        return {"excluded_by_security_type":
                security_type.ExclusionCollector().summary()}
    return {"excluded_by_security_type": coll.summary()}


# ── 外部訊號入口的證券別閘門 ───────────────────────────────────────────────
def _eligible_external_ids(ids, *, source: str,
                           collector=None) -> set:
    """外部訊號帶進來的代號裡,哪些是可以持有的上市櫃普通股。

    原 bug(2026-08-15 修,第二輪):普通股白名單只裝在 `_prepare_panel()` 裡,
    而 `picks_by_date` 與 `StrategyPositionPolicy` 這兩條路徑**正好都不經過
    panel**。`backtest_portfolio` 當時只做 `universe_info.update(
    _security_type_provenance())` —— 那是把統計寫進 summary,不是閘門。
    重現:用已知 DR 代號 9103 注入外部 picks,回測照樣建立持倉
    (`summary["open_positions_end"] == 1`),而 `excluded_by_security_type.total`
    是 0 —— summary 反而背書「這份池沒有洩漏」。

    未來最重要的研究入口(外部 `make_signals` / policy)因此可以把興櫃、DR、
    創新板送進一份宣稱「普通股池」的回測,而興櫃沒有 ±10% 漲跌停,偏誤方向是
    系統性灌高動能策略的 Sharpe。

    判定共用 `security_type.filter_ids`(不另寫第二份規則,兩份遲早分岔):
      - 已知的非普通股(興櫃 / DR / 創新板 / ETF / ETN / 受益證券 / 特別股)
        → 從訊號裡剔除,並記進 request 級排除紀錄簿(summary 數字因此是真的);
      - 證券別**判不出來**(不在 TaiwanStockInfo、欄位空白、沒見過的產業別)
        → `on_unknown="raise"` fail-closed。缺資訊不得預設放行,那正是原 bug
        的另一種形態。
    """
    return set(security_type.filter_ids(ids, source=source, on_unknown="raise",
                                        collector=collector))


def _gate_external_picks(picks_by_date: Dict, *, collector=None) -> Dict:
    """對 `picks_by_date` 施加證券別閘門(見 `_eligible_external_ids`)。

    刻意**保留**被清空的日期(值變成空 list)而不是刪掉整個 key:`min/max
    (picks_by_date)` 決定評估窗上下界,少掉一天等於偷偷改了評估區間。
    """
    all_ids = {str(sid) for picks in picks_by_date.values()
               for sid, *_rest in (picks or ())}
    if not all_ids:
        return picks_by_date
    kept = _eligible_external_ids(sorted(all_ids),
                                  source="backtest.picks_by_date",
                                  collector=collector)
    if len(kept) == len(all_ids):
        return picks_by_date
    return {d: [p for p in (picks or ()) if str(p[0]) in kept]
            for d, picks in picks_by_date.items()}


def _gate_signal_frame(signal_frame, *, collector=None):
    """對 policy 路徑的 `signal_frame` 施加同一道證券別閘門。

    整列剔除(而不是只把 `eligible` 標成 False):`eligible=False` 的語意是
    「訊號那端說今天不合格」,和「這根本不是上市櫃普通股、任何一天都不該被持有」
    不同,混在一起之後 summary 分不出被擋掉的是哪一種。
    """
    frame = pd.DataFrame(signal_frame)
    if len(frame) == 0 or "stock_id" not in frame.columns:
        return signal_frame
    ids = frame["stock_id"].astype(str)
    kept = _eligible_external_ids(sorted(dict.fromkeys(ids)),
                                  source="backtest.signal_frame",
                                  collector=collector)
    if len(kept) == ids.nunique():
        return signal_frame
    return frame[ids.isin(kept).to_numpy()].reset_index(drop=True)


def _apply_future_pool_downgrade(universe_meta: Dict[str, Any]) -> None:
    """用了未來池就不可能是正式證據 —— 就地降級,理由寫進 `evidence_note`。"""
    if not universe_meta.get("future_pool_bypassed"):
        return
    _downgrade_formal_evidence(
        universe_meta,
        "候選池建構日晚於資料快照(未來池 look-ahead),含選股前視,不可作正式證據")


def _apply_pool_asof_downgrade(universe_meta: Dict[str, Any]) -> None:
    """解析不出「實際用的是哪一份候選池」時降級。

    不知道池是哪一份 = 不知道它的建構日 = 無法排除未來池 look-ahead。這種
    情況下給正式證據等於用「猜對了」當前提。
    """
    if not universe_meta.get("candidate_pool_asof_ambiguous"):
        return
    _downgrade_formal_evidence(
        universe_meta,
        "symbols 同時是多份候選池檔的子集,無法確定實際用的是哪一份"
        "(as_of 未知,無法排除未來池)")


def _evaluation_provenance(split_info, segment: Optional[str]) -> Dict[str, Any]:
    """IS / embargo / OS 的固定日期(以及「呼叫端根本沒宣告」這件事)。

    為什麼要進 summary:IS/OS 是在凍結資料**內部**畫的線,引擎不知道那條線存在
    (AGENTS.md 陷阱 5 的註)。過去 summary 只有 `period` 與 `eval_audit`,
    要判斷「這段到底是 IS 還是 OS、embargo 幾天」只能翻當初的腳本或憑記憶 ——
    而 holdout 被看過幾次正是這個 repo 最需要留痕跡的事。

    沒宣告時**不猜**:`split_declared=False` 明說這個數字沒有被綁到任何切割。
    """
    out: Dict[str, Any] = {
        "segment": segment,
        "split_declared": split_info is not None,
        "is_window": None,
        "os_window": None,
        "embargo_trading_days": None,
        "split_mode": None,
        # config 的切割設定:即使呼叫端沒宣告,也看得到當時的全域設定值。
        "is_os_split_config": getattr(config, "IS_OS_SPLIT", None),
        "embargo_days_config": getattr(config, "EMBARGO_DAYS", None),
        "ic_horizon": getattr(config, "BT_IC_HORIZON", None),
    }
    if split_info is None:
        return out
    d = split_info.to_dict() if hasattr(split_info, "to_dict") else dict(split_info)
    out.update({
        "is_window": [d.get("is_start"), d.get("is_end")],
        "os_window": [d.get("os_start"), d.get("os_end")],
        "embargo_trading_days": d.get("n_embargo"),
        "split_mode": d.get("mode"),
        "n_is": d.get("n_is"),
        "n_os": d.get("n_os"),
        "n_total": d.get("n_total"),
    })
    return out


def _dynamic_universe_settings(dynamic_enabled: bool,
                               universe_top_n: int) -> Dict[str, Any]:
    """dynamic universe 的**實際生效**設定(每日成員資格怎麼決定)。

    這些值決定了「哪些股票當天可被選」,跟因子權重同級的 load-bearing 參數,
    但過去只有走 `_prepare_panel` 的路徑會記到一半(external picks 路徑完全沒有)。
    """
    return {
        "dynamic_enabled": bool(dynamic_enabled),
        "top_n": int(universe_top_n) if dynamic_enabled else None,
        "lookback": config.DYNAMIC_UNIVERSE_LOOKBACK,
        "min_obs": config.DYNAMIC_UNIVERSE_MIN_OBS,
        "min_avg_volume_lots": config.DYNAMIC_UNIVERSE_MIN_AVG_VOLUME_LOTS,
        "min_avg_turnover": config.DYNAMIC_UNIVERSE_MIN_AVG_TURNOVER,
        "candidate_pool_n_config": config.DYNAMIC_UNIVERSE_CANDIDATE_POOL,
    }


# ── 預先計算所有股票的因子（含未來報酬）──────────────────────────────
def _prepare_panel(symbols: List[str], min_score_for_trade: float,
                   start_date: Optional[str], end_date: Optional[str],
                   *,
                   dynamic_enabled: Optional[bool] = None,
                   universe_top_n: Optional[int] = None,
                   keep_non_members: bool = False,
                   universe_provider=None,
                   sample: bool = False,
                   static_universe_comparator: bool = False) -> pd.DataFrame:
    """
    把所有股票每一天的因子 + 綜合分數 + 未來N日報酬，攤平成一個大 panel。
    這個 panel 同時用於 (1) 整體回測選股 (2) 因子 IC 分析。

    ⚠ 這是**引擎內部**函式,`keep_non_members` 預設 False(只留動態 universe 成員日)
    是為了引擎自己的橫斷面選股;研究/策略程式請一律走公開入口
    `build_research_panel()`(預設稠密)。回傳的 panel 會戳上稠密度標籤
    (`panel.attrs["panel_density"]`),稀疏 panel 上做 `ts_`/rolling 會被
    `factor_engine.panel_density` 擋下(不變式 3)。
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
        # 引擎邊界的**次要**防線:主要判定在池建構(security_type.filter_*)。
        # 這裡刻意不查 registry —— 引擎可能收到研究用的合成代號,查不到證券別就
        # raise 會把「池已經篩過了」的正常路徑一起擋掉。共用同一份非普通股清單,
        # 所以 DR/受益證券/創新板現在也擋得住(原本只認字串 "ETF"/"ETN")。
        if security_type.is_non_common_industry(industry) or sid.startswith("00"):
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
        "industry_asof": _asof,          # = 資料快照日(TaiwanStockInfo 以此戳快取)
        **_dynamic_universe_settings(dynamic_enabled, universe_top_n),
    }
    candidate_mask = None
    if universe_provider is not None:
        candidate_mask = universe_provider.candidate_mask(panel)
        # provider 自帶候選池規則 / pool size / pool as-of(月頻 PIT 的歷史末端)。
        universe_meta.update(universe_provider.metadata())
    else:
        # legacy 單日池:pool as-of 必須來自**實際被用到的那份池檔**,
        # 不是每日 top-N 那份(見 `_legacy_pool_provenance` 的原 bug 說明)。
        universe_meta.update(_legacy_pool_provenance(
            symbols, dynamic_enabled=dynamic_enabled,
            universe_top_n=universe_top_n))
    universe_meta.update(_future_pool_provenance(
        universe_meta.get("candidate_pool_asof"), _asof,
        pool_top_n=universe_meta.get("candidate_pool_top_n"),
        # 解析歪掉時仍要比對得到 expected 池的 as_of(見該函式第 3 點)。
        other_pool_asofs=universe_meta.get("candidate_pool_asof_candidates")))
    # provenance 最後蓋上:誠實標籤不可被 provider metadata 或舊欄位覆寫。
    universe_meta.update(universe_provenance)
    universe_meta.update(_security_type_provenance())
    _apply_future_pool_downgrade(universe_meta)
    _apply_pool_asof_downgrade(universe_meta)
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
        # 成員資格設定已由 `_dynamic_universe_settings` 統一記過(所有路徑一致),
        # 這裡只補「這次實際的成員統計」。
        universe_meta.update(dynamic_universe.membership_summary(ranked))
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
    # 稠密度標籤:讓「這個 panel 能不能拿去算 ts_」寫在資料上,而不是靠呼叫端記得。
    # static 模式沒有成員過濾(整段都 in_dynamic_universe=True),序列本來就連續。
    panel_density.tag(
        panel,
        panel_density.DENSE if (keep_non_members or not dynamic_enabled)
        else panel_density.MEMBERS_ONLY,
    )
    return panel


# ── 公開入口:研究/策略要 panel 一律走這裡（不變式 3 的預設安全）──────────
def build_research_panel(symbols: Optional[List[str]] = None, *,
                         start_date: Optional[str] = None,
                         end_date: Optional[str] = None,
                         dynamic_enabled: Optional[bool] = None,
                         universe_top_n: Optional[int] = None,
                         universe_provider=None,
                         sample: bool = False,
                         static_universe_comparator: bool = False,
                         members_only: bool = False) -> pd.DataFrame:
    """回傳研究用 panel,**預設稠密**(每檔保留完整交易日序列)。

    為什麼要有這個入口:`_prepare_panel` 的 `keep_non_members` 預設是 False,
    也就是「只留動態 universe 成員日」。那個預設對引擎自己的橫斷面選股是對的,
    但對任何 `ts_`／rolling 因子都是錯的 —— long panel 的 `rolling(20)` 算的是
    「20 列」,一檔間歇進出 universe 的股票,那 20 列會橫跨 60+ 個日曆日
    (AGENTS.md 陷阱 1)。實測 `rotation_research` 的 `breakout_20` 因此翻轉約 3%
    的訊號、命中率被灌水約 +9.6%。

    所以正確的分工是:

        panel = backtest.build_research_panel(**pit.backtest_kwargs())  # 稠密
        score = build_signal(panel)                       # 因子在稠密 panel 上算
        picks = panel[panel["in_dynamic_universe"]]        # 成員過濾留到選股階段

    `members_only=True` 只給「純當日橫斷面統計」用(IC、分位、族群 breadth):
    那類統計本來就只看同一天的橫斷面,因子本身已在 `_prepare_panel` 內部於完整
    個股序列上算好。這種 panel 會被標成 `members_only`,之後任何 `ts_` 算子都會
    fail-closed raise,而不是靜默給出失真的值。

    候選池的意圖仍由 `_resolve_universe_source` 強制(PIT provider / 顯式
    static comparator / sample),這裡只決定稠密度。

    證券別排除統計(`panel.attrs["universe"]["excluded_by_security_type"]`)只算
    **這一次呼叫**:沒有外層 `security_type.exclusion_scope()` 時自己開一本,
    不會沿用 process 全域紀錄簿(那會讓第二次建 panel 借到第一次的數字)。
    """
    with security_type.exclusion_scope(
            security_type.active_collector(), label="build_research_panel"):
        return _build_research_panel(
            symbols, start_date=start_date, end_date=end_date,
            dynamic_enabled=dynamic_enabled, universe_top_n=universe_top_n,
            universe_provider=universe_provider, sample=sample,
            static_universe_comparator=static_universe_comparator,
            members_only=members_only)


def _build_research_panel(symbols: Optional[List[str]] = None, *,
                          start_date: Optional[str] = None,
                          end_date: Optional[str] = None,
                          dynamic_enabled: Optional[bool] = None,
                          universe_top_n: Optional[int] = None,
                          universe_provider=None,
                          sample: bool = False,
                          static_universe_comparator: bool = False,
                          members_only: bool = False) -> pd.DataFrame:
    """`build_research_panel` 的本體(排除紀錄簿由外層開好)。"""
    return _prepare_panel(
        symbols,
        0.0,                      # min_score_for_trade:引擎歷史殘留參數,panel 不用它
        start_date,
        end_date,
        dynamic_enabled=dynamic_enabled,
        universe_top_n=universe_top_n,
        keep_non_members=not members_only,
        universe_provider=universe_provider,
        sample=sample,
        static_universe_comparator=static_universe_comparator,
    )


# ── StrategyPositionPolicy 的訊號快照 ───────────────────────────────────────
def _prepare_signal_snapshots(signal_frame):
    """把長表 `date/stock_id/rank` 切成 `{快照日 -> 當日完整排名}`。

    **決策日就是快照日,引擎不自己算星期幾。** 舊的 `rebalance_every/phase` 是
    「每 N 個交易日」,一旦訊號那端用的是「每週最後一個交易日」,兩者在有假日的
    週就會錯開,而錯開的方向剛好是「用還沒發生的訊號」或「漏掉整週」。決策頻率
    的語意屬於訊號產生端(規格 §3.1),所以由 signal_frame 表達,引擎只負責照做。

    回傳 `(snapshots, sorted_dates)`。
    """
    frame = pd.DataFrame(signal_frame).copy()
    missing = {"date", "stock_id", "rank"} - set(frame.columns)
    if missing:
        raise ValueError(
            f"[fail-closed] signal_frame 缺欄位 {sorted(missing)};"
            "至少要有 date / stock_id / rank")
    frame["date"] = pd.to_datetime(frame["date"])
    dupes = frame.duplicated(subset=["date", "stock_id"]).sum()
    if dupes:
        raise ValueError(
            f"[fail-closed] signal_frame 有 {dupes} 筆重複的 (date, stock_id);"
            "同一天同一檔兩個 rank,決策結果會取決於列順序")
    snapshots = {d: g.reset_index(drop=True) for d, g in frame.groupby("date")}
    return snapshots, sorted(snapshots)


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
def backtest_portfolio(*args, exclusion_collector=None, **kwargs) -> Dict:
    """`_backtest_portfolio` 的公開入口:替這一次 request 開一本排除紀錄簿。

    參數與說明全部在 `_backtest_portfolio`(這裡只多一個 `exclusion_collector`)。

    為什麼要有這層(2026-08-15 修,第二輪):證券別排除統計原本存在 module 級
    全域 list,`reset_exclusion_log()` 還寫著「一個 process = 一次研究執行,
    不該清」。那個假設對「同一 process 連續跑兩次回測」與「平行 GA 搜尋」都是錯的
    —— 實測連續呼叫兩次,第二次的 `excluded_by_security_type` 仍含第一次的紀錄,
    等於用別人的排除數替這份績效背書。改成每次 request 自己的
    `ExclusionCollector`(隨 request 建立、隨 summary 回傳),與
    `CROSS_SECTIONAL_STRATEGY_RESEARCH_SPEC.md` §5.7「immutable request,
    不得靠改寫全域狀態傳遞參數」同一條原則。

    `exclusion_collector`:呼叫端想把**池建構**的排除也算進這次 request 時傳進來
    (或改用 `security_type.exclusion_scope()` 把兩段包在一起);不傳就用當下
    scope 的那一本,沒有 scope 就開一本新的 —— 絕不退回全域紀錄簿。
    """
    collector = (security_type.active_collector()
                 if exclusion_collector is None else exclusion_collector)
    if collector is None:
        collector = security_type.ExclusionCollector(label="backtest_portfolio")
    with security_type.exclusion_scope(collector):
        return _backtest_portfolio(*args, **kwargs)


def _backtest_portfolio(symbols: Optional[List[str]] = None,
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
                       static_universe_comparator: bool = False,
                       evaluation_split_info=None,
                       segment: Optional[str] = None,
                       strategy_spec=None,
                       signal_frame=None,
                       strategy_position_policy=None,
                       regime_by_date: Optional[Mapping] = None,
                       initial_capital: Optional[float] = None,
                       order_size_mode: Optional[str] = None,
                       minimum_commission: Optional[float] = None) -> Dict:
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

    provenance 參數(都只影響 `summary`,不影響任何數字):
      - `evaluation_split_info`:`evaluation.splits.EvaluationSplit` 或它的
        `to_dict()`。傳了才能在結果裡看到這段是哪一組 IS/embargo/OS 邊界;
        沒傳就誠實記 `split_declared=False`(= 這個數字沒有被綁到任何切割,
        事後不可宣稱它是 IS 或 OS)。
      - `segment`:`"IS"` / `"OS"` / `"forward"` 之類的段名。
      - `strategy_spec`:`strategies.spec.StrategySpec`。引擎自己只知道 config 那半
        參數;走 external picks 的策略(S19)其訊號視窗與權重在 spec 裡,不傳就
        不會出現在 summary,那份績效等於少了決定它的一半規則。

    StrategyPositionPolicy 路徑(第三條,見 STRATEGY_POSITION_POLICY_SPEC.md):
      - `signal_frame`:長表 `date / stock_id / rank`(可含 `raw_score` /
        `eligible` / `snapshot_complete`)。**決策日由這張表的快照日期定義**,
        引擎不自己算「每五列」或星期幾 —— 假日週會讓那種推法整段錯位。
        `snapshot_complete` 沒宣告時,該快照視為**完整性未知**(規格 §9B.1):
        持股從快照消失只能解讀為 unknown,不會產生 `not_ranked` 退出,
        `summary[...]["snapshot_complete_all_days"]` 也會是 False。要讓
        `not_ranked` 生效,訊號那端必須自己宣告 `snapshot_complete=True`。
      - `strategy_position_policy`:`strategies.position_policy.StrategyPositionPolicy`。
        傳了就改走 desired-state 迴圈(T 日收盤決策 → T+1 開盤成交),與
        `picks_by_date` 互斥。沒傳時本函式的行為與此次改動前**完全相同**。
      - `initial_capital` / `order_size_mode` / `minimum_commission`:immutable
        request 參數。過去要換資金情境只能就地改全域 `config`,兩個情境會互相
        污染(而且改壞了不會有人發現)。這三個參數只影響本次呼叫,**不寫回 config**;
        不傳就沿用 config 值(legacy 行為不變)。
      - `regime_by_date`:`{date -> "risk_on"/"caution"/"risk_off"}`,必須是外部
        已帶 PIT provenance 的判定。引擎不算 regime 公式(規格 §8)。
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
    policy_enabled = strategy_position_policy is not None
    if policy_enabled:
        if external_picks:
            raise ValueError(
                "strategy_position_policy 與 picks_by_date 互斥:兩者都在決定"
                "「今天要持有什麼」,同時給等於有兩套投組規則,結果無法歸因")
        if signal_frame is None or len(signal_frame) == 0:
            raise ValueError(
                "[fail-closed] strategy_position_policy 需要 signal_frame"
                "(date / stock_id / rank);沒有排名快照就沒有決策日,"
                "引擎不會自己推算星期幾")
        if bool(getattr(config, "MARKET_FILTER_ENABLED", False)):
            raise ValueError(
                "MARKET_FILTER_ENABLED 與 strategy_position_policy 互斥:"
                "曝險調整由 policy 的 regime slots 表達,兩套同時作用會重複降曝險")
    # ── 外部訊號的證券別閘門(兩條繞過 panel 的路徑都要真的擋)──────────────
    # 這裡是**閘門**,不是統計:被擋掉的代號從訊號裡消失,不可能建立持倉。
    # 舊版只在 summary 補一行 provenance,所以 9103(DR)注入外部 picks 照樣成交。
    _security_exclusions = security_type.active_collector()
    if external_picks and picks_by_date:
        picks_by_date = _gate_external_picks(picks_by_date,
                                             collector=_security_exclusions)
    if policy_enabled:
        signal_frame = _gate_signal_frame(signal_frame,
                                          collector=_security_exclusions)
        if len(pd.DataFrame(signal_frame)) == 0:
            raise ValueError(
                "[fail-closed] signal_frame 的標的全部被證券別閘門擋掉"
                "(非上市櫃普通股);沒有可持有的標的就沒有可回測的策略")
    symbols, universe_provider, universe_provenance = _resolve_universe_source(
        symbols, sample=sample, dynamic_enabled=dynamic_enabled,
        universe_provider=universe_provider,
        static_universe_comparator=static_universe_comparator,
        caller="backtest_portfolio",
        external_picks=external_picks or policy_enabled,
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
    signal_snapshots: Dict[Any, pd.DataFrame] = {}
    if policy_enabled:
        # policy 路徑沒有 panel,但同一道未還原價 fail-closed 閘門仍要生效。
        _assert_price_integrity(symbols)
        signal_snapshots, decision_dates = _prepare_signal_snapshots(signal_frame)
        cal = sorted(set().union(*[set(p["date"]) for p in price_cache.values()])) \
            if price_cache else []
        lo = min(decision_dates)
        all_dates = [d for d in cal if d >= lo]
        if start_date:
            all_dates = [d for d in all_dates if d >= pd.to_datetime(start_date)]
        hard_end = pd.to_datetime(end_date) if end_date else None
        # 評估窗上界:與 external picks 分支**刻意不同**。那裡的 picks 是逐日的,
        # 所以「最後一個訊號日」就是訊號用完的那一天;這裡的 snapshot 是每週一
        # 次,把窗截到最後一個快照日會系統性砍掉每段 IS/OS 的最後一週(而且是
        # 部位還開著的那一週)。policy 路徑因此以呼叫端顯式宣告的 `end_date`
        # 為準 —— 那條線本來就是 evaluation/splits 畫出來的邊界。沒給 end_date
        # 時仍退回保守作法(截到最後快照日),避免無聲跑到資料末端。
        if hard_end is not None:
            all_dates = [d for d in all_dates if d <= hard_end]
        elif not let_positions_run:
            bound = max(decision_dates)
            if any(d > bound for d in all_dates):
                print(f"[backtest] policy 路徑未指定 end_date,評估窗截到最後一個"
                      f"訊號快照日 {str(bound)[:10]}。正式 IS/OS 請顯式傳 end_date。")
            all_dates = [d for d in all_dates if d <= bound]
        if not all_dates:
            return {"error": "policy 路徑在指定期間內沒有交易日,無法回測"}
        # 快照日必須真的是市場交易日,否則那一天永遠不會被走到 —— 決策日靜默
        # 消失,而回測會照跑完並產出一組「訊號從來沒被執行過」的績效。
        in_window = [s for s in decision_dates
                     if all_dates[0] <= s <= all_dates[-1]]
        orphans = [s for s in in_window if s not in set(all_dates)]
        if orphans:
            raise ValueError(
                f"[fail-closed] signal_frame 有 {len(orphans)} 個快照日不是價格資料"
                f"裡的交易日(例:{[str(x)[:10] for x in orphans[:3]]});"
                "那些決策日會被靜默略過")
    elif not external_picks:
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
    # policy 路徑的候選來源是 signal_frame 的排名快照;PIT 驗證要驗的東西一樣,
    # 所以把它攤成 picks 形狀交給同一支 `_verify_external_picks_are_pit`,
    # 不另外寫第二份候選池驗證(兩份遲早分岔)。
    _pit_check_picks = picks_by_date
    if policy_enabled:
        _pit_check_picks = {
            d: [(sid, float("nan"), sid) for sid in snap["stock_id"]]
            for d, snap in signal_snapshots.items()
        }
    if external_picks or policy_enabled:
        universe_info = {
            "enabled": dynamic_enabled, "direction": "long_only",
            "candidate_source": ("strategy_position_policy_signal_frame"
                                 if policy_enabled else "external_picks_by_date"),
            "survivorship_free": False, "industry_pit": False,
            "industry_asof": _asof,
            # 候選池 as-of 沒有 provider 就是**不知道**(picks 由呼叫端決定)。
            # 舊版在這裡填快照日,等於替一個沒被驗證過的候選池戳上合規日期。
            "candidate_pool_asof": None,
            "candidate_pool_asof_source": "unresolved",
            **_dynamic_universe_settings(dynamic_enabled, universe_top_n),
        }
        # 呼叫端自建 panel/picks 但仍傳了真正的 PIT provider(S19 就是這樣)時,
        # summary 必須保留 provider 的真實 metadata —— 否則正式策略的候選池規則、
        # pool as-of 會在結果裡被寫成 "external_picks_by_date" 這種空白標籤,
        # 之後沒人能從 summary 判斷這段績效的候選池到底是不是 PIT。
        if universe_provider is not None:
            universe_info["picks_source"] = universe_info["candidate_source"]
            universe_info.update(universe_provider.metadata())
            universe_info["candidate_pool_asof_source"] = "universe_provider"
        universe_info.update(_future_pool_provenance(
            universe_info.get("candidate_pool_asof"), _asof,
            pool_top_n=universe_info.get("candidate_pool_top_n")))
        universe_info.update(universe_provenance)
        # PIT 章不是「有 provider 物件」就能蓋:picks 必須逐日落在候選遮罩內。
        # 這一步排在 `universe_provenance` 之後,因為它蓋掉的正是那組樂觀標籤。
        if universe_provider is not None:
            universe_info.update(_verify_external_picks_are_pit(
                _pit_check_picks, universe_provider,
                dates_in_scope=all_dates, caller="backtest_portfolio"))
        # 候選池是 PIT 還不夠:picks 背後的訊號規則若沒有 provenance,這份績效
        # 一樣無法被重現或被稽核(見下方 `strategy_spec` 的說明)。
        if universe_info.get("formal_evidence_eligible") and strategy_spec is None:
            _downgrade_formal_evidence(
                universe_info,
                "picks 由呼叫端產生但未附 StrategySpec:訊號規則(視窗/權重)"
                "在 summary 裡沒有任何 provenance,不可作正式證據")
        _apply_future_pool_downgrade(universe_info)
    else:
        universe_info = panel.attrs.get("universe", {
            "enabled": dynamic_enabled, "direction": "long_only",
            **_dynamic_universe_settings(dynamic_enabled, universe_top_n),
            **universe_provenance,
        })
    # 證券別統計是「這份池是哪一種池」的唯一線索,三條路徑都要蓋(external picks
    # 與 policy 路徑沒有 panel.attrs,panel 路徑則要更新成 request 當下的最新統計)。
    # 真正的閘門在上面 `_gate_external_picks` / `_gate_signal_frame` —— 這一行只是
    # 把那道閘門擋掉的數字寫進結果,單靠它擋不住任何東西(那正是原 bug)。
    universe_info.update(_security_type_provenance(_security_exclusions))

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

    # 投組狀態。資金情境是 **immutable request**:傳進來就只影響這一次執行,
    # 不寫回 config —— 過去要比較 100 萬研究情境與 50 萬個人情境只能就地改全域
    # 參數,兩次執行會互相污染,而且還原漏了不會有任何欄位看得出來(規格 §4.1)。
    initial_capital = float(
        getattr(config, "BT_INITIAL_CAPITAL", 1_000_000.0)
        if initial_capital is None else initial_capital)
    if initial_capital <= 0:
        raise ValueError("initial_capital 必須為正數")
    order_size_mode = OrderSizeMode(
        getattr(config, "BT_ORDER_SIZE_MODE", OrderSizeMode.RESEARCH_FRACTIONAL.value)
        if order_size_mode is None else order_size_mode)
    cost_model = TaiwanStockCostModel(
        commission_rate=config.BT_FEE,
        minimum_commission=(getattr(config, "BT_MIN_COMMISSION", 0.0)
                            if minimum_commission is None else minimum_commission),
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

    def _mark_to_market(di, d) -> float:
        """收盤淨值 = 現金 + 各部位市值。

        缺 bar(停牌/被清理列)時延用「最後一次已知收盤」,不回退成本價——回退成本
        會讓權益曲線在缺 bar 日假跳到成本、隔日跳回,灌大波動/回撤;且下市股不會
        被凍結在成本價(那在 survivorship-free 重跑時會變成忽略下市虧損的樂觀偏誤)。
        """
        mtm = cash
        for sid, pos in positions.items():
            bar, _ = _price_row(sid, d)
            if bar is not None:
                pos["last_close"] = float(bar["close"])
                pos["last_bar_di"] = di        # 記最後有 bar 的日,供缺bar殭屍出場計齡
            mtm += pos["shares"] * pos["last_close"]
        return mtm

    # ── StrategyPositionPolicy 路徑的 desired / realized 狀態 ────────────────
    decision_log: List[dict] = []
    order_log: List[dict] = []
    target_portfolio: List[dict] = []
    desired_targets: Dict[str, float] = {}      # sid -> target_weight(決策日更新)
    desired_notional: Dict[str, float] = {}     # sid -> 決策當下淨值算出的目標金額
    exit_intents: Dict[str, str] = {}           # sid -> reason_code(成交前不清掉)
    resize_intents: Dict[str, float] = {}       # sid -> 目標金額(concentration cap)
    policy_audit: Dict[str, int] = {
        "limit_down_exit_delays": 0,        # 一字跌停賣不掉,部位留在 realized
        "limit_up_entry_skips": 0,          # 一字漲停買不到
        "halted_or_missing_bar_delays": 0,  # 停牌/缺 bar 無法成交
        "stale_delist_forced_exits": 0,     # 斷 bar 超過門檻 → 引擎強制結算(非 policy 意圖)
        "insufficient_cash_entry_skips": 0, # 現金不足(通常正是前一項的下游)
        "lot_rounding_entry_skips": 0,      # 資金不足一個合法交易單位
        "disposition_entry_blocks": 0,      # 處置期間禁新倉
        "invalid_price_skips": 0,
        "n_desired_exits": 0, "n_realized_exits": 0,
        "n_desired_entries": 0, "n_realized_entries": 0,
        "n_desired_resizes": 0, "n_realized_resizes": 0,
        "n_decision_days": 0, "n_policy_snapshots": 0,
        "n_snapshot_incomplete_days": 0,
    }
    _policy_decision_dates = set(decision_dates) if policy_enabled else set()
    _policy_snapshot_dates = sorted(signal_snapshots) if policy_enabled else []
    # regime 由外部(帶 PIT provenance)決定;給了就必須逐日給滿。缺哪天就當
    # risk_on 放行,等於在資料缺口上偷偷恢復滿曝險 —— 那正是最該擋的方向。
    _regime_map: Dict[Any, str] = {}
    if policy_enabled and regime_by_date:
        _regime_map = {pd.Timestamp(k): str(v) for k, v in regime_by_date.items()}
        gaps = [d for d in all_dates if d not in _regime_map]
        if gaps:
            raise ValueError(
                f"[fail-closed] regime_by_date 缺 {len(gaps)} 個交易日"
                f"(例:{[str(x)[:10] for x in gaps[:3]]});缺值不得當成 risk_on")

    def _order(d, sid, side, status, reason, *, shares=0.0, price=float("nan"),
               intended_notional=float("nan"), action="", reason_code=""):
        """order_log 一列 = 一個「送進事件引擎的意圖」及它的下場。

        沒有這份紀錄時,跌停賣不掉、現金不足、湊不到一張全都只是「跳過」,回測
        看起來永遠是想買就買到 —— 而那正是最會灌水的一塊(規格 §7)。
        """
        order_log.append({
            "date": d, "stock_id": sid, "side": side, "status": status,
            "reason": reason, "shares": float(shares), "price": float(price),
            "intended_notional": float(intended_notional),
            "action": action, "reason_code": reason_code,
        })

    def _record_exit_trade(sid, pos, d, exit_price, reason, days_held, shares=None):
        """結算(整筆或部分)退出並把 proceeds 加進現金。回傳 proceeds。"""
        nonlocal cash
        shares = float(pos["shares"]) if shares is None else float(shares)
        portion = shares / float(pos["shares"]) if pos["shares"] else 1.0
        cost_part = float(pos["cost"]) * portion
        proceeds = float(cost_model.sell_proceeds(shares, exit_price))
        cash += proceeds
        gross = (exit_price - pos["entry_price"]) / pos["entry_price"]
        net = proceeds / cost_part - 1.0 if cost_part else float("nan")
        trades.append({
            "stock_id": sid, "name": pos["name"],
            "signal_date": pos["signal_date"],
            "entry_date": pos["entry_date"], "exit_date": d,
            "entry_price": round(pos["entry_price"], 2),
            "exit_price": round(exit_price, 2),
            "shares": shares,
            "entry_cost": round(cost_part, 2),
            "exit_proceeds": round(proceeds, 2),
            "hold_bars": days_held,
            "gross_ret": round(gross, 4),
            "ret": round(net, 4),
            "exit_reason": reason,
            "composite": round(pos["composite"], 2),
        })
        return proceeds

    def _settle_stale_delisted(di, d, sid, pos):
        """持股連續斷 bar 太久(疑似長停牌/下市)的 fail-closed 結算。

        回傳實際結算價;還沒到門檻就回傳 None(續抱)。

        為什麼要抽成共用 helper
        ------------------------
        這段判定原本只有 legacy 迴圈有,policy 路徑另外抄了一份 —— 而且抄進了
        「這檔已經有退出意圖」的分支裡。於是**沒有**退出意圖的下市股(下市股在排名
        快照裡本來就會消失,只有 snapshot_complete 才會產生 not_ranked;非決策日
        更是完全不看排名)永遠留在帳上、以凍結的最後收盤計價,下市虧損被整段忽略,
        而且 legacy 那句「沒有正式清算資料,拒絕假設可用最後收盤賣出」在 policy
        路徑上永遠不會觸發。方向剛好是樂觀偏誤,所以兩條路徑必須共用同一份實作。

        呼叫點必須在**當日 mark-to-market 之前**(兩條路徑都是):`last_bar_di` 由
        `_mark_to_market` 在收盤時更新,先 MTM 再判定會讓 stale 永遠少算一天。
        """
        nonlocal n_stale_exits
        stale = di - pos.get("last_bar_di", pos.get("entry_di", di))
        if stale < config.BT_STALE_EXIT_DAYS:
            return None
        recovery = getattr(config, "BT_DELIST_RECOVERY", None)
        if recovery is None:
            raise RuntimeError(
                f"{sid} 已連續 {stale} 個市場交易日無 bar（疑似長停牌/下市）；"
                "沒有正式清算資料，拒絕假設可用最後收盤賣出。"
                "可用 SWING_DELIST_RECOVERY=0~1 做明確敏感度測試"
            )
        exit_price = pos["last_close"] * float(recovery)
        _record_exit_trade(sid, pos, d, exit_price, "stale_delisted",
                           di - pos.get("entry_di", di))
        del positions[sid]
        n_stale_exits += 1
        return exit_price

    def _policy_settle_desired(di, d):
        """T 日形成的 desired state,在 T+1(也就是今天)嘗試成交。

        順序是規格 §6 的不變式,不可對調:**先退出 → 只把實際成交的 proceeds 加進
        現金 → 再用真實現金進場**。倒過來就會出現「A 一字跌停賣不掉,卻已經用它
        的賣出款買了 B」——曝險與績效憑空多一份,而且看不出來。
        """
        nonlocal cash, n_limit_skip, n_disp_skip, n_lot_skip, n_stale_exits

        # (a0) 缺 bar 的持股:**每一檔每天**都要判定,不能只判「已經有退出意圖」
        #      的那些。下市股通常一個退出意圖都沒有(它在排名快照裡直接消失),
        #      漏掉就等於把下市虧損當成沒發生 —— 這是 policy 路徑一度真的存在的
        #      樂觀偏誤,見 `_settle_stale_delisted` 的說明。
        for sid in list(positions.keys()):
            pos = positions[sid]
            bar, _idx = _price_row(sid, d)
            if bar is not None:
                continue
            reason = exit_intents.get(sid)
            settled_price = _settle_stale_delisted(di, d, sid, pos)
            if settled_price is not None:
                # 強制結算沒有「意圖」可言時,reason_code 記 forced_exit
                #(規格 §3.2 的第一種每日事件:失去合法交易資格)。
                _order(d, sid, "sell", "filled", "stale_delisted_recovery",
                       shares=pos["shares"], price=settled_price,
                       action="exit", reason_code=reason or "forced_exit")
                exit_intents.pop(sid, None)
                resize_intents.pop(sid, None)
                # 目標部位也要清掉,否則 (c) 會每天對一檔已下市的股票重下買單。
                desired_targets.pop(sid, None)
                desired_notional.pop(sid, None)
                policy_audit["stale_delist_forced_exits"] += 1
                if reason is not None:
                    policy_audit["n_realized_exits"] += 1
            elif reason is not None:
                _order(d, sid, "sell", "unfilled", "halted_no_bar",
                       action="exit", reason_code=reason)
                policy_audit["halted_or_missing_bar_delays"] += 1

        # (a) 退出:賣不掉就留在 realized holdings 繼續 MTM,意圖不清掉。
        for sid in list(positions.keys()):
            reason = exit_intents.get(sid)
            if reason is None:
                continue
            pos = positions[sid]
            bar, idx = _price_row(sid, d)
            if bar is None:
                continue    # (a0) 已處理:不是已強制結算,就是已記 halted_no_bar
            if idx <= pos["entry_idx"]:
                # 進場當天不出場(T 日收盤才形成的意圖不可能在同一天成交)。
                _order(d, sid, "sell", "unfilled", "same_day_as_entry",
                       action="exit", reason_code=reason)
                continue
            if config.BT_MODEL_LIMIT_LOCK and idx > 0:
                pc = price_cache[sid]["close"].iloc[idx - 1]
                if _limit_lock(bar, float(pc)) == "down":
                    _order(d, sid, "sell", "unfilled", "limit_down_lock",
                           action="exit", reason_code=reason)
                    policy_audit["limit_down_exit_delays"] += 1
                    continue
            exit_price = float(bar["open"])
            if not np.isfinite(exit_price) or exit_price <= 0:
                _order(d, sid, "sell", "unfilled", "invalid_open_price",
                       action="exit", reason_code=reason)
                policy_audit["invalid_price_skips"] += 1
                continue
            days_held = idx - pos["entry_idx"]
            proceeds = _record_exit_trade(sid, pos, d, exit_price, reason, days_held)
            _order(d, sid, "sell", "filled", reason, shares=pos["shares"],
                   price=exit_price, intended_notional=proceeds,
                   action="exit", reason_code=reason)
            del positions[sid]
            exit_intents.pop(sid, None)
            resize_intents.pop(sid, None)
            policy_audit["n_realized_exits"] += 1

        # (b) 單檔超過集中度上限 → 部分減碼(規格 §4.2 唯一的 resize 來源)。
        for sid in list(resize_intents.keys()):
            if sid not in positions or sid in exit_intents:
                resize_intents.pop(sid, None)
                continue
            pos = positions[sid]
            bar, idx = _price_row(sid, d)
            if bar is None or idx <= pos["entry_idx"]:
                _order(d, sid, "sell", "unfilled", "halted_no_bar",
                       action="resize", reason_code="concentration_cap")
                policy_audit["halted_or_missing_bar_delays"] += 1
                continue
            if config.BT_MODEL_LIMIT_LOCK and idx > 0:
                pc = price_cache[sid]["close"].iloc[idx - 1]
                if _limit_lock(bar, float(pc)) == "down":
                    _order(d, sid, "sell", "unfilled", "limit_down_lock",
                           action="resize", reason_code="concentration_cap")
                    policy_audit["limit_down_exit_delays"] += 1
                    continue
            px = float(bar["open"])
            target_value = float(resize_intents[sid])
            if not np.isfinite(px) or px <= 0:
                policy_audit["invalid_price_skips"] += 1
                continue
            excess = float(pos["shares"]) * px - target_value
            unit = (getattr(config, "BT_REGULAR_LOT_SHARES", 1000)
                    if order_size_mode == OrderSizeMode.REGULAR_LOT else 1)
            if order_size_mode == OrderSizeMode.RESEARCH_FRACTIONAL:
                sell_shares = max(0.0, excess / px)
            else:
                sell_shares = float(int(max(0.0, excess / px) // unit) * unit)
            sell_shares = min(sell_shares, float(pos["shares"]))
            if sell_shares <= 0:
                _order(d, sid, "sell", "unfilled", "lot_rounding",
                       action="resize", reason_code="concentration_cap")
                resize_intents.pop(sid, None)
                continue
            _record_exit_trade(sid, pos, d, px, "concentration_cap",
                               idx - pos["entry_idx"], shares=sell_shares)
            _order(d, sid, "sell", "filled", "concentration_cap",
                   shares=sell_shares, price=px, intended_notional=target_value,
                   action="resize", reason_code="concentration_cap")
            portion_left = 1.0 - sell_shares / float(pos["shares"])
            pos["cost"] = float(pos["cost"]) * portion_left
            pos["shares"] = float(pos["shares"]) - sell_shares
            resize_intents.pop(sid, None)
            policy_audit["n_realized_resizes"] += 1
            if pos["shares"] <= 0:
                del positions[sid]

        # (c) 進場:只用**已經在手上**的現金,不預支未實現的賣出款。
        for sid, target_w in sorted(desired_targets.items(),
                                    key=lambda kv: (-kv[1], kv[0])):
            if sid in positions or sid in exit_intents:
                continue
            notional = float(desired_notional.get(sid, target_w * equity))
            if disp_days and d in disp_days.get(sid, ()):
                n_disp_skip += 1
                policy_audit["disposition_entry_blocks"] += 1
                _order(d, sid, "buy", "unfilled", "disposition_no_new_position",
                       intended_notional=notional, action="enter",
                       reason_code="new_top_k")
                continue
            bar, idx = _price_row(sid, d)
            if bar is None or idx == 0:
                _order(d, sid, "buy", "unfilled", "no_bar",
                       intended_notional=notional, action="enter",
                       reason_code="new_top_k")
                policy_audit["halted_or_missing_bar_delays"] += 1
                continue
            if config.BT_MODEL_LIMIT_LOCK:
                if _limit_lock(bar, float(price_cache[sid]["close"].iloc[idx - 1])) == "up":
                    n_limit_skip += 1
                    policy_audit["limit_up_entry_skips"] += 1
                    _order(d, sid, "buy", "unfilled", "limit_up_lock",
                           intended_notional=notional, action="enter",
                           reason_code="new_top_k")
                    continue
            entry_price = float(bar["open"])
            if not np.isfinite(entry_price) or entry_price <= 0:
                policy_audit["invalid_price_skips"] += 1
                _order(d, sid, "buy", "unfilled", "invalid_open_price",
                       intended_notional=notional, action="enter",
                       reason_code="new_top_k")
                continue
            alloc = min(notional, cash)
            shares, total_cost = (0.0, 0.0)
            if alloc > 0:
                shares, total_cost = size_long_order(
                    alloc, entry_price, mode=order_size_mode, costs=cost_model,
                    regular_lot_shares=getattr(config, "BT_REGULAR_LOT_SHARES", 1000))
            if shares <= 0:
                # 現金不足與湊不到一個交易單位是兩種不同的失敗,分開記:前者
                # 多半是「前面有一筆賣單沒成交」的下游效應,後者是資金情境問題。
                if alloc < notional - 1e-9:
                    policy_audit["insufficient_cash_entry_skips"] += 1
                    why = "insufficient_cash"
                else:
                    n_lot_skip += 1
                    policy_audit["lot_rounding_entry_skips"] += 1
                    why = "lot_rounding"
                _order(d, sid, "buy", "unfilled", why,
                       intended_notional=notional, action="enter",
                       reason_code="new_top_k")
                continue
            cash -= total_cost
            positions[sid] = {
                "name": sid, "composite": float(policy_scores.get(sid, np.nan)),
                "signal_date": all_dates[di - 1] if di > 0 else d,
                "entry_date": d, "entry_idx": idx, "entry_price": entry_price,
                "cost": total_cost, "shares": shares,
                "ma_exit_today": np.nan, "pending_ma_exit": False,
                "last_close": entry_price,
                "entry_di": di, "last_bar_di": di,
            }
            _order(d, sid, "buy", "filled", "new_top_k", shares=shares,
                   price=entry_price, intended_notional=notional,
                   action="enter", reason_code="new_top_k")
            policy_audit["n_realized_entries"] += 1

    def _policy_decide_at_close(di, d):
        """T 日收盤:用截至今天可得的排名快照形成 desired state。"""
        nonlocal desired_targets, desired_notional
        asof_snapshot = None
        for s in _policy_snapshot_dates:
            if s <= d:
                asof_snapshot = s
            else:
                break
        if asof_snapshot is None:
            return
        # 已經成交或已消失的部位不該留下殘餘意圖(否則 fail-closed 檢查會被
        # 一個不存在的 key 蒙混過去)。
        for sid in [s for s in exit_intents if s not in positions]:
            exit_intents.pop(sid, None)
        is_decision_day = d in _policy_decision_dates
        rows = []
        for sid, pos in positions.items():
            rows.append({
                "stock_id": sid,
                "weight": (pos["shares"] * pos["last_close"] / equity
                           if equity > 0 else 0.0),
                "entry_price": pos["entry_price"],
                "close": pos["last_close"],
                "holding_days": di - pos.get("entry_di", di),
                # 引擎手上還有沒有沒成交的退出意圖 —— policy 用它分辨
                # 「今天才跌破停損」與「早就跌破、只是賣不掉」。
                "exit_pending": sid in exit_intents,
            })
        holdings_frame = pd.DataFrame(rows, columns=[
            "stock_id", "weight", "entry_price", "close", "holding_days",
            "exit_pending"])
        regime = _regime_map.get(d, "risk_on")
        next_exec = (all_dates[di + 1] if di + 1 < len(all_dates)
                     else pd.Timestamp(d) + pd.Timedelta(days=1))
        decision = strategy_position_policy.decide(
            as_of=d, signals=signal_snapshots[asof_snapshot],
            holdings=holdings_frame, equity=float(equity), regime=regime,
            is_decision_day=is_decision_day, next_execution=next_exec)

        policy_audit["n_policy_snapshots"] += 1
        if is_decision_day:
            policy_audit["n_decision_days"] += 1
        if not decision.snapshot_complete:
            policy_audit["n_snapshot_incomplete_days"] += 1
        for row in decision.actions.to_dict(orient="records"):
            decision_log.append({
                "date": d, "regime": regime,
                "is_decision_day": bool(is_decision_day),
                "snapshot_asof": asof_snapshot,
                "snapshot_complete": bool(decision.snapshot_complete),
                **row,
            })
        for sid, score in zip(signal_snapshots[asof_snapshot]["stock_id"],
                              signal_snapshots[asof_snapshot].get(
                                  "raw_score", pd.Series(dtype=float))):
            policy_scores[str(sid)] = float(score) if pd.notna(score) else np.nan

        new_exits = decision.exits()
        if is_decision_day:
            desired_targets = decision.target_map()
            desired_notional = decision.notional_map()
            policy_audit["n_desired_entries"] += sum(
                1 for sid in desired_targets if sid not in positions)
            target_portfolio.append({
                "date": d, "regime": regime,
                "equity": float(equity),
                "target_cash_weight": float(decision.target_cash_weight),
                "targets": decision.targets.to_dict(orient="records"),
                "fingerprint": decision.fingerprint,
            })
        else:
            # 非決策日只允許移除(強制/風險退出),不得新增排名換股。
            for sid in new_exits:
                desired_targets.pop(sid, None)
                desired_notional.pop(sid, None)
        for sid, reason in new_exits.items():
            if sid in positions and sid not in exit_intents:
                # 已存在的意圖不覆寫:reason_code 要指向**當初做決定的那一天**,
                # 否則一筆賣不掉的排名退出會在跌停第二天被改寫成 risk_stop。
                exit_intents[sid] = reason
                policy_audit["n_desired_exits"] += 1
        for row in decision.actions.itertuples():
            if str(row.action) == "resize":
                resize_intents[str(row.stock_id)] = float(row.target_weight) * float(equity)
                policy_audit["n_desired_resizes"] += 1
        # snapshot 不完整時不得把「沒出現在名單裡的持股」當成 target 0
        # (規格 §6)。policy 已經負責不產生那種退出,這裡再驗一次結果。
        for sid in positions:
            if sid not in desired_targets and sid not in exit_intents:
                raise RuntimeError(
                    f"[fail-closed] {sid} 既不在 target portfolio 也沒有退出意圖:"
                    "desired state 不完整,拒絕讓部位靜默留在帳上")

    policy_scores: Dict[str, float] = {}

    for di, d in enumerate(all_dates):
        if policy_enabled:
            _policy_settle_desired(di, d)
            equity = _mark_to_market(di, d)
            equity_curve.append((d, equity))
            _policy_decide_at_close(di, d)
            continue

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
                # 判定與結算和 policy 路徑共用 `_settle_stale_delisted`(欄位逐項等價:
                # 整筆賣出時 portion=1.0,cost_part 就是 pos["cost"])。
                _settle_stale_delisted(di, d, sid, pos)
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
        equity = _mark_to_market(di, d)
        equity_curve.append((d, equity))

    # ── 結算：用每日淨值算正確的績效指標 ────────────────────────────
    if not trades and len(positions) == 0:
        empty = {"error": "回測期間無任何交易（可能門檻太高或樣本太少）", "n_trades": 0}
        # 「一筆都沒成交」很可能正是證券別閘門擋光了外部訊號(例如整組 picks 都是
        # DR/興櫃)。少了這份統計就只剩一句「門檻太高或樣本太少」,查不到真因。
        empty.update(_security_type_provenance(_security_exclusions))
        if policy_enabled:
            # policy 路徑「一筆都沒成交」本身常常就是結論(全被處置禁倉/一字漲停/
            # 現金不足擋掉)。沒有這幾份紀錄就只剩一句無法追查的錯誤字串。
            empty["decision_log"] = decision_log
            empty["order_log"] = order_log
            empty["desired_realized_audit"] = dict(policy_audit)
        return empty

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
            # 實際生效的 config 值(即使濾網關著也記):多支研究腳本會就地改寫
            # 這些全域參數,還原不完整時,污染只有從「實際值」看得出來。
            "config_rule": getattr(config, "MARKET_FILTER_RULE", None),
            "config_riskoff_weight": getattr(
                config, "MARKET_FILTER_RISKOFF_WEIGHT", None),
            "n_filter_exits": n_filter_exits,
            "n_regime_switches": n_regime_switches,
        },
        "universe": universe_info,
        "evaluation": _evaluation_provenance(evaluation_split_info, segment),
        # 這份結果是哪一份程式碼算出來的。dirty 工作樹 = 對不到 commit = 無法重現。
        "provenance": provenance.git_state(),
        "data": {
            "price_dataset": getattr(config, "PRICE_DATASET", "TaiwanStockPrice"),
            "adjusted_price": price_integrity.is_adjusted_price_dataset(
                getattr(config, "PRICE_DATASET", "TaiwanStockPrice")
            ),
            "snapshot_end": getattr(config, "SNAPSHOT_END_DATE", ""),
            # 自建還原價(除權息回溯)是否開啟:未還原資料集下,它決定了報酬序列
            # 本身,跟因子權重同級的 load-bearing 設定。
            "self_adjust_prices": bool(getattr(config, "SELF_ADJUST_PRICES", False)),
            "allow_unadjusted_backtest": bool(
                getattr(config, "ALLOW_UNADJUSTED_BACKTEST", False)),
            # 未還原價 + 逃生門開啟時為 True:此結果含公司行動污染,非已驗證績效。
            "integrity_bypassed": (
                not price_integrity.is_adjusted_price_dataset(
                    getattr(config, "PRICE_DATASET", "TaiwanStockPrice"))
                and bool(getattr(config, "ALLOW_UNADJUSTED_BACKTEST", False))
            ),
        },
        # 報酬口徑:這份權益曲線是含息還是不含息,以及與它比較的基準是哪一種。
        # 個股序列在自建/官方還原價下含息,而基準長年用 TAIEX 價格指數(不含息)
        # —— 實測每年憑空生出 2.86pp 超額、Sharpe 差 0.113。口徑不一致時
        # `summary_block()` 直接 raise,所以有 summary 就代表比較是同一把尺。
        "return_convention": return_convention.summary_block(),
        "params": {
            "exit_mode": config.BT_EXIT_MODE,
            "ma_exit": config.BT_MA_EXIT,
            "trend_stop": config.BT_TREND_STOP_LOSS,
            "max_hold": config.BT_MAX_HOLD_DAYS,
            "min_composite": config.MIN_COMPOSITE,
            "rebalance_every": rebalance_every,
            "rebalance_phase": rebalance_phase,
            "max_positions": max_positions,
            # 每個再平衡日最多從候選清單取幾檔(投組參數,過去只在呼叫端存在)。
            "top_n": int(top_n),
            "trend_guard": bool(getattr(config, "TREND_GUARD_ENABLED", False)),
            "stale_exit_days": getattr(config, "BT_STALE_EXIT_DAYS", None),
            "let_positions_run": bool(let_positions_run),
            "picks_source": ("strategy_position_policy" if policy_enabled
                             else "external_picks_by_date" if external_picks
                             else "engine_composite"),
            # 因子權重:最決定性的研究參數,過去完全不在 summary 裡 —— 同一份
            # 報告換一組權重重跑,兩份結果長得一模一樣,沒有任何欄位區分得出來。
            "factor_weights": dict(getattr(config, "FACTOR_WEIGHTS", {}) or {}),
            # external picks 時引擎不算 composite,權重當下沒有作用:記下來但
            # 標明沒生效,免得日後把它讀成「這組權重產生了這個績效」。
            "factor_weights_applied": not (external_picks or policy_enabled),
            # 策略單元自己的訊號/投組規格(呼叫端傳 StrategySpec 才有)。
            "strategy": (strategy_spec.rules()
                         if hasattr(strategy_spec, "rules") else strategy_spec),
        },
    }
    result = {"summary": summary, "trades": tdf, "equity_curve": eq.reset_index()}
    if not policy_enabled:
        # policy 關閉時**一個新 key 都不加**:legacy summary/trades/回傳結構要能
        # 逐位元對得起來,否則「行為不變」只能靠讀 code 相信(規格 §7 的相容要求)。
        return result

    # ── StrategyPositionPolicy 的稽核輸出(規格 §7)────────────────────────
    # 規則、desired state、realized state 與「為什麼沒成交」都要能由結果重建。
    signal_lo, signal_hi = _policy_snapshot_dates[0], _policy_snapshot_dates[-1]
    exit_stats: Dict[str, Dict[str, float]] = {}
    if not tdf.empty:
        for reason, grp in tdf.groupby("exit_reason"):
            exit_stats[str(reason)] = {
                "n": int(len(grp)),
                "avg_hold_bars": round(float(grp["hold_bars"].mean()), 2),
                "avg_realized_ret": round(float(grp["ret"].mean()), 4),
                "median_realized_ret": round(float(grp["ret"].median()), 4),
                "win_rate": round(float((grp["ret"] > 0).mean()), 4),
            }
    summary["eval_audit"].update({
        "signal_window": [str(signal_lo)[:10], str(signal_hi)[:10]],
        "n_decision_days": int(policy_audit["n_decision_days"]),
        # policy 路徑的評估窗上界由呼叫端的 end_date 決定(見上方說明),所以
        # 「跑超過最後一個快照日幾天」必須誠實記下來讓人檢查,不是零就好。
        "days_beyond_last_signal_snapshot": int(
            sum(1 for x in all_dates if x > signal_hi)),
        "end_date_declared": bool(end_date),
    })
    summary["strategy_position_policy"] = {
        "enabled": True,
        "rules": strategy_position_policy.rules(),
        "rules_hash": strategy_position_policy.rules_hash(),
        # 決策頻率的語意在訊號那端;引擎照 snapshot 日期走,不自己推星期幾。
        "decision_day_source": "signal_frame_snapshot_dates",
        "n_decision_days": int(policy_audit["n_decision_days"]),
        "n_policy_snapshots": int(policy_audit["n_policy_snapshots"]),
        "regime_source": ("caller_regime_by_date" if regime_by_date
                          else "default_risk_on_no_external_regime"),
        "regime_pit_provenance": bool(regime_by_date),
        "desired_realized_audit": dict(policy_audit),
        "exit_reason_stats": exit_stats,
        "capital_scenario": {
            "initial_capital": initial_capital,
            "order_size_mode": order_size_mode.value,
            "minimum_commission": float(cost_model.minimum_commission),
            "source": "immutable_backtest_request",
        },
        "snapshot_complete_all_days": bool(
            policy_audit["n_snapshot_incomplete_days"] == 0),
        "target_portfolio": target_portfolio,
    }
    result["decision_log"] = decision_log
    result["order_log"] = order_log
    result["target_portfolio"] = target_portfolio
    return result


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


def _run_full_rules_hash(**engine: Any) -> str:
    """`run_full` 這條路徑的規則識別碼(holdout 台帳的 `strategy_hash`)。

    為什麼不是 manifest 的 `rules_sha256_16`:`run_full` 不吃凍結的
    `StrategySpec` —— 它跑的是 config 的 `FACTOR_WEIGHTS` 合成分數 + CLI 傳進來
    的投組參數。所以識別碼取「凍結時會凍的那一整組 config」+「這次的引擎參數」,
    兩者都是會改變 OS 數字的東西。雜湊本身走
    `evaluation.holdout.rules_fingerprint`(和 `freeze_manifest.rules_hash` 同一份
    實作),換規則就換 hash,台帳才分得出「同一套規則又看了一次」與「另一套規則
    第一次看」。
    """
    # 延後 import:freeze_manifest 會經 strategies 反向指回 backtest,模組層
    # import 會繞成環。這裡一次 run_full 只呼叫一次,成本可以忽略。
    import freeze_manifest
    return holdout_ledger.rules_fingerprint({
        "config": freeze_manifest.frozen_config(),
        "strategy": None,               # 這條路徑沒有策略單元規格
        "engine": dict(sorted(engine.items())),
    })


def run_full(sample: bool = True, top_n: int = 3, rebalance_every: int = 5,
             pool: Optional[int] = None,
             dynamic_enabled: Optional[bool] = None,
             universe_top_n: Optional[int] = None,
             static_comparator: bool = False,
             single_phase_debug: bool = False):
    """一次跑完整體回測 + 因子IC，並印報告。

    `static_comparator=True` = 關掉 dynamic universe、用 legacy 單日靜態池跑對照組。
    這條路徑刻意保留(它是偏誤對照組),但結果會在 summary 標
    `formal_evidence_eligible=False`,不可當正式證據。

    `single_phase_debug=True` 只跑 phase 0,**僅供 debug**(例如快速看引擎有沒有
    跑通)。相位掃描走 `evaluation.phases.sweep_phases` —— 正式 IS/OS 與
    `forward_test` 共用同一份實作,旗標會一路標進 `phase_stats`,單相位的數字
    不得當成策略績效。
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
    if single_phase_debug:
        print("[backtest] ⚠ single_phase_debug:只跑 phase 0,這是 debug 路徑,"
              "單一相位的績效只是一條路徑,不可作正式證據。\n")
    else:
        print(f"[backtest] 每段跑滿 {rebalance_every} 個等價再平衡相位；"
              "決策看中位數與最小值。\n")

    trade_frames = []
    results = {}
    sweeps = {}
    for segment, (start, end) in {"IS": split.is_window,
                                  "OS": split.os_window}.items():

        def _run_phase(phase: int, segment=segment, start=start, end=end):
            """單一相位的 body。掃描本身交給 evaluation.phases.sweep_phases,
            這裡不再自己寫 `for phase in range(...)` —— 那份手寫迴圈跟 S19 的
            那份連相位數的決定方式都不同,是 P1-1 要收掉的重複實作。"""
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
                # IS/embargo/OS 的固定日期跟著每一段結果走:少了它,事後只能
                # 從 period 猜這個 Sharpe 是 IS 還是 OS 的。
                evaluation_split_info=split, segment=segment,
            )
            results[(segment, phase)] = res
            if "summary" not in res:
                return {"segment": segment, "phase": phase,
                        "error": res.get("error", "?")}
            summary = res["summary"]
            actual_end = pd.Timestamp(summary["eval_audit"]["eval_window"][1])
            if actual_end > pd.Timestamp(end):
                raise RuntimeError(
                    f"{segment} phase={phase} 評估窗溢出 {actual_end.date()} > {end}"
                )
            if "trades" in res and not res["trades"].empty:
                trades = res["trades"].copy()
                trades["segment"] = segment
                trades["rebalance_phase"] = phase
                trade_frames.append(trades)
            return {
                "segment": segment, "phase": phase,
                "n_trades": summary["n_trades"],
                "ann_ret": summary["ann_ret"], "sharpe": summary["sharpe"],
                "max_drawdown": summary["max_drawdown"],
                "cum_ret": summary["cum_ret"],
                "integrity_bypassed": summary["data"]["integrity_bypassed"],
                "survivorship_free": summary["universe"].get("survivorship_free", False),
                "eval_end": summary["eval_audit"]["eval_window"][1],
            }

        sweeps[segment] = sweep_phases(
            _run_phase, n_phases=rebalance_every,
            single_phase_debug=single_phase_debug,
            stats_kwargs={"drawdown_col": "max_drawdown"},
        )

    phase_df = phase_combine(list(sweeps.values()))
    segment_stats = {seg: sw.stats() for seg, sw in sweeps.items()}
    print("=" * 94)
    print(f"  {'段':<4}{'相位':>5}{'交易':>7}{'年化':>10}{'Sharpe':>10}{'MaxDD':>10}{'累積':>10}")
    print("-" * 94)
    valid = phase_df[phase_df.get("error").isna()] if "error" in phase_df else phase_df
    for _, row in valid.iterrows():
        print(f"  {row['segment']:<4}{int(row['phase']):>5}{int(row['n_trades']):>7}"
              f"{row['ann_ret']:>10.1%}{row['sharpe']:>10.2f}"
              f"{row['max_drawdown']:>10.1%}{row['cum_ret']:>10.1%}")
    print("-" * 94)
    for segment, st in segment_stats.items():
        if not st.get("n_phases"):
            print(f"  {segment} 相位摘要：無有效相位")
            continue
        debug = "（single_phase_debug，非正式證據）" if st["single_phase_debug"] else ""
        print(f"  {segment} 相位摘要：Sharpe 中位 {st['sharpe_median']:.2f} / "
              f"最小 {st['sharpe_min']:.2f}；MaxDD 最差 "
              f"{st['worst_max_drawdown']:.1%}{debug}")

    # ── holdout 揭露:跑出 OS 數字 = 看過那段 holdout,一律 append 進台帳 ──
    # OS 邊界隨快照滑動(見 evaluation/holdout.py 的說明),沒有台帳的話,
    # 「上次已經當 OS 看過」這件事下次就查不到,重疊區間會被當成 fresh OOS 再報一次。
    os_sweep = sweeps.get("OS")
    holdout_record = None
    if os_sweep is not None and not os_sweep.empty:
        holdout_record = holdout_ledger.record_reveal(
            strategy_hash=_run_full_rules_hash(
                top_n=int(top_n), rebalance_every=int(rebalance_every),
                dynamic_enabled=bool(effective_dynamic),
                universe_top_n=int(universe_top_n),
                static_universe_comparator=bool(static_flag),
                sample=bool(sample),
            ),
            # run_full 沒有策略單元(走 config 的合成分數),所以不套任何策略名的
            # 既成 consumed 宣告 —— 那些宣告是綁在具名策略上的。
            strategy_name=None,
            os_start=split.os_window[0], os_end=split.os_window[1],
            source="backtest.run_full", segment="OS",
            is_window=split.is_window,
            embargo_trading_days=split.n_embargo,
            split_mode=split.mode,
            context={
                "sample": bool(sample),
                "top_n": int(top_n),
                "rebalance_every": int(rebalance_every),
                "dynamic_enabled": bool(effective_dynamic),
                "static_universe_comparator": bool(static_flag),
                "single_phase_debug": bool(single_phase_debug),
                "n_phases": segment_stats["OS"].get("n_phases"),
                # smoke sample、static 對照組、單相位 debug 都不是正式證據,
                # 但它們**仍然看過那段資料**,所以照樣入帳、只是標出來。
                "formal_evidence_eligible": not (sample or static_flag
                                                 or single_phase_debug),
            },
        )
        seen = ("⚠ 這段 OS 先前已被同一套規則看過"
                f"(重疊 {holdout_record['previously_seen_days']} 天,"
                f"真正沒看過的起點 {holdout_record['fresh_os_start']})"
                if holdout_record["holdout_previously_seen"]
                else "這套規則第一次揭露這段 OS")
        print(f"  holdout 台帳 #{holdout_record['seq']}："
              f"{holdout_record['holdout_status']}｜{seen}")
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

    # `single_phase_debug` 由掃描的**意圖**決定,不從列數反推:再平衡天數真的是 1
    # 的正式全相位掃描不該被誤標成 debug,反之單相位 debug 也不可以裝成全相位。
    return {"split": split.to_dict(), "phases": phase_df,
            "phase_stats": segment_stats,
            "single_phase_debug": bool(single_phase_debug),
            # 這次揭露 OS 的台帳紀錄(previously_seen 代表這段不是 fresh holdout)。
            "holdout": holdout_record,
            "results": results}, ic_results


if __name__ == "__main__":
    run_full(sample=True, top_n=3, rebalance_every=5)
