# -*- coding: utf-8 -*-
"""golden path:Python `make_signals()` → validator → policy → 唯一事件引擎 → artifacts。

**這不是第二套引擎。** 這裡一行損益都不算:所有績效數字都來自
`event_backtest.backtest_portfolio()`,本模組只負責組 request、跑滿五個等價 weekly
phase、把結果落成可稽核的檔案。

為什麼要有這支:`backtest_portfolio()` 同時服務三條路徑,參數面很寬。要正確跑
一次「外部橫斷面訊號的正式回測」,必須同時記得傳 PIT provider、資金情境、
成本模式、切割邊界與 strategy spec —— 少傳一個,結果不是壞掉,而是**安靜降級**
成不可作正式證據的東西。這支把那組正確組合固定下來。

CLI:

    PYTHONPATH=. .venv/bin/python -m research.golden_path \\
      --strategy s19_reference_make_signals --fixture synthetic \\
      --capital research --output-dir <dir>
"""
from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional

import numpy as np
import pandas as pd

from backtest import event_backtest
import config
import provenance
from evaluation.phases import PhaseSweep, sweep_phases
from research import artifacts
from research.contracts import (
    REQUEST_OWNED_KEYS, BacktestRequest, CandidateSpec, EvaluationProtocol)
from research.fixtures import Fixture, build_fixture
from research.holdout import (
    SEGMENT_IS, SEGMENT_OS, assert_within_segment, boundary_audit)
from research.signal_validation import validate_signal_frame
from research.screening import build_candidate_screen, format_candidate_screen
from strategy_kit import registry
from strategy_kit.contracts import DataRequirements, SignalContext
from strategy_kit.position_policy import (
    StrategyPositionPolicy,
    StrategyPositionPolicySpec,
)

CAPITAL_SCENARIOS: Dict[str, float] = {"research": 1_000_000.0,
                                       "personal": 500_000.0}
SCENARIO_ORDER_SIZE_MODE: Dict[str, str] = {"research": "research_fractional",
                                            "personal": "odd_lot_proxy"}
TRADING_DAYS = 252


@dataclass
class GoldenPathResult:
    run_dir: str
    manifest: Dict[str, Any]
    summary: Dict[str, Any]
    audit: Dict[str, Any]
    tables: Dict[str, pd.DataFrame]


# ── metrics(只從引擎給的權益曲線算,不重算損益)───────────────────────────
def _curve_metrics(equity: pd.DataFrame, initial: float) -> Dict[str, float]:
    eq = pd.to_numeric(equity["equity"], errors="coerce").dropna()
    if eq.empty:
        raise RuntimeError("[fail-closed] 權益曲線為空,無法計算 metrics")
    final = float(eq.iloc[-1])
    ret = eq.pct_change().dropna()
    ann_ret = float(ret.mean() * TRADING_DAYS) if len(ret) else 0.0
    ann_vol = (float(ret.std(ddof=1) * math.sqrt(TRADING_DAYS))
               if len(ret) > 1 else 0.0)
    downside = ret[ret < 0]
    dd_dev = (float(np.sqrt((downside ** 2).mean()) * math.sqrt(TRADING_DAYS))
              if len(downside) else 0.0)
    peak = eq.cummax()
    max_dd = float(((eq - peak) / peak).min()) if len(eq) else 0.0
    return {
        "initial_capital": float(initial),
        "final_capital": final,
        "net_profit": final - float(initial),
        "cum_return": final / float(initial) - 1.0,
        "ann_return": ann_ret,
        "ann_volatility": ann_vol,
        "sharpe": (ann_ret / ann_vol) if ann_vol > 0 else 0.0,
        "sortino": (ann_ret / dd_dev) if dd_dev > 0 else 0.0,
        "max_drawdown": max_dd,
    }


def _turnover(trades: pd.DataFrame, equity: pd.DataFrame) -> float:
    """單邊成交名目 / 平均淨值。研究用近似,不是券商口徑。"""
    if trades is None or trades.empty or "entry_cost" not in trades.columns:
        return 0.0
    mean_eq = float(pd.to_numeric(equity["equity"], errors="coerce").mean())
    if not math.isfinite(mean_eq) or mean_eq <= 0:
        return 0.0
    return float(pd.to_numeric(trades["entry_cost"], errors="coerce").sum() / mean_eq)


BENCHMARK_METHOD = "daily_equal_weight_rebalanced_eligible"


class BenchmarkUnavailableError(RuntimeError):
    """算不出合格基準;**不得**回傳 0% 假裝有基準。"""


@dataclass
class _PhaseRunBundle:
    """共用 phase runner 的最小回傳值；不另建第二套績效或 artifact 模型。"""

    sweep: PhaseSweep
    runs: Dict[int, Dict[str, Any]]
    policies: Dict[int, StrategyPositionPolicy]
    representative_phase: int


def _run_validated_signal_phases(*, signals: pd.DataFrame,
                                 policy_spec: StrategyPositionPolicySpec,
                                 initial_capital: float,
                                 order_size_mode: str,
                                 start_date, end_date,
                                 strategy_spec=None,
                                 universe_kwargs: Optional[Mapping[str, Any]] = None,
                                 engine_kwargs: Optional[Mapping[str, Any]] = None,
                                 n_phases: int = event_backtest.WEEKLY_PHASES,
                                 ) -> _PhaseRunBundle:
    """把**已驗證的日頻 SignalFrame**跑滿所有等價 phase。

    repo strategy 與外部序列化 SignalFrame 都走這一支。phase 掃描交給
    `evaluation.phases.sweep_phases()`；本函式只描述「一個 phase 怎麼跑」。
    """
    if not isinstance(policy_spec, StrategyPositionPolicySpec):
        raise TypeError("[fail-closed] policy_spec 必須是 StrategyPositionPolicySpec")

    all_days = sorted(pd.to_datetime(signals["date"].unique()))
    runs: Dict[int, Dict[str, Any]] = {}
    phase_policies: Dict[int, StrategyPositionPolicy] = {}
    universe_args = dict(universe_kwargs or {})
    engine_args = dict(engine_kwargs or {})

    def _run_phase(phase: int) -> Optional[Dict[str, Any]]:
        picked = set(event_backtest.select_decision_snapshots(
            all_days, decision_frequency=policy_spec.decision_frequency,
            phase=phase))
        sub = signals[signals["date"].isin(picked)]
        if sub.empty:
            return None

        # policy 有 stop lock 等路徑狀態；每一相位只能共用 frozen spec，不能共用物件。
        phase_policy = StrategyPositionPolicy(policy_spec)
        phase_policies[phase] = phase_policy
        result = event_backtest.backtest_portfolio(
            signal_frame=sub,
            strategy_position_policy=phase_policy,
            initial_capital=float(initial_capital),
            order_size_mode=str(order_size_mode),
            start_date=start_date,
            end_date=end_date,
            strategy_spec=strategy_spec,
            **universe_args,
            **engine_args,
        )
        if not isinstance(result, dict) or "summary" not in result:
            return None
        equity = result.get("equity_curve")
        if not isinstance(equity, pd.DataFrame) or equity.empty:
            raise RuntimeError(
                f"[fail-closed] phase {phase} 沒有權益曲線，不能當成完成的回測")
        metrics = _curve_metrics(equity, float(initial_capital))
        runs[phase] = result
        return {
            "phase": phase,
            "n_decision_days": len(picked),
            "n_trades": int(result["summary"].get("n_trades") or 0),
            **{k: metrics[k] for k in (
                "cum_return", "ann_return", "ann_volatility", "sharpe",
                "sortino", "max_drawdown")},
        }

    sweep = sweep_phases(_run_phase, n_phases=int(n_phases),
                         single_phase_debug=False)
    if len(sweep.rows) != int(n_phases):
        raise RuntimeError(
            f"[fail-closed] phase 只完成 {len(sweep.rows)}/{n_phases}；"
            f"缺少 {list(sweep.phases_without_result)}")

    ids = {id(p) for p in phase_policies.values()}
    if len(ids) != len(phase_policies):
        raise RuntimeError(
            "[fail-closed] 相位之間共用了同一個 policy instance；"
            "停損鎖與持倉狀態會跨相位污染")
    rule_hashes = {p.rules_hash() for p in phase_policies.values()}
    if len(rule_hashes) > 1:
        raise RuntimeError(
            f"[fail-closed] 相位之間的 policy 規則不一致:{sorted(rule_hashes)}")

    phases = sweep.rows.sort_values("phase").reset_index(drop=True)
    ordered = phases.sort_values(["sharpe", "phase"], kind="mergesort")
    representative = int(ordered.iloc[len(ordered) // 2]["phase"])
    return _PhaseRunBundle(
        sweep=PhaseSweep(
            rows=phases,
            n_phases_full=sweep.n_phases_full,
            phases_run=sweep.phases_run,
            phases_without_result=sweep.phases_without_result,
            single_phase_debug=sweep.single_phase_debug,
        ),
        runs=runs,
        policies=phase_policies,
        representative_phase=representative,
    )


def _equal_weight_benchmark(fixture: Fixture, equity: pd.DataFrame,
                            initial: float) -> Dict[str, Any]:
    """同口徑基準:每日對「當日 eligible 母體」等權再平衡。

    母體與順序都會改變答案,所以四個步驟的次序是規格的一部分(goal §3.3):

      1. **先在完整個股歷史上算每檔的日報酬。** 先篩成員再算 pct_change 的話,
         一檔股票在它剛進入 universe 那天會拿不到前收(前一天被篩掉了),報酬
         變成 NaN;成員資格變動越頻繁,基準被吃掉的報酬越多。
      2. **再篩當日 `in_dynamic_universe`。** 稠密 panel 為了讓 `ts_` 算子能算,
         刻意保留當日不可買的股票(local fixture 實測 86.7% 的列是非成員)。
         把它們平均進基準,等於拿一個策略根本不能買的母體當比較對象。
      3. 當日等權平均(等權 = 每日再平衡,不是買進持有)。
      4. 限制在與策略權益曲線相同的日期窗。

    名稱必須描述真正的算法:這是**每日再平衡**的等權組合,不是 buy-and-hold。
    兩者在成分變動時會明顯分岔,叫錯名字會讓人以為超額報酬的比較基礎是後者。

    算不出來就 raise:一個假的 0% 基準會讓任何正報酬策略看起來都有超額。
    """
    panel = fixture.panel
    if "in_dynamic_universe" not in panel.columns:
        raise BenchmarkUnavailableError(
            "[fail-closed] panel 沒有 in_dynamic_universe,無法界定基準母體。"
            "不回傳 0% 基準 —— 假基準會讓任何正報酬看起來都有超額")
    days = pd.to_datetime(equity["date"])
    lo, hi = days.min(), days.max()

    # 步驟 1:完整歷史上算報酬(不先套窗、不先篩成員)。
    close = panel.pivot_table(index="date", columns="stock_id", values="close")
    rets_all = close.sort_index().pct_change()
    # 步驟 2:當日成員遮罩。
    member = panel.pivot_table(index="date", columns="stock_id",
                               values="in_dynamic_universe", aggfunc="max")
    member = member.reindex(index=rets_all.index,
                            columns=rets_all.columns).fillna(False).astype(bool)
    eligible_rets = rets_all.where(member)
    # 步驟 4:套窗。用 `> lo` 而不是 `>= lo`:策略在權益曲線第一天的報酬是 0
    #        (`_curve_metrics` 對 equity 做 pct_change().dropna() 也丟掉第一天),
    #        基準若從 lo 當天就開始累積,會白拿一天策略沒有的報酬。
    in_window = (eligible_rets.index > lo) & (eligible_rets.index <= hi)
    windowed = eligible_rets.loc[in_window]
    # 步驟 3:當日等權平均(skipna:當天非成員不參與平均,也不當成 0%)。
    daily = windowed.mean(axis=1, skipna=True).dropna()
    n_names = windowed.notna().sum(axis=1)
    if daily.empty:
        raise BenchmarkUnavailableError(
            f"[fail-closed] 評估窗 [{lo.date()}, {hi.date()}] 內沒有任何 eligible "
            "個股報酬,算不出基準。不回傳 0% —— 那會讓任何正報酬看起來都有超額")

    curve = (1.0 + daily).cumprod()
    cum = float(curve.iloc[-1] - 1.0)
    ann = float(daily.mean() * TRADING_DAYS)
    vol = (float(daily.std(ddof=1) * math.sqrt(TRADING_DAYS))
           if len(daily) > 1 else 0.0)
    return {
        "method": BENCHMARK_METHOD,
        "population": "in_dynamic_universe_per_day",
        "rebalance": "daily_equal_weight",
        "return_convention": "same_series_as_strategy",
        "annualization": f"arithmetic_mean_x_{TRADING_DAYS}",
        "cum_return": cum, "ann_return": ann, "ann_volatility": vol,
        "sharpe": (ann / vol) if vol > 0 else 0.0,
        "n_days": int(len(daily)),
        "n_symbols_mean": float(n_names.loc[daily.index].mean()),
        "n_symbols_min": int(n_names.loc[daily.index].min()),
        "window": [str(lo.date()), str(hi.date())],
    }


MARKET_BENCHMARK_METHOD = "taiex_total_return_index"


def _market_benchmark(equity: pd.DataFrame) -> Dict[str, Any]:
    """大盤基準:**加權股價報酬指數**(含息),不是價格指數。

    為什麼一定要用報酬指數:個股價格走的是還原價(含息),拿它去比 TAIEX **價格**
    指數,等於白賺台股約 3~4% 的年殖利率。實測同一段窗:價格指數 +22.84%、
    報酬指數 +28.17% —— 那 5.3pp 純粹是配息,不是策略賺的。

    為什麼要有它(而不是只有等權基準):兩者回答不同問題 ——

      等權基準(`_equal_weight_benchmark`)  「我選股選得好不好?」
                                            同一個 universe 內比,排除 universe 的影響
      加權報酬指數(這一支)                 「我該做這個,還是買 0050?」
                                            投資決策要的是這個

    只報前者等於只回答研究問題。抓不到指數時回 `available=False` 並附原因,
    **不回 0%** —— 假的 0% 基準會讓任何正報酬都看起來有超額。
    """
    days = pd.to_datetime(equity["date"])
    lo, hi = days.min(), days.max()
    try:
        import data as _data
        ix = _data.fetch_market_total_return_index()
    except Exception as exc:                                # noqa: BLE001
        return {"method": MARKET_BENCHMARK_METHOD, "available": False,
                "reason": f"{type(exc).__name__}: {exc}"[:160]}
    if ix is None or getattr(ix, "empty", True):
        return {"method": MARKET_BENCHMARK_METHOD, "available": False,
                "reason": "報酬指數回空表"}
    ix = ix.copy()
    ix["date"] = pd.to_datetime(ix["date"])
    value_cols = [c for c in ix.columns if c != "date"]
    if not value_cols:
        return {"method": MARKET_BENCHMARK_METHOD, "available": False,
                "reason": "報酬指數沒有數值欄"}
    col = "close" if "close" in value_cols else value_cols[0]
    s = ix[(ix["date"] >= lo) & (ix["date"] <= hi)].sort_values("date")
    if len(s) < 3:
        return {"method": MARKET_BENCHMARK_METHOD, "available": False,
                "reason": f"窗內只有 {len(s)} 個指數點"}
    vals = pd.to_numeric(s[col], errors="coerce").dropna()
    if len(vals) < 3 or float(vals.iloc[0]) <= 0:
        return {"method": MARKET_BENCHMARK_METHOD, "available": False,
                "reason": "指數值無法解析"}
    r = vals.pct_change().dropna()
    ann = float(r.mean() * TRADING_DAYS)
    vol = float(r.std(ddof=1) * math.sqrt(TRADING_DAYS)) if len(r) > 1 else 0.0
    return {
        "method": MARKET_BENCHMARK_METHOD, "available": True,
        "series": col, "n_days": int(len(vals)),
        "cum_return": float(vals.iloc[-1] / vals.iloc[0] - 1.0),
        "ann_return": ann, "ann_volatility": vol,
        "sharpe": (ann / vol) if vol > 0 else 0.0,
        "window": [str(lo.date()), str(hi.date())],
        "note": "含息(報酬指數);與個股還原價同口徑",
    }


# ── 規則身分(唯一建構點)──────────────────────────────────────────────────
def build_candidate_spec(*, strategy_id: str,
                         params: Optional[Mapping[str, Any]] = None,
                         policy_spec: Optional[StrategyPositionPolicySpec] = None,
                         eligibility_rule_id: str) -> CandidateSpec:
    """規則身分的唯一建構點:**只吃宣告性輸入,不吃 panel、不吃資料窗**。

    抽出來的理由是一條紀律,不是整潔:`strategy_rule_hash` 的七個欄位全部在
    run 之前就已知(策略版本、參數、投組/退出規則、universe 規則、
    eligibility id、git commit),所以「凍結的規則有沒有被改過」這個問題
    **可以在載入任何 OS 資料之前**回答。它原本埋在 `run_golden_path()` 中段,
    於是只能在 OS 已經跑完之後才比對得出來 —— 2026-08-17 control_h4 就是這樣
    在 git commit 與 `signal_params`(多了 `trend_guard=True`)都變了的情況下,
    整段 OS 先被算完才被擋下。

    唯一一個要跑過才知道的欄位是 `eligibility_rule_id`(訊號欄的常數),因此
    它由呼叫端傳入:揭露前用凍結時記下的值,run 之後再用實際值比對一次。
    """
    strategy = registry.resolve(strategy_id)
    spec = policy_spec if policy_spec is not None else StrategyPositionPolicySpec()
    if not isinstance(spec, StrategyPositionPolicySpec):
        raise TypeError(
            "[fail-closed] policy_spec 必須是 StrategyPositionPolicySpec")
    return CandidateSpec(
        strategy_id=strategy_id,
        strategy_version=str(getattr(strategy, "version", "unknown")),
        signal_params=dict(params or strategy.default_parameters()),
        portfolio_params={k: v for k, v in spec.rules().items()
                          if k not in ("hard_stop_pct", "max_hold_days")},
        exit_params={"hard_stop_pct": spec.hard_stop_pct,
                     "max_hold_days": spec.max_hold_days},
        eligibility_rule_id=str(eligibility_rule_id),
        # `provenance.git_state()` 的鍵是 `git_commit`,不是 `commit`。
        # 原本取錯鍵 → 這裡永遠是空字串 → 規則指紋從來沒有綁到程式碼版本,
        # 而且因為預設值是 ""(合法),沒有任何地方會報錯。
        code_fingerprint=str(provenance.git_state().get("git_commit", "")),
    )


# ── 主流程 ────────────────────────────────────────────────────────────────
def run_golden_path(*, strategy_id: str, fixture_name: str = "synthetic",
                    capital: str = "research", output_dir,
                    params: Optional[Mapping[str, Any]] = None,
                    signal_frame: Optional[pd.DataFrame] = None,
                    policy: Optional[StrategyPositionPolicy] = None,
                    stamp: str = "run", fixture_kwargs: Optional[Mapping] = None,
                    engine_kwargs: Optional[Mapping[str, Any]] = None,
                    holdout_protocol=None, segment: Optional[str] = None,
                    fixture=None,
                    ) -> GoldenPathResult:
    if capital not in CAPITAL_SCENARIOS:
        raise ValueError(f"[fail-closed] 未知資金情境 {capital!r};"
                         f"可用 {sorted(CAPITAL_SCENARIOS)}")

    strategy = registry.resolve(strategy_id)
    # `StrategyPositionPolicy` 帶路徑相依狀態(持倉判斷、災難停損鎖、統計計數)。
    # 五個 phase 共用同一個 instance 的話,phase 0 停損鎖住的標的會擋掉 phase 3
    # 的買進 —— 五條「互相獨立的等價路徑」變成一條有記憶的鏈,而且結果會隨
    # 執行順序改變。所以這裡只從呼叫端拿**規則**(frozen spec),每個 phase 各自
    # 建自己的 instance(§3.2 的明確 clone,不是通用 DI)。
    policy_spec = policy.spec if policy is not None else StrategyPositionPolicySpec()
    if not isinstance(policy_spec, StrategyPositionPolicySpec):
        raise TypeError(
            "[fail-closed] policy.spec 必須是 StrategyPositionPolicySpec")

    # single-holdout:資料窗由 protocol 決定,而且在**建 panel 時**就生效
    # (規格 §2.1:不得先載入 OS 再只裁掉輸出)。
    window = None
    scoring = None
    if holdout_protocol is not None:
        if segment not in (SEGMENT_IS, SEGMENT_OS):
            raise ValueError(
                f"[fail-closed] 有 holdout protocol 時必須指定 segment;"
                f"得到 {segment!r}")
        window = holdout_protocol.window(segment)
        scoring = holdout_protocol.scoring_window(segment)
    if fixture is not None:
        # 重用已建好的 fixture(例如同一份 local panel 跑多支假說)。
        # 窗仍然要套 —— segment 邊界不因為「panel 是別人建的」而放行。
        import copy as _copy

        from research.fixtures import apply_window
        fixture = apply_window(_copy.copy(fixture), window)
    else:
        fixture = build_fixture(fixture_name, window=window,
                                **dict(fixture_kwargs or {}))

    requirements = DataRequirements.from_mapping(strategy.data_requirements())
    requirements.validate_panel(fixture.panel, who=f"{strategy_id}/{fixture_name}")

    context = SignalContext(
        as_of=pd.Timestamp(fixture.end_date),
        start_date=pd.Timestamp(fixture.start_date),
        end_date=pd.Timestamp(fixture.end_date),
        universe_provider_id=fixture.name,
        eligibility_rule_id="fixture_declared",
        mode="discovery" if fixture.name == "synthetic" else "validation",
    )
    # 外部已序列化訊號也能進**同一條完整 Golden Path**。它仍須對應一個 allowlisted
    # strategy id/version，並使用相同 fixture、validator、phase runner、benchmark 與
    # artifacts；不能只因為訊號已先算好就走較寬鬆的旁路。
    raw_signals = (signal_frame.copy(deep=True)
                   if signal_frame is not None
                   else strategy.make_signals(
                       fixture.panel, dict(params or {}), context))
    validated = validate_signal_frame(
        raw_signals, who=f"{strategy_id}/{fixture_name}",
        as_of_max=pd.Timestamp(fixture.end_date))
    signals = validated.frame
    actual_ids = set(signals["strategy_id"].astype(str))
    actual_versions = set(signals["strategy_version"].astype(str))
    expected_version = str(getattr(strategy, "version", "unknown"))
    if actual_ids != {strategy_id} or actual_versions != {expected_version}:
        raise ValueError(
            "[fail-closed] 外部 SignalFrame provenance 與 registry 不一致:"
            f" strategy_id={sorted(actual_ids)}(預期 {strategy_id!r}),"
            f" strategy_version={sorted(actual_versions)}"
            f"(預期 {expected_version!r})")
    if scoring is not None:
        lo, hi = pd.Timestamp(scoring[0]), pd.Timestamp(scoring[1])
        signals = signals[(signals["date"] >= lo) & (signals["date"] <= hi)]
        if signals.empty:
            raise ValueError(
                f"[fail-closed] 計分窗 [{lo.date()}, {hi.date()}] 內沒有訊號")

    spec = policy_spec
    # 走**同一個**建構點 —— reveal_locked_os() 的前置閘門也呼叫它。
    # 兩份等價實作遲早會分岔,而分岔的症狀是「揭露前算的 hash 與 run 後算的
    # hash 不同」,那樣閘門會變成噪音,最後被關掉。
    candidate = build_candidate_spec(
        strategy_id=strategy_id, params=params, policy_spec=spec,
        eligibility_rule_id=str(signals["eligibility_rule_id"].iloc[0]))
    protocol = EvaluationProtocol(
        data_snapshot=str(getattr(config, "SNAPSHOT_END_DATE", "")),
        price_dataset=str(getattr(config, "PRICE_DATASET", "")),
        adjustment_anchor=str(getattr(config, "PRICE_ADJUST_ANCHOR", "")),
        fixture=fixture.name, capital_scenario=capital,
        initial_capital=CAPITAL_SCENARIOS[capital],
        order_size_mode=SCENARIO_ORDER_SIZE_MODE[capital],
        minimum_commission=float(getattr(config, "BT_MIN_COMMISSION", 0.0)),
        phases=event_backtest.WEEKLY_PHASES,
        decision_frequency=str(spec.decision_frequency),
        return_convention="strategy_and_benchmark_share_one_price_series",
        segment="fixture" if fixture.name == "synthetic" else "reference",
    )
    request = BacktestRequest(candidate=candidate, protocol=protocol,
                              engine_kwargs=dict(engine_kwargs or {}))

    # ── 跑滿五個等價 weekly phase；repo/external 共用唯一 phase runner ──
    with fixture.engine_context():
        phase_bundle = _run_validated_signal_phases(
            signals=signals,
            policy_spec=policy_spec,
            initial_capital=protocol.initial_capital,
            order_size_mode=protocol.order_size_mode,
            start_date=(scoring[0] if scoring else fixture.start_date),
            end_date=(scoring[1] if scoring else fixture.end_date),
            strategy_spec=candidate,
            universe_kwargs=fixture.universe_kwargs,
            engine_kwargs=request.engine_kwargs,
            n_phases=protocol.phases,
        )

    phases = phase_bundle.sweep.rows
    stats = phase_bundle.sweep.stats()
    phase_policies = phase_bundle.policies
    ids = {id(p) for p in phase_policies.values()}
    rep_phase = phase_bundle.representative_phase
    rep = phase_bundle.runs[rep_phase]
    equity = rep["equity_curve"]
    trades = rep.get("trades", pd.DataFrame())
    metrics = _curve_metrics(equity, protocol.initial_capital)
    benchmark = _equal_weight_benchmark(fixture, equity, protocol.initial_capital)
    market = _market_benchmark(equity)

    n_trades = int(rep["summary"].get("n_trades") or 0)
    win_rate = float(rep["summary"].get("win_rate") or 0.0)
    summary = {
        **metrics,
        "turnover": _turnover(trades, equity),
        "n_trades": n_trades,
        "win_rate": win_rate,
        "benchmark": benchmark,
        "excess_vs_benchmark": metrics["cum_return"] - float(
            benchmark.get("cum_return", 0.0)),
        # 大盤基準與等權基準回答不同問題,兩個都報(見 `_market_benchmark`)。
        "benchmark_market": market,
        "excess_vs_market": (metrics["cum_return"] - float(market["cum_return"]))
                            if market.get("available") else None,
        "representative_phase": rep_phase,
        "phase_stats": stats,
        "engine_summary": rep["summary"],
        "signal_validation": validated.to_dict(),
    }

    uni = rep["summary"].get("universe") or {}
    pol_block = rep["summary"].get("strategy_position_policy") or {}
    ev = rep["summary"].get("eval_audit") or {}
    data_block = rep["summary"].get("data") or {}
    synthetic = fixture.name == "synthetic"

    # segment 邊界要先算出來才能進 checklist(不能先宣告通過再去驗)。
    bounds: Optional[Dict[str, Any]] = None
    if holdout_protocol is not None:
        panel_dates = fixture.panel["date"]
        bounds = boundary_audit(
            protocol=holdout_protocol, segment=segment,
            panel_input=(panel_dates.min(), panel_dates.max()),
            tables={"signals": signals,
                    "equity_curve": equity,
                    "decisions": rep.get("decision_log"),
                    "orders": rep.get("order_log"),
                    "trades": trades})
        assert_within_segment(bounds)

    git = provenance.git_state()
    if str(git.get("git_commit") or "") in ("", provenance.UNKNOWN):
        code_identity = "fail_no_commit"
    elif bool(git.get("git_dirty")):
        # 不是「有點髒」而已:dirty 工作樹代表這份數字對不到任何 commit,
        # 誰都無法從版控重現它。研究期間常態如此,所以它出現在 checklist 上,
        # 而不是讓 run 直接失敗。
        code_identity = "fail_dirty_worktree"
    else:
        code_identity = "pass"

    # ── 人可以直接讀的 checklist(goal §5)────────────────────────────────
    # 規則只有一條:除了兩個資訊欄位,其餘每一格都必須literally 是 "pass",
    # `formal_evidence_ready` 由此推導。不再有第二套 evidence 狀態機。
    checklist: Dict[str, str] = {
        "signal_validation": ("pass" if validated.formal_evidence_eligible
                              else f"fail_{validated.evidence_note[:40]}"),
        "pit_universe": ("pass" if uni.get("candidate_pool_pit") is True
                         else "fail_not_point_in_time"),
        "adjusted_price": (
            "pass" if (data_block.get("integrity_bypassed") is False
                       and bool(data_block.get("adjustment_anchor")))
            else "fail_integrity_bypassed_or_anchor_missing"),
        "evaluation_boundary": (
            "pass" if (ev.get("days_beyond_last_pick") == 0
                       and (bounds is None or bounds.get("within_segment") is True))
            else f"fail_days_beyond={ev.get('days_beyond_last_pick')}"),
        "all_phases": ("pass" if len(phases) == event_backtest.WEEKLY_PHASES
                       else f"fail_only_{len(phases)}_of_{event_backtest.WEEKLY_PHASES}"),
        "phase_independence": ("pass" if len(ids) == len(phase_policies)
                               else "fail_shared_policy_instance"),
        "benchmark": ("pass" if float(benchmark.get("n_days") or 0) > 0
                      else "fail_no_benchmark_days"),
        "code_identity": code_identity,
        "data_source": "pass" if not synthetic else "fail_synthetic_fixture_only",
        "strategy_evidence_status": (
            "pass" if str(getattr(strategy, "evidence_status", "")) == "validated"
            else f"fail_{getattr(strategy, 'evidence_status', 'unspecified')}"),
        "engine_universe_evidence": (
            "pass" if uni.get("formal_evidence_eligible") is True
            else "fail_engine_marked_not_eligible"),
        # ── 以下兩格是資訊,不參與 formal_evidence_ready 推導 ──
        # 沒有 holdout_protocol 時**不可以**說 "not_revealed" —— 那種 run 是在
        # 完整 panel 上不設邊界地計分,很可能整段掃過 locked OS。2026-08-16 就
        # 這樣燒掉了 H2 的 OS,而 audit 當時寫著 not_revealed,看起來完全合規。
        # 「沒有邊界」和「有邊界且沒揭露」是兩件事,不能共用同一個字。
        "os_status": ("unbounded_no_holdout_protocol" if holdout_protocol is None
                      else ("revealed" if segment == SEGMENT_OS else "not_revealed")),
        "performance_claim": "research_only",
    }
    INFORMATIONAL = ("os_status", "performance_claim")
    blockers: List[str] = [f"{k}={v}" for k, v in checklist.items()
                           if k not in INFORMATIONAL and v != "pass"]
    formal_ready = not blockers
    audit = {
        "checklist": checklist,
        "pipeline_complete": True,
        "real_event_engine_used": True,
        "signal_validator_passed": True,
        "all_weekly_phases_ran": len(phases) == event_backtest.WEEKLY_PHASES,
        "n_phases_ran": int(len(phases)),
        "single_phase_debug": False,
        "fixture": fixture.name,
        "fixture_offline": bool(fixture.offline),
        "fixture_readiness": fixture.readiness,
        "signal_validation": validated.to_dict(),
        "pit_universe": uni.get("candidate_pool_pit"),
        "price_integrity_bypassed": data_block.get("integrity_bypassed"),
        "adjustment_anchor": data_block.get("adjustment_anchor"),
        "price_space_execution": (rep["summary"].get("execution") or {}
                                  ).get("price_space_execution"),
        "common_stock_rule": (uni.get("excluded_by_security_type") or {}).get("rule"),
        "regime_evidence": pol_block.get("regime_evidence"),
        "snapshot_complete_all_days": pol_block.get("snapshot_complete_all_days"),
        "eval_audit": ev,
        "evidence_status": str(getattr(strategy, "evidence_status", "unspecified")),
        "formal_evidence_ready": formal_ready,
        "formal_evidence_blockers": blockers,
        "performance_claim": "none",
        "claim_boundary": (
            "本 run 只證明「策略 → 訊號 → 部位決策 → 真實事件模擬 → 結果檔」"
            "接通。管線跑通 != 策略有效,也 != 通過 clean OOS;"
            "策略證據等級一律以 STRATEGY_REGISTRY.md 為準。"),
        "provenance": provenance.git_state(),
    }

    if holdout_protocol is not None:
        audit["segment"] = segment
        audit["segment_boundary"] = bounds
        audit["holdout_protocol"] = holdout_protocol.to_dict()

    manifest = {**request.manifest(), "run_stamp": stamp,
                "n_signal_rows": int(len(signals)),
                "n_signal_days": int(signals["date"].nunique()),
                "representative_phase": rep_phase}

    candidate_screen = build_candidate_screen(
        signals, panel=fixture.panel, top_n=int(spec.entry_rank))

    run_id = artifacts.build_run_id(strategy_id=strategy_id,
                                    run_hash=request.evaluation_run_hash(),
                                    stamp=stamp)
    run = artifacts.create_run_directory(output_dir, run_id)
    tables = {
        "signals": signals,
        "phase_results": phases,
        "decisions": pd.DataFrame(rep.get("decision_log") or []),
        "orders": pd.DataFrame(rep.get("order_log") or []),
        "trades": trades if isinstance(trades, pd.DataFrame) else pd.DataFrame(),
        "equity_curve": equity,
        "candidate_screen": candidate_screen,
    }
    artifacts.write_run(run, manifest=manifest, summary=summary, audit=audit,
                        tables=tables)
    artifacts.write_text(run, "candidate_screen",
                         format_candidate_screen(candidate_screen))
    return GoldenPathResult(run_dir=str(run.path), manifest=manifest,
                            summary=summary, audit=audit, tables=tables)


# ── 既有 signal_frame 入口(policy_research_run 轉呼叫這裡)──────────────
# 為什麼搬過來:goal 明文「不得保留兩套 runner 邏輯」。舊入口吃的是**已經算好
# 的 signal_frame**,新入口吃的是 strategy_id;前者是後者的子集,所以邏輯只留
# 一份在 research 層,policy_research_run 只剩 re-export。
def run_signal_frame_backtest(*,
                        signal_frame,
                        policy: Optional[StrategyPositionPolicy] = None,
                        capital: str = "research",
                        start_date: Optional[str] = None,
                        end_date: Optional[str] = None,
                        universe=None,
                        regime_by_date=None,
                        strategy_spec=None,
                        evaluation_split_info=None,
                        segment: Optional[str] = None,
                        order_size_mode: Optional[str] = None,
                        minimum_commission: Optional[float] = None,
                        **engine_kwargs) -> Dict[str, Any]:
    """把外部日頻 SignalFrame 跑成完整五相位 policy 回測。

    - **訊號先過 validator。** 這裡與 `run_golden_path` 共用同一支
      `validate_signal_frame`,不為外部訊號開第二條比較寬鬆的路。外部訊號之所以
      危險,不是因為來自別處,而是因為沒人替它檢查 key 唯一性、排名母體、快照
      完整性與 as-of 邊界 —— 若 repo 內策略走嚴格路、外部走寬鬆路,那條寬鬆路
      遲早變成大家繞過檢查的門。
    - phase 選擇與 repo strategy 共用 `_run_validated_signal_phases()`；呼叫端不用
      先挑一個星期幾。回傳值保留代表 phase 的相容欄位，另附 `phase_results`、
      `phase_stats` 與 `representative_phase`。
    - 候選池預設走 `universes.historical_pit_universe()`(月頻 PIT),不讓呼叫端
      自己湊 symbols —— 那正是 2026-08 之前所有研究腳本靜默退回單日靜態池的原因。
    - 資金情境是 immutable request 參數,**不寫回全域 config**,所以同一個
      process 連續跑 research 與 personal 兩次不會互相污染。
    - `engine_kwargs` 不得覆寫 runner 自己管理的欄位(見 `REQUEST_OWNED_KEYS`)。
    - 其餘閘門(價格完整性、稠密 panel、普通股白名單、T+1、漲跌停、處置、成本、
      provenance)沿用引擎既有的,這裡一個都不繞過。
    """
    if capital not in CAPITAL_SCENARIOS:
        raise ValueError(
            f"[fail-closed] 未知的資金情境 {capital!r};"
            f"可用:{sorted(CAPITAL_SCENARIOS)}")

    clashes = sorted(set(engine_kwargs) & set(REQUEST_OWNED_KEYS))
    if clashes:
        raise ValueError(
            f"[fail-closed] engine_kwargs 想覆寫 runner 擁有的欄位 {clashes}。"
            "這些是 PIT provider、資金情境、評估窗與 segment 的來源;"
            "能被一個 dict 靜默蓋掉的話,前面所有閘門都等於沒有")

    # 同一支 validator,不為外部訊號開特例(§3.1)。
    validated = validate_signal_frame(
        signal_frame, who="run_signal_frame_backtest",
        as_of_max=(pd.Timestamp(end_date) if end_date else None))
    signal_frame = validated.frame

    policy_spec = (policy.spec if policy is not None
                   else StrategyPositionPolicySpec())
    if not isinstance(policy_spec, StrategyPositionPolicySpec):
        raise TypeError(
            "[fail-closed] policy.spec 必須是 StrategyPositionPolicySpec")

    if universe is None:
        from universes import historical_pit_universe
        universe = historical_pit_universe()
    uni_kwargs = universe.backtest_kwargs()

    request = dict(engine_kwargs)
    request.update(evaluation_split_info=evaluation_split_info,
                   segment=segment)
    if minimum_commission is not None:
        request["minimum_commission"] = float(minimum_commission)
    if regime_by_date is not None:
        request["regime_by_date"] = regime_by_date

    bundle = _run_validated_signal_phases(
        signals=signal_frame,
        policy_spec=policy_spec,
        initial_capital=CAPITAL_SCENARIOS[capital],
        order_size_mode=(order_size_mode or SCENARIO_ORDER_SIZE_MODE[capital]),
        start_date=start_date,
        end_date=end_date,
        strategy_spec=strategy_spec,
        universe_kwargs=uni_kwargs,
        engine_kwargs=request,
        n_phases=event_backtest.WEEKLY_PHASES,
    )
    result = dict(bundle.runs[bundle.representative_phase])
    result["capital_scenario_name"] = capital
    result["signal_validation"] = validated.to_dict()
    result["phase_results"] = bundle.sweep.rows
    result["phase_stats"] = bundle.sweep.stats()
    result["representative_phase"] = bundle.representative_phase
    return result


# ── 稽核:這份結果能不能當正式證據 ─────────────────────────────────────────
def audit_summary(result: Dict[str, Any]) -> Dict[str, Any]:
    """把散在 summary 各段的正式證據條件攤成一張表。

    每一項都對應一個實際發生過、會產生假結論的缺陷,所以判定一律取「明確為真」
    才算通過;欄位缺失一律視為未通過(不知道 != 沒問題)。
    """
    summary = (result or {}).get("summary") or {}
    uni = summary.get("universe") or {}
    data = summary.get("data") or {}
    ev = summary.get("eval_audit") or {}
    pol = summary.get("strategy_position_policy") or {}
    excluded = uni.get("excluded_by_security_type") or {}

    days_beyond = ev.get("days_beyond_last_pick")
    checks = {
        # 訊號用完後仍繼續 MTM = 把後段行情算進這一段(實測曾讓 IS Sharpe
        # 從 0.306 變成 1.607)。
        "eval_window_not_overflowing": days_beyond == 0,
        # 候選池是不是月頻 PIT(不是就代表用了單日靜態池回套歷史)。
        "pit_universe": uni.get("candidate_pool_pit") is True,
        # 未還原價逃生門有沒有被打開。
        "price_integrity_not_bypassed": data.get("integrity_bypassed") is False,
        # 興櫃沒有 ±10% 漲跌停,混進來會系統性灌高動能策略的 Sharpe。
        # 判準是「這次 request 真的套了普通股白名單」——`rule` 由 request 級
        # collector 填,沒開 collector 就沒有這個欄位,那代表閘門沒生效。
        "common_stock_only": bool(excluded.get("rule")),
        # 裸字串 regime 不算 provenance。
        "regime_verified": pol.get("regime_evidence") in ("verified",
                                                          "none_constant_risk_on"),
        # 訊號快照完整性:缺旗標時未出現的持股只能當 unknown,不可自動賣。
        "snapshot_complete_all_days": pol.get("snapshot_complete_all_days") is True,
        # 引擎自己的總結論。
        "formal_evidence_eligible": uni.get("formal_evidence_eligible") is True,
    }
    return {
        "checks": checks,
        "formal_evidence_ready": all(checks.values()),
        "days_beyond_last_pick": days_beyond,
        "capital_scenario": pol.get("capital_scenario"),
        "policy_rules_hash": pol.get("rules_hash"),
        "regime_evidence": pol.get("regime_evidence"),
        "excluded_by_security_type": excluded,
        "evidence_note": uni.get("evidence_note"),
        "cash_audit": pol.get("cash_audit"),
        "desired_realized_audit": pol.get("desired_realized_audit"),
        "exit_reason_stats": pol.get("exit_reason_stats"),
        "period": summary.get("period"),
        "n_trades": summary.get("n_trades"),
    }


def format_audit(audit: Dict[str, Any]) -> str:
    """把稽核表印成人看得懂的一段;**只描述管線狀態,不評價策略**。"""
    lines = ["=" * 72, "  policy backtest 稽核摘要", "=" * 72]
    for name, ok in (audit.get("checks") or {}).items():
        lines.append(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    lines.append("-" * 72)
    lines.append(f"  formal_evidence_ready = {audit.get('formal_evidence_ready')}")
    lines.append(f"  period                = {audit.get('period')}")
    lines.append(f"  n_trades              = {audit.get('n_trades')}")
    lines.append(f"  policy_rules_hash     = {audit.get('policy_rules_hash')}")
    lines.append(f"  capital_scenario      = {audit.get('capital_scenario')}")
    if audit.get("evidence_note"):
        lines.append(f"  evidence_note         = {audit['evidence_note']}")
    lines.append("-" * 72)
    lines.append("  註:本摘要只說明「管線是否在合格條件下跑完」。")
    lines.append("      管線跑通 != 策略有效,也 != 通過 clean OOS。")
    lines.append("      策略證據等級一律以 STRATEGY_REGISTRY.md 為準。")
    lines.append("=" * 72)
    return "\n".join(lines)


def _default_output_dir() -> str:
    return str(config.OUTPUT_DIR / "research_runs")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="research.golden_path",
        description="Python make_signals → policy → 唯一事件引擎 → 可稽核結果")
    ap.add_argument("--strategy", required=True, choices=registry.available())
    ap.add_argument("--fixture", default="synthetic",
                    choices=("synthetic", "local"))
    ap.add_argument("--capital", default="research",
                    choices=sorted(CAPITAL_SCENARIOS))
    ap.add_argument("--output-dir", default=None)
    ap.add_argument("--stamp", default="run")
    ap.add_argument("--candidate-pool-n", type=int, default=None)
    args = ap.parse_args(argv)

    fixture_kwargs = ({"candidate_pool_n": args.candidate_pool_n}
                      if args.candidate_pool_n else {})
    try:
        result = run_golden_path(
            strategy_id=args.strategy, fixture_name=args.fixture,
            capital=args.capital,
            output_dir=args.output_dir or _default_output_dir(),
            stamp=args.stamp, fixture_kwargs=fixture_kwargs)
    except Exception as exc:                            # noqa: BLE001
        print(f"[golden_path] FAILED: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 1

    print(f"[golden_path] run dir: {result.run_dir}")
    print(f"[golden_path] formal_evidence_ready="
          f"{result.audit['formal_evidence_ready']} "
          f"performance_claim={result.audit['performance_claim']}")
    print("[golden_path] 管線跑通 != 策略有效,也 != 通過 clean OOS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
