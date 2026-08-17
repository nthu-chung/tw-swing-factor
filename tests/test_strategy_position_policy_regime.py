# -*- coding: utf-8 -*-
"""外部 regime 的 PIT provenance 閘門(規格 §4.3)的離線回歸測試。

原本的 bug(2026-08-15 獨立審查實測)
--------------------------------------
規格 §4.3 寫死三件事:「policy 只接受**已帶 PIT provenance** 的 regime」、
「regime 必須有 hysteresis／來源時間戳」、「不得用今天資料回寫歷史 regime」。
但實作上傳進 `backtest_portfolio(regime_by_date=...)` 與 `policy.decide(regime=...)`
的都只是 `"risk_on"` / `"caution"` / `"risk_off"` 這種**裸字串**,而 summary 的
`regime_pit_provenance` 是 `bool(regime_by_date)` —— 「有傳東西」被當成
「有 PIT provenance」。

於是:拿今天的大盤走勢回頭標歷史每一天的 regime(risk_off 那幾週剛好避開崩盤),
回測照跑,`formal_evidence_eligible` 照樣是 True,summary 還會蓋一個
`regime_pit_provenance: True` 的章。整份結果自稱「regime 有 PIT 出處」,而輸入
裡根本沒有任何出處可查。

現在的行為:regime 沒有 provenance 物件(來源 / as-of 時間戳 / hysteresis 設定)
時只能標 `unverified`,並降級 `formal_evidence_eligible`。本次**不**實作 regime
的判定演算法(§8 明文那是另一份規格),只要求「沒有 provenance 就不能假裝有」。

⚠ 這裡的合成價格與假 regime 只驗證閘門行為,不代表任何策略績效。
"""
from __future__ import annotations

import unittest
from unittest import mock

import pandas as pd

from backtest import event_backtest
import config
import security_type
from _offline_registry import common_stock_registry, common_stocks
from strategies import s19_chip_momentum as _s19
from strategy_kit.position_policy import (
    RegimeProvenance,
    RegimeState,
    StrategyPositionPolicy,
    StrategyPositionPolicySpec,
    normalize_regime,
)
from universes import MonthlyPITUniverseProvider

_SPEC = _s19.SPEC
_SYMBOLS = ("1101", "1102")


def _provenance(as_of, source="tests.fake_regime_rule"):
    return RegimeProvenance(source=source, as_of=pd.Timestamp(as_of),
                            hysteresis="confirm_2_days")


def _one_slot_policy():
    return StrategyPositionPolicy(StrategyPositionPolicySpec(
        entry_rank=1, exit_rank=2, max_slots=1, slot_weight=1.0,
        single_name_cap=1.0, risk_on_slots=1, caution_slots=0,
        risk_off_slots=0))


# ── 1. RegimeProvenance / RegimeState 本身 ────────────────────────────────
class RegimeProvenanceObjectTest(unittest.TestCase):
    def test_bare_string_normalizes_to_unverified(self):
        state = normalize_regime("risk_on")
        self.assertEqual(state.label, "risk_on")
        self.assertFalse(state.verified)
        self.assertIsNone(state.provenance)

    def test_provenance_object_is_verified_and_serializable(self):
        state = RegimeState("caution", _provenance("2026-03-06"))
        self.assertTrue(state.verified)
        self.assertEqual(state.rules()["provenance"], {
            "source": "tests.fake_regime_rule",
            "as_of": "2026-03-06 00:00:00",
            "hysteresis": "confirm_2_days"})

    def test_blank_source_or_hysteresis_fails_closed(self):
        """空字串的來源 = 沒有來源,不得當成已驗證放行。"""
        for bad in ({"source": "  "}, {"hysteresis": ""}):
            with self.subTest(bad=bad):
                kwargs = {"source": "x", "as_of": pd.Timestamp("2026-03-06"),
                          "hysteresis": "y"}
                kwargs.update(bad)
                with self.assertRaises(ValueError):
                    RegimeProvenance(**kwargs)


# ── 2. policy.decide 層 ───────────────────────────────────────────────────
class PolicyDecideRegimeTest(unittest.TestCase):
    def _decide(self, regime, as_of="2026-03-09"):
        policy = StrategyPositionPolicy(StrategyPositionPolicySpec())
        return policy, policy.decide(
            as_of=pd.Timestamp(as_of),
            signals=pd.DataFrame([{"stock_id": "1101", "rank": 1,
                                   "raw_score": 1.0, "eligible": True,
                                   "snapshot_complete": True}]),
            holdings=pd.DataFrame(), equity=1_000_000.0,
            regime=regime, is_decision_day=True)

    def test_bare_string_regime_is_marked_unverified(self):
        policy, d = self._decide("risk_on")
        self.assertEqual(d.regime, "risk_on")
        self.assertFalse(d.regime_verified)
        self.assertIsNone(d.regime_provenance)
        self.assertEqual(policy.state()["n_unverified_regime_decisions"], 1)

    def test_regime_with_provenance_is_verified(self):
        policy, d = self._decide(RegimeState("risk_on", _provenance("2026-03-09")))
        self.assertTrue(d.regime_verified)
        self.assertEqual(d.regime_provenance["source"], "tests.fake_regime_rule")
        self.assertEqual(policy.state().get("n_unverified_regime_decisions", 0), 0)

    def test_provenance_dated_after_the_decision_day_fails_closed(self):
        """as-of 晚於決策日 = 用未來資料回寫歷史 regime(規格 §4.3)。"""
        with self.assertRaisesRegex(ValueError, "未來資料"):
            self._decide(RegimeState("risk_off", _provenance("2026-03-10")),
                         as_of="2026-03-09")

    def test_provenance_changes_the_decision_fingerprint(self):
        """同一個 label,有無出處是兩份不同可信度的決策 → 指紋不得相同。"""
        _, bare = self._decide("risk_on")
        _, verified = self._decide(
            RegimeState("risk_on", _provenance("2026-03-09")))
        self.assertNotEqual(bare.fingerprint, verified.fingerprint)


# ── 3. 引擎 / summary 層 ──────────────────────────────────────────────────
def _pit_history() -> pd.DataFrame:
    """兩檔都夠大 → 每個月的候選池都含兩檔(池本身不是這支測試的主題)。"""
    rows = []
    for d in pd.bdate_range("2026-01-01", "2026-03-31"):
        for sid in _SYMBOLS:
            rows.append({"date": d, "stock_id": sid, "turnover": 9e8})
    return pd.DataFrame(rows)


def _prices(dates) -> pd.DataFrame:
    return pd.DataFrame({
        "date": dates, "open": 100.0, "high": 101.0, "low": 99.0,
        "close": 100.0, "volume": 5_000_000.0})


class RegimeProvenanceInSummaryTest(unittest.TestCase):
    """policy 路徑:regime 沒有出處 → summary 標 unverified 且降級。

    基準線刻意用真正的 PIT provider + StrategySpec,讓
    `formal_evidence_eligible` 在沒有 regime 問題時是 **True** —— 否則「降級」
    這件事根本觀察不到(旗標本來就是 False)。
    """

    DATES = list(pd.bdate_range("2026-03-02", "2026-03-20"))

    def _run(self, regime_by_date):
        # 決策日 = 每週一(weekly 快照);rank 固定,不是這支測試的主題。
        snaps = [d for d in self.DATES if d.weekday() == 0]
        signals = pd.DataFrame([
            {"date": d, "stock_id": "1101", "rank": 1, "raw_score": 1.0,
             "eligible": True, "snapshot_complete": True}
            for d in snaps
        ])
        price = _prices(self.DATES)
        security_type.set_registry(common_stock_registry(*_SYMBOLS))
        with (
            common_stocks(*_SYMBOLS),
            mock.patch.object(event_backtest, "_assert_price_integrity",
                              lambda *a, **k: None),
            mock.patch.object(event_backtest, "_load_disposition_days",
                              lambda *a, **k: {}),
            mock.patch.object(event_backtest.data, "fetch_price",
                              side_effect=lambda *a, **k: price.copy()),
            mock.patch.object(config, "BT_MODEL_LIMIT_LOCK", True),
        ):
            return event_backtest.backtest_portfolio(
                symbols=list(_SYMBOLS), sample=False, dynamic_enabled=True,
                start_date=str(self.DATES[0])[:10],
                end_date=str(self.DATES[-1])[:10],
                signal_frame=signals,
                strategy_position_policy=_one_slot_policy(),
                regime_by_date=regime_by_date,
                universe_provider=MonthlyPITUniverseProvider.from_history(
                    _pit_history(), top_n=2, min_obs=5),
                strategy_spec=_SPEC,
            )

    def test_baseline_without_regime_overlay_stays_eligible(self):
        """完全不給 regime = 不做 regime overlay,沒用到外部資料 → 不降級。"""
        summary = self._run(None)["summary"]
        meta = summary["strategy_position_policy"]
        self.assertEqual(meta["regime_evidence"], "none_constant_risk_on")
        self.assertFalse(meta["regime_pit_provenance"])
        self.assertTrue(summary["universe"]["formal_evidence_eligible"])

    def test_regime_with_provenance_stays_eligible_and_records_the_source(self):
        regimes = {d: RegimeState("risk_on", _provenance(d)) for d in self.DATES}
        summary = self._run(regimes)["summary"]
        meta = summary["strategy_position_policy"]
        self.assertEqual(meta["regime_evidence"], "verified")
        self.assertTrue(meta["regime_pit_provenance"])
        self.assertEqual(meta["n_regime_days_unverified"], 0)
        self.assertEqual({p["source"] for p in meta["regime_provenance"]},
                         {"tests.fake_regime_rule"})
        self.assertTrue(summary["universe"]["formal_evidence_eligible"])

    def test_bare_string_regime_is_unverified_and_downgrades_the_evidence(self):
        summary = self._run({d: "risk_on" for d in self.DATES})["summary"]
        meta = summary["strategy_position_policy"]
        self.assertEqual(meta["regime_evidence"], "unverified")
        self.assertFalse(meta["regime_pit_provenance"])
        self.assertEqual(meta["n_regime_days_unverified"], len(self.DATES))
        self.assertIsNone(meta["regime_provenance"])
        universe = summary["universe"]
        self.assertFalse(universe["formal_evidence_eligible"])
        self.assertIn("provenance", universe["evidence_note"])

    def test_one_missing_provenance_day_is_enough_to_downgrade(self):
        """整段只有一天是裸字串也要降級:部分驗證不是驗證。"""
        regimes = {d: RegimeState("risk_on", _provenance(d)) for d in self.DATES}
        regimes[self.DATES[3]] = "risk_off"
        summary = self._run(regimes)["summary"]
        meta = summary["strategy_position_policy"]
        self.assertEqual(meta["regime_evidence"], "unverified")
        self.assertEqual(meta["n_regime_days_unverified"], 1)
        self.assertFalse(summary["universe"]["formal_evidence_eligible"])

    def test_decision_log_records_whether_the_regime_was_verified(self):
        """只記 label 的話,事後分不出「有依據的 risk_off」與「手打的 risk_off」。"""
        result = self._run({d: "risk_on" for d in self.DATES})
        log = pd.DataFrame(result["decision_log"])
        self.assertIn("regime_verified", log.columns)
        self.assertEqual(set(log["regime_verified"]), {False})


if __name__ == "__main__":
    unittest.main()
