# -*- coding: utf-8 -*-
"""外部 make_signals → StrategyPositionPolicy → 事件引擎 的**唯一研究入口**。

為什麼需要這支檔案:`backtest_portfolio()` 的參數面很寬(它同時服務 legacy
picks_by_date、engine composite 與 policy 三條路徑),要正確跑一次「外部橫斷面
訊號的正式回測」必須同時記得傳 PIT provider、資金情境、成本模式、切割邊界與
strategy spec —— 少傳一個,結果不是壞掉,而是**安靜地降級**成不可作正式證據的
東西。這支檔案把那組正確組合固定下來,並在跑完後把「這份結果能不能當正式證據」
攤成一張明確的稽核表。

它**不是**第二套回測引擎:所有計算都轉給 `backtest.backtest_portfolio()`,
這裡只負責組 request 與讀 summary。

用法:

    from policy_research_run import run_policy_backtest, audit_summary
    res = run_policy_backtest(signal_frame=frame, capital="research")
    print(format_audit(audit_summary(res)))

命令列(reference run,S19 只當管線驗收案例,不是績效宣稱):

    PYTHONPATH=. .venv/bin/python policy_research_run.py --capital research
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import backtest
import config
from strategies.position_policy import (
    StrategyPositionPolicy,
    StrategyPositionPolicySpec,
)


# 規格 §4.1:兩個不可混淆的資金情境。初始資金屬於 immutable backtest request,
# 不屬於 signal —— 同一個 policy 必須能在兩個情境重跑而互不污染。
CAPITAL_SCENARIOS: Dict[str, float] = {
    "research": 1_000_000.0,
    "personal": 500_000.0,
}

# 50 萬、10 檔的個人情境需要 integer-share 的 odd-lot proxy(規格 §4.1)。
# proxy 沒有獨立零股行情,所以 summary 會留 warning,不得宣稱精確成交。
SCENARIO_ORDER_SIZE_MODE: Dict[str, str] = {
    "research": "research_fractional",
    "personal": "odd_lot_proxy",
}


def run_policy_backtest(*,
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
    """跑一次 policy 路徑的正式回測。

    - 候選池預設走 `universes.historical_pit_universe()`(月頻 PIT),不讓呼叫端
      自己湊 symbols —— 那正是 2026-08 之前所有研究腳本靜默退回單日靜態池的原因。
    - 資金情境是 immutable request 參數,**不寫回全域 config**,所以同一個
      process 連續跑 research 與 personal 兩次不會互相污染。
    - 其餘閘門(價格完整性、稠密 panel、普通股白名單、T+1、漲跌停、處置、成本、
      provenance)沿用引擎既有的,這裡一個都不繞過。
    """
    if capital not in CAPITAL_SCENARIOS:
        raise ValueError(
            f"[fail-closed] 未知的資金情境 {capital!r};"
            f"可用:{sorted(CAPITAL_SCENARIOS)}")

    pol = policy or StrategyPositionPolicy(StrategyPositionPolicySpec())

    if universe is None:
        from universes import historical_pit_universe
        universe = historical_pit_universe()
    uni_kwargs = universe.backtest_kwargs()

    request = dict(uni_kwargs)
    request.update(
        signal_frame=signal_frame,
        strategy_position_policy=pol,
        initial_capital=CAPITAL_SCENARIOS[capital],
        order_size_mode=(order_size_mode
                         or SCENARIO_ORDER_SIZE_MODE[capital]),
        start_date=start_date,
        end_date=end_date,
        strategy_spec=strategy_spec,
        evaluation_split_info=evaluation_split_info,
        segment=segment,
    )
    if minimum_commission is not None:
        request["minimum_commission"] = float(minimum_commission)
    if regime_by_date is not None:
        request["regime_by_date"] = regime_by_date
    request.update(engine_kwargs)

    result = backtest.backtest_portfolio(**request)
    if isinstance(result, dict):
        result["capital_scenario_name"] = capital
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


def _cli(argv) -> int:
    capital = "research"
    if "--capital" in argv:
        capital = argv[argv.index("--capital") + 1]
    print(f"[policy_research_run] 資金情境 = {capital} "
          f"({CAPITAL_SCENARIOS.get(capital)} TWD)、"
          f"order_size_mode = {SCENARIO_ORDER_SIZE_MODE.get(capital)}")
    print(f"[policy_research_run] 資料快照 = "
          f"{getattr(config, 'SNAPSHOT_END_DATE', '') or 'live'}")
    print("[policy_research_run] 這支 CLI 只印組態;要跑 reference run 請由呼叫端"
          "提供 signal_frame(見模組 docstring),避免這裡偷偷內建一組策略參數。")
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys
    raise SystemExit(_cli(sys.argv[1:]))
