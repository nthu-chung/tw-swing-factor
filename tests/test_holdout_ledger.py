# -*- coding: utf-8 -*-
"""holdout 使用紀錄的離線回歸測試(P1-3)。

原本的 bug(這支測試逐條釘住)
------------------------------
系統**完全沒有**「這段 OS 已經被看過」的紀錄,而 IS/OS 切點是由凍結資料自身的
首尾日決定的(`evaluation/splits.py` 錨在 `dts[-1]`),資料視窗兩端又隨
`SNAPSHOT_END_DATE` 滑動(`data.py` 的 `start = end - HISTORY_DAYS`)。實測三個
真實快照:

    快照 2026-06-22 → OS = 2025-11-19 ~ 2026-06-18
    快照 2026-08-06 → OS 起點變成 2026-01-05

亦即 2025-11-19 ~ 2026-01-04 這段**從 OS 變成 IS**。推進快照後重跑,同一段資料
會被第二次當成 holdout 報成 fresh OOS,而系統沒有任何欄位擋得住 —— forward-only
又已經是唯一剩下的證據升級路徑。

另外兩個原本不成立的性質:
  - manifest 只凍切割**參數**,沒有釘住解出來的 IS/embargo/OS **日期**。
  - append-only 只靠「用 'a' 模式開檔」,既有列被事後改寫沒有任何人看得出來。

⚠ 這裡的假 summary / 合成 panel 只驗證台帳與旗標的行為,不代表任何策略績效;
S19 的證據等級仍是 blocked,其既有 OS 仍是 consumed / pseudo-OOS。
"""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

import config
import freeze_manifest
from evaluation import holdout
from strategies import s19_chip_momentum as s19

T0 = datetime(2026, 8, 15, 9, 0, 0)


class _LedgerCase(unittest.TestCase):
    """每個測試都用自己的 outputs/,絕不碰真實台帳。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.ledger = self.out / holdout.LEDGER_NAME
        p = mock.patch.object(config, "OUTPUT_DIR", self.out)
        p.start()
        self.addCleanup(p.stop)

    def lines(self):
        return [ln for ln in self.ledger.read_text(encoding="utf-8").splitlines()
                if ln.strip()]

    def reveal(self, *, os_start, os_end, strategy_hash="hash_a",
               strategy_name=None, now=T0, **kw):
        return holdout.record_reveal(
            strategy_hash=strategy_hash, strategy_name=strategy_name,
            os_start=os_start, os_end=os_end,
            source="tests", now=now, **kw)


# ── 1. 第一次揭露入帳 / 第二次標 previously_seen ──────────────────────────
class RevealLedgerTest(_LedgerCase):
    def test_first_reveal_records_hash_dates_time_and_commit(self):
        """第一次正式揭露 OS → append 一列:strategy hash、OS 日期、reveal time、
        Git commit。原本這四件事一件都沒被記下來。"""
        rec = self.reveal(os_start="2025-11-19", os_end="2026-06-18",
                          is_window=["2024-06-23", "2025-10-20"],
                          embargo_trading_days=10, split_mode="ratio")
        self.assertEqual(len(self.lines()), 1)
        self.assertEqual(rec["strategy_hash"], "hash_a")
        self.assertEqual((rec["os_start"], rec["os_end"]),
                         ("2025-11-19", "2026-06-18"))
        self.assertEqual(rec["reveal_at"], "2026-08-15T09:00:00")
        self.assertIn("git_commit", rec)
        self.assertIn("git_dirty", rec)
        self.assertEqual(rec["is_window"], ["2024-06-23", "2025-10-20"])
        self.assertEqual(rec["embargo_trading_days"], 10)
        self.assertFalse(rec["holdout_previously_seen"])
        self.assertEqual(rec["holdout_status"], "fresh")
        self.assertTrue(rec["fresh_oos_claim_allowed"])
        self.assertEqual(rec["prev_sha256"], holdout.GENESIS)

    def test_second_run_over_same_os_is_flagged_previously_seen(self):
        """相同 OS 可以為重現目的再跑,但必須標 `holdout_previously_seen=True`,
        不可再稱 fresh OOS。"""
        self.reveal(os_start="2025-11-19", os_end="2026-06-18")
        again = self.reveal(os_start="2025-11-19", os_end="2026-06-18",
                            now=datetime(2026, 8, 16, 9, 0, 0))
        self.assertEqual(len(self.lines()), 2, "重跑要留下自己的一列,不是覆寫")
        self.assertTrue(again["holdout_previously_seen"])
        self.assertEqual(again["holdout_status"], "consumed")
        self.assertFalse(again["fresh_oos_claim_allowed"])
        self.assertIsNone(again["fresh_os_start"])
        self.assertEqual(again["prior_reveals_same_rules"], [1])

    def test_slid_window_overlap_counts_as_previously_seen(self):
        """**這次要修的核心 bug**:推進快照後 OS 起點右移,新舊窗重疊但不相等。

        用日期字串等值比對的話,快照 2026-06-22 的 OS(2025-11-19~2026-06-18)
        與快照 2026-08-06 的 OS(2026-01-05~2026-08-04)會被判成兩段互不相干的
        holdout,而 2026-01-05~2026-06-18 這段早就看過了。
        """
        self.reveal(os_start="2025-11-19", os_end="2026-06-18")
        slid = self.reveal(os_start="2026-01-05", os_end="2026-08-04",
                           now=datetime(2026, 8, 16, 9, 0, 0))
        self.assertTrue(slid["holdout_previously_seen"])
        self.assertEqual(slid["holdout_status"], "partially_consumed")
        # 真正沒被看過的只有 2026-06-19 之後那一段
        self.assertEqual(slid["fresh_os_start"], "2026-06-19")
        self.assertEqual(slid["previously_seen_days"],
                         (pd.Timestamp("2026-06-18")
                          - pd.Timestamp("2026-01-05")).days + 1)

    def test_disjoint_window_is_fresh(self):
        """完全沒重疊的新窗仍是 fresh —— 台帳不可保守到讓 forward 永遠無法累積。"""
        self.reveal(os_start="2025-11-19", os_end="2026-06-18")
        nxt = self.reveal(os_start="2026-06-19", os_end="2026-08-04",
                          now=datetime(2026, 8, 16, 9, 0, 0))
        self.assertFalse(nxt["holdout_previously_seen"])
        self.assertEqual(nxt["holdout_status"], "fresh")

    def test_other_rules_do_not_consume_this_rules_holdout(self):
        """另一套規則(不同 hash)看過同一段,不會讓這套規則變成 previously_seen,
        但重疊次數要記下來(同一段 holdout 被 N 套規則輪流看是多重檢定問題)。"""
        self.reveal(os_start="2025-11-19", os_end="2026-06-18",
                    strategy_hash="hash_other")
        mine = self.reveal(os_start="2025-11-19", os_end="2026-06-18",
                           strategy_hash="hash_a",
                           now=datetime(2026, 8, 16, 9, 0, 0))
        self.assertFalse(mine["holdout_previously_seen"])
        self.assertEqual(mine["prior_reveals_other_rules"], 1)

    def test_reveal_without_strategy_hash_fails_closed(self):
        with self.assertRaisesRegex(holdout.HoldoutLedgerError, "strategy_hash"):
            self.reveal(os_start="2026-01-01", os_end="2026-02-01",
                        strategy_hash="")

    def test_reversed_window_fails_closed(self):
        with self.assertRaisesRegex(holdout.HoldoutLedgerError, "顛倒"):
            self.reveal(os_start="2026-06-18", os_end="2025-11-19")


# ── 2. append-only:既有列不可被覆寫,改過要看得見 ────────────────────────
class LedgerImmutabilityTest(_LedgerCase):
    def test_existing_rows_are_never_rewritten(self):
        first = self.reveal(os_start="2025-11-19", os_end="2026-06-18")
        body_after_first = self.lines()[0]
        for i in range(3):
            self.reveal(os_start="2026-06-19", os_end="2026-08-04",
                        now=datetime(2026, 8, 16 + i, 9, 0, 0))
        rows = self.lines()
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0], body_after_first, "既有列不可被改寫")
        self.assertEqual([json.loads(r)["seq"] for r in rows], [1, 2, 3, 4])
        self.assertEqual(json.loads(rows[0])["reveal_at"], first["reveal_at"])

    def test_tampered_row_is_detected(self):
        """靜默重寫是這份台帳唯一的致命傷 —— 改過必須讀不出來,而不是照常回傳。"""
        self.reveal(os_start="2025-11-19", os_end="2026-06-18")
        self.reveal(os_start="2026-06-19", os_end="2026-08-04",
                    now=datetime(2026, 8, 16, 9, 0, 0))
        rows = self.lines()
        doctored = json.loads(rows[0])
        doctored["os_start"] = "2026-05-01"       # 把「看過的範圍」改小
        self.ledger.write_text(
            json.dumps(doctored, ensure_ascii=False) + "\n" + rows[1] + "\n",
            encoding="utf-8")
        with self.assertRaisesRegex(holdout.HoldoutLedgerError, "被事後改過"):
            holdout.read_ledger()

    def test_deleted_row_breaks_the_chain(self):
        self.reveal(os_start="2025-11-19", os_end="2026-06-18")
        self.reveal(os_start="2026-06-19", os_end="2026-08-04",
                    now=datetime(2026, 8, 16, 9, 0, 0))
        rows = self.lines()
        self.ledger.write_text(rows[1] + "\n", encoding="utf-8")   # 抽掉第一列
        with self.assertRaisesRegex(holdout.HoldoutLedgerError, "prev_sha256"):
            holdout.read_ledger()

    def test_broken_ledger_blocks_new_reveals(self):
        """台帳壞掉時不可以「當成空的」繼續寫 —— 那等於用一次寫入洗掉歷史。"""
        self.reveal(os_start="2025-11-19", os_end="2026-06-18")
        self.ledger.write_text("{ not json\n", encoding="utf-8")
        with self.assertRaises(holdout.HoldoutLedgerError):
            self.reveal(os_start="2026-06-19", os_end="2026-08-04")

    def test_concurrent_reveals_do_not_overwrite_each_other(self):
        """併發揭露:每一次都要留下自己的一列,而且整條鏈仍然接得起來。

        沒有檔案鎖時,兩個 process 會同時讀到「台帳是空的」→ 兩列都寫
        `prev_sha256=genesis`、`seq=1`,鏈斷掉、而且兩邊都宣稱 fresh。
        """
        errors: list = []

        def worker(i: int):
            try:
                self.reveal(os_start="2025-11-19", os_end="2026-06-18",
                            strategy_hash=f"hash_{i}",
                            now=datetime(2026, 8, 15, 9, i, 0))
            except Exception as exc:            # pragma: no cover - 失敗才會進來
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(errors, [])
        rows = holdout.read_ledger()             # 會順便驗鏈
        self.assertEqual(len(rows), 6)
        self.assertEqual(sorted(r["seq"] for r in rows), list(range(1, 7)))
        self.assertEqual(len({r["strategy_hash"] for r in rows}), 6)


# ── 3. S19 既有 OS 維持 consumed / pseudo-OOS ─────────────────────────────
class S19ConsumedHoldoutTest(_LedgerCase):
    OS = ("2025-11-19", "2026-06-18")

    def test_s19_existing_os_stays_consumed_even_with_empty_ledger(self):
        """S19 現有 OS 明確維持 consumed / pseudo-OOS,不得重設為 clean。

        台帳是空的(乾淨 clone、或 outputs/ 被清掉)時尤其重要:consumed 狀態
        寫在程式碼的 `KNOWN_CONSUMED_HOLDOUTS`,不是某台機器上的 jsonl。
        """
        self.assertEqual(holdout.read_ledger(), [])
        st = holdout.reveal_status(strategy_hash="whatever",
                                   strategy_name="s19_chip_momentum",
                                   os_start=self.OS[0], os_end=self.OS[1])
        self.assertTrue(st["holdout_previously_seen"])
        self.assertEqual(st["holdout_status"], "consumed")
        self.assertFalse(st["fresh_oos_claim_allowed"])
        self.assertTrue(st["declared_consumed"])
        self.assertEqual(st["declared_consumed"][0]["status"],
                         "consumed_pseudo_oos")

    def test_s19_declaration_matches_registry_os_window(self):
        """宣告的 OS 窗要對得上 STRATEGY_REGISTRY / 報告裡那一段,否則這份宣告
        擋不到真正被消耗的區間。"""
        declared = [c for c in holdout.KNOWN_CONSUMED_HOLDOUTS
                    if c.strategy == "s19_chip_momentum"]
        self.assertEqual(len(declared), 1)
        self.assertEqual(tuple(declared[0].os_window), self.OS)
        self.assertIn("洩漏", declared[0].reason)

    def test_s19_reveal_is_recorded_as_consumed(self):
        rec = self.reveal(os_start=self.OS[0], os_end=self.OS[1],
                          strategy_hash="s19hash",
                          strategy_name="s19_chip_momentum")
        self.assertTrue(rec["holdout_previously_seen"])
        self.assertEqual(rec["holdout_status"], "consumed")

    def test_s19_report_entry_records_a_consumed_reveal(self):
        """S19 報告入口每跑一次就入帳一列,而且必然是 consumed。

        `main()` 需要真實資料所以不在測試裡跑,但揭露這一步要有覆蓋 ——
        否則這段程式只有在真的跑報告時才會第一次被執行。
        """
        rec = s19._record_os_reveal(
            s19.SPEC, is_window=["2024-06-23", "2025-10-20"],
            os_start=self.OS[0], os_end=self.OS[1], n_phases=20)
        self.assertEqual(rec["strategy_name"], "s19_chip_momentum")
        self.assertEqual(rec["source"], "strategies.s19_chip_momentum.main")
        self.assertTrue(rec["holdout_previously_seen"])
        self.assertEqual(rec["holdout_status"], "consumed")
        # 台帳的 strategy_hash 與 manifest 的 rules_sha256_16 必須是同一個
        m = freeze_manifest.build_manifest("s19", freeze_date="2026-08-15")
        self.assertEqual(rec["strategy_hash"], m["rules_sha256_16"])

    def test_other_strategies_are_not_affected_by_s19_declaration(self):
        st = holdout.reveal_status(strategy_hash="h", strategy_name="s99_other",
                                   os_start=self.OS[0], os_end=self.OS[1])
        self.assertFalse(st["holdout_previously_seen"])

    def test_forward_window_after_declaration_is_still_fresh(self):
        """既成宣告只覆蓋已看過的資料窗,不可把未來的 forward 窗也一起吃掉 ——
        否則 S19 就再也沒有任何能升級證據等級的路。"""
        st = holdout.reveal_status(strategy_hash="h",
                                   strategy_name="s19_chip_momentum",
                                   os_start="2026-08-07", os_end="2026-09-30")
        self.assertFalse(st["holdout_previously_seen"])
        self.assertEqual(st["holdout_status"], "fresh")


# ── 4. manifest 固定記錄 IS / embargo / OS 邊界 ───────────────────────────
class ManifestHoldoutBoundaryTest(_LedgerCase):
    CAL = pd.bdate_range("2024-06-24", periods=500)

    def test_manifest_records_resolved_is_embargo_os_boundaries(self):
        m = freeze_manifest.build_manifest("x", freeze_date="2026-08-15",
                                           calendar=self.CAL)
        h = m["holdout"]
        self.assertTrue(h["resolved"])
        split = freeze_manifest.build_evaluation_split(
            self.CAL, minimum_embargo_days=config.BT_IC_HORIZON)
        self.assertEqual(h["is_window"], list(split.is_window))
        self.assertEqual(h["os_window"], list(split.os_window))
        self.assertEqual(h["embargo_trading_days"], split.n_embargo)
        self.assertEqual(h["split_mode"], config.EVAL_SPLIT_MODE)

    def test_boundaries_do_not_enter_the_rules_hash(self):
        """解出來的日期是**資料**的函數:進 hash 的話,同一套規則在不同快照下
        會變成兩套規則,forward 永遠對不上自己的凍結。"""
        with_cal = freeze_manifest.build_manifest("x", freeze_date="2026-08-15",
                                                  calendar=self.CAL)
        without = freeze_manifest.build_manifest("x", freeze_date="2026-08-15")
        self.assertEqual(with_cal["rules_sha256_16"], without["rules_sha256_16"])
        self.assertNotIn("holdout", with_cal["rules"])

    def test_manifest_without_holdout_section_is_refused(self):
        m = freeze_manifest.build_manifest("x", freeze_date="2026-08-15",
                                           calendar=self.CAL)
        del m["holdout"]
        st = freeze_manifest.validate_manifest(m)
        self.assertFalse(st.ok)
        self.assertTrue(any("holdout" in p for p in st.problems), st.problems)

    def test_unresolved_boundaries_are_warned_not_silently_accepted(self):
        m = freeze_manifest.build_manifest("x", freeze_date="2026-08-15")
        st = freeze_manifest.validate_manifest(m)
        self.assertTrue(st.ok, st.describe())
        self.assertTrue(any("未解析" in w for w in st.warnings), st.warnings)

    def test_previous_schema_is_now_legacy(self):
        """schema 2 沒有 holdout 邊界 → 不得冒充可靠凍結版本。"""
        m = freeze_manifest.build_manifest("x", freeze_date="2026-08-15",
                                           calendar=self.CAL)
        m["manifest_schema"] = 2
        del m["holdout"]
        self.assertFalse(freeze_manifest.validate_manifest(m).ok)

    def test_rules_hash_has_a_single_implementation(self):
        """manifest 的 `rules_sha256_16` 與台帳的 `strategy_hash` 必須是同一個
        東西,否則「這段 OS 是哪一套規則看的」對不起來。"""
        payload = {"config": {"A": 1}, "strategy": {"name": "x"}}
        self.assertEqual(freeze_manifest.rules_hash(payload),
                         holdout.rules_fingerprint(payload))


# ── 5. forward:每次揭露都入帳,重疊的窗不得再稱 fresh OOS ────────────────
def _panel(start: str, n_days: int = 120, sids=("A", "B", "C", "D")) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    rows = []
    for k, sid in enumerate(sids):
        px = 100.0
        for d in pd.bdate_range(start, periods=n_days):
            px *= 1.0 + rng.normal(0.001 * (k + 1), 0.02)
            rows.append({"date": d, "stock_id": sid, "name": f"N{sid}",
                         "close": px, "volume": 1e6 * (k + 1),
                         "foreign_net": 1e4, "trust_net": 5e3,
                         "in_dynamic_universe": True, "trend_ok": True})
    return pd.DataFrame(rows).sort_values(["date", "stock_id"]).reset_index(drop=True)


class _FakeProvider:
    all_symbols = ["A", "B", "C", "D"]

    def metadata(self):
        return {"candidate_rule": "month_M_uses_only_calendar_month_M_minus_1"}


def _fake_summary(n: int) -> dict:
    return {"sharpe": 0.5, "ann_ret": 0.1, "ann_vol": 0.2, "max_drawdown": -0.1,
            "n_trades": 40, "win_rate": 0.5, "payoff_ratio": 2.0,
            "eval_audit": {"days_beyond_last_pick": 0},
            "universe": {"candidate_pool_pit": True}}


class ForwardHoldoutTest(_LedgerCase):
    """forward 窗也是 holdout:第二次跑同一段只是重現,不是新的樣本外。"""

    def _forward(self, *, freeze_date: str, panel_start: str, snapshot: str,
                 now: datetime, spec=None):
        import backtest
        import forward_test

        panel = _panel(panel_start)
        panel.attrs["universe_provider"] = _FakeProvider()
        calls = []

        def fake_bt(**kwargs):
            calls.append(kwargs)
            return {"summary": _fake_summary(len(calls)),
                    "trades": pd.DataFrame(), "equity_curve": pd.DataFrame()}

        with mock.patch.object(config, "SNAPSHOT_END_DATE", snapshot):
            m = freeze_manifest.build_manifest("fwd", spec, freeze_date=freeze_date)
            path = freeze_manifest.manifest_path(m)
            if not path.exists():
                path.write_text(json.dumps(m, ensure_ascii=False, default=str),
                                encoding="utf-8")
            with (
                mock.patch.object(s19, "build_panel",
                                  return_value=(panel, ["A", "B", "C", "D"])),
                mock.patch.object(backtest, "backtest_portfolio",
                                  side_effect=lambda **kw: fake_bt(**kw)),
                mock.patch.object(s19, "equal_weight_baseline",
                                  return_value={"sharpe": 0.4, "ann_ret": 0.08,
                                                "ann_vol": 0.2, "n_days": 90}),
            ):
                return forward_test.run(str(path), now=now)

    def test_first_forward_over_untouched_window_is_fresh_and_recorded(self):
        payload = self._forward(freeze_date="2026-08-25", panel_start="2026-09-01",
                                snapshot="2026-11-30", now=T0)
        rec = payload["holdout"]
        self.assertEqual(len(self.lines()), 1)
        self.assertFalse(rec["holdout_previously_seen"])
        self.assertTrue(payload["fresh_oos"])
        self.assertEqual(rec["segment"], "forward")
        self.assertEqual(rec["strategy_hash"], payload["rules_sha256_16"])
        self.assertEqual(rec["strategy_name"], "s19_chip_momentum")
        self.assertEqual(rec["os_start"], "2026-08-26")
        # manifest 的 holdout 邊界跟著結果走
        self.assertIn("manifest_holdout_boundaries", payload)

    def test_second_forward_over_same_window_is_previously_seen(self):
        self._forward(freeze_date="2026-08-25", panel_start="2026-09-01",
                      snapshot="2026-11-30", now=T0)
        again = self._forward(freeze_date="2026-08-25", panel_start="2026-09-01",
                              snapshot="2026-11-30",
                              now=datetime(2026, 8, 15, 10, 0, 0))
        self.assertEqual(len(self.lines()), 2)
        self.assertTrue(again["holdout"]["holdout_previously_seen"])
        self.assertEqual(again["holdout"]["holdout_status"], "consumed")
        self.assertFalse(again["fresh_oos"])
        self.assertIn("不得宣稱 fresh OOS", again["evidence_note"])

    def test_forward_over_s19_consumed_window_is_not_fresh_oos(self):
        """凍結日落在 S19 已看過的資料窗內 → forward 期與 consumed holdout 重疊,
        不得標成 fresh OOS(S19 的既有 OS 維持 consumed)。"""
        payload = self._forward(freeze_date="2026-02-02", panel_start="2026-01-01",
                                snapshot="2026-06-22", now=T0)
        self.assertTrue(payload["holdout"]["holdout_previously_seen"])
        self.assertFalse(payload["fresh_oos"])
        self.assertTrue(payload["holdout"]["declared_consumed"])

    def test_refused_duplicate_run_records_no_reveal(self):
        """被同名輸出擋下來的重跑什麼都沒揭露,不該在台帳留下一筆「看過」。"""
        self._forward(freeze_date="2026-08-25", panel_start="2026-09-01",
                      snapshot="2026-11-30", now=T0)
        with self.assertRaises(FileExistsError):
            self._forward(freeze_date="2026-08-25", panel_start="2026-09-01",
                          snapshot="2026-11-30", now=T0)
        self.assertEqual(len(self.lines()), 1)


# ── 6. 正式 IS/OS(run_full)揭露 OS 也要入帳 ─────────────────────────────
class RunFullHoldoutTest(_LedgerCase):
    def _run_full(self):
        import backtest
        import evaluation_split

        dates = pd.bdate_range("2024-01-01", periods=120)

        def fake_portfolio(*_a, **kwargs):
            start = pd.Timestamp(kwargs.get("start_date") or dates[0])
            end = pd.Timestamp(kwargs.get("end_date") or dates[-1])
            eq = dates[(dates >= start) & (dates <= end)]
            return {
                "summary": {
                    "n_trades": 5, "ann_ret": 0.1, "sharpe": 1.0,
                    "max_drawdown": -0.1, "cum_ret": 0.1,
                    "data": {"integrity_bypassed": False},
                    "universe": {"survivorship_free": True},
                    "eval_audit": {"eval_window": [str(eq[0].date()),
                                                   str(eq[-1].date())]},
                },
                "equity_curve": pd.DataFrame({"date": eq, "equity": 1.0}),
                "trades": pd.DataFrame(),
            }

        with (
            mock.patch.object(backtest.uni, "get_universe", return_value=["A"]),
            mock.patch.object(backtest, "backtest_portfolio",
                              side_effect=fake_portfolio),
            mock.patch.object(backtest, "factor_ic", return_value=pd.DataFrame()),
        ):
            result, _ = backtest.run_full(sample=True, top_n=1, rebalance_every=3,
                                          dynamic_enabled=False)
        split = evaluation_split.build_evaluation_split(
            dates, minimum_embargo_days=config.BT_IC_HORIZON)
        return result, split

    def test_run_full_records_os_reveal_with_split_boundaries(self):
        result, split = self._run_full()
        rec = result["holdout"]
        self.assertIsNotNone(rec)
        self.assertEqual([rec["os_start"], rec["os_end"]], list(split.os_window))
        self.assertEqual(rec["is_window"], list(split.is_window))
        self.assertEqual(rec["embargo_trading_days"], split.n_embargo)
        self.assertEqual(rec["source"], "backtest.run_full")
        self.assertFalse(rec["holdout_previously_seen"])
        # smoke sample 仍然看過那段資料 → 照樣入帳,只是標成非正式證據
        self.assertFalse(rec["context"]["formal_evidence_eligible"])

    def test_rerunning_run_full_flags_previously_seen(self):
        self._run_full()
        result, _ = self._run_full()
        self.assertEqual(len(self.lines()), 2)
        self.assertTrue(result["holdout"]["holdout_previously_seen"])
        self.assertEqual(result["holdout"]["holdout_status"], "consumed")


if __name__ == "__main__":
    unittest.main()
