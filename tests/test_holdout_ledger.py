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

⚠ 這裡的假 summary / 合成 panel 只驗證揭露紀錄與旗標的行為,不代表任何策略績效;
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
    """每個測試都用自己的 outputs/,絕不碰真實揭露紀錄。"""

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
        """完全沒重疊的新窗仍是 fresh —— 揭露紀錄不可保守到讓 forward 永遠無法累積。"""
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

    def test_changing_an_unrelated_config_param_does_not_refresh_the_holdout(self):
        """**這次要修的核心 bug**:換一個規則 hash 不會讓看過的資料變回沒看過。

        `previously_seen` 綁在涵蓋 79 個 config 參數的 rules hash 上,而參數研究
        迴圈正是消耗 holdout 的主要途徑。實測:同一段 OS 用 hash H1 揭露兩次會
        正確標成 consumed;但只要把 `config.BBANDS_K` 從 2.0 改成 2.5(S19 的訊號
        與 FACTOR_WEIGHTS 都不讀這個參數)重算 hash,同一段 OS 就回報
        `holdout_status='fresh'`、`fresh_oos_claim_allowed=True`。

        修法不是把不同規則也算成 `holdout_previously_seen`(那是另一套規則的樣本
        外,語意不同),而是另外報一個**不分規則的窗口口徑**,並讓
        `fresh_oos_claim_allowed` 同時要求兩者都沒看過。
        """
        h1 = freeze_manifest.rules_hash(freeze_manifest.rules_payload(s19.SPEC))
        with mock.patch.object(config, "BBANDS_K", config.BBANDS_K + 0.5):
            h2 = freeze_manifest.rules_hash(
                freeze_manifest.rules_payload(s19.SPEC))
        self.assertNotEqual(h1, h2, "改 config 參數本來就該換 hash")

        self.reveal(os_start="2026-09-01", os_end="2026-10-31",
                    strategy_hash=h1, strategy_name="s19_chip_momentum")
        again = self.reveal(os_start="2026-09-01", os_end="2026-10-31",
                            strategy_hash=h2, strategy_name="s19_chip_momentum",
                            now=datetime(2026, 8, 16, 9, 0, 0))
        # 同規則口徑仍然誠實:這套 hash 確實是第一次看
        self.assertFalse(again["holdout_previously_seen"])
        self.assertEqual(again["holdout_status"], "fresh")
        # 但不分規則的口徑看得見,而且不得再宣稱 fresh OOS
        self.assertTrue(again["window_previously_revealed_any_rules"])
        self.assertEqual(again["window_reveal_count_any_rules"], 1)
        self.assertEqual(again["window_distinct_rules_any"], 1)
        self.assertFalse(again["fresh_oos_claim_allowed"])
        self.assertIn("多重檢定", again["fresh_oos_blocked_reason"])

    def test_untouched_window_is_still_claimable_as_fresh(self):
        """反向:沒有任何規則看過的窗仍然可以宣稱 fresh —— 否則 forward 永遠
        無法累積,新的多重檢定口徑就變成一個把系統鎖死的閘門。"""
        self.reveal(os_start="2026-09-01", os_end="2026-10-31",
                    strategy_hash="hash_other")
        nxt = self.reveal(os_start="2026-11-01", os_end="2026-12-31",
                          strategy_hash="hash_a",
                          now=datetime(2026, 8, 16, 9, 0, 0))
        self.assertTrue(nxt["fresh_oos_claim_allowed"])
        self.assertFalse(nxt["window_previously_revealed_any_rules"])
        self.assertIsNone(nxt["fresh_oos_blocked_reason"])

    def test_same_rules_reveal_reports_the_same_rules_reason(self):
        """兩個口徑的理由不可互相冒充:同規則重跑要說「同一套規則」。"""
        self.reveal(os_start="2026-09-01", os_end="2026-10-31")
        again = self.reveal(os_start="2026-09-01", os_end="2026-10-31",
                            now=datetime(2026, 8, 16, 9, 0, 0))
        self.assertIn("同一套規則", again["fresh_oos_blocked_reason"])
        self.assertNotIn("多重檢定", again["fresh_oos_blocked_reason"])

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
        """靜默重寫是這份揭露紀錄唯一的致命傷 —— 改過必須讀不出來,而不是照常回傳。"""
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
        """揭露紀錄壞掉時不可以「當成空的」繼續寫 —— 那等於用一次寫入洗掉歷史。"""
        self.reveal(os_start="2025-11-19", os_end="2026-06-18")
        self.ledger.write_text("{ not json\n", encoding="utf-8")
        with self.assertRaises(holdout.HoldoutLedgerError):
            self.reveal(os_start="2026-06-19", os_end="2026-08-04")

    def test_deleting_the_whole_ledger_is_not_a_clean_slate(self):
        """**整檔刪除**必須看得見 —— 這是雜湊鏈唯一擋不到的形狀。

        原 bug(2026-08-15 審查實測):鏈只擋改列/刪列/插列,`read_ledger` 對不
        存在的檔直接 `return []`,所以 `os.remove(outputs/holdout_ledger.jsonl)`
        之後,同 hash 同窗立刻回報 `fresh`、零警告 —— append-only 的紀錄被一個
        `rm` 洗掉。而這兩份 ledger 又被 `.gitignore` 的 `outputs/*` 排除,連
        「檔案不見了」都不會出現在 git status。
        """
        first = self.reveal(os_start="2026-09-01", os_end="2026-10-31")
        self.assertTrue(holdout.checkpoint_path().exists())
        self.ledger.unlink()

        with self.assertRaisesRegex(holdout.HoldoutLedgerError, "刪除或截斷"):
            holdout.read_ledger()
        # 也不得靠 reveal_status「查一下」就繞過(那正是回報 fresh 的入口)
        with self.assertRaises(holdout.HoldoutLedgerError):
            holdout.reveal_status(strategy_hash=first["strategy_hash"],
                                  strategy_name=None,
                                  os_start="2026-09-01", os_end="2026-10-31")
        # 更不得靠「再寫一列」重新開始(那會把揭露紀錄洗成 seq=1 的新鏈)
        with self.assertRaises(holdout.HoldoutLedgerError):
            self.reveal(os_start="2026-09-01", os_end="2026-10-31",
                        now=datetime(2026, 8, 16, 9, 0, 0))

    def test_truncating_the_ledger_is_detected(self):
        """留著檔案但砍掉尾巴 = 鏈仍然自洽,只有列數指紋看得出來。"""
        self.reveal(os_start="2026-09-01", os_end="2026-10-31")
        self.reveal(os_start="2026-11-01", os_end="2026-12-31",
                    now=datetime(2026, 8, 16, 9, 0, 0))
        rows = self.lines()
        self.ledger.write_text(rows[0] + "\n", encoding="utf-8")
        with self.assertRaisesRegex(holdout.HoldoutLedgerError, "刪除或截斷"):
            holdout.read_ledger()

    def test_checkpoint_tracks_row_count_and_last_hash(self):
        """指紋內容就是「幾列 + 末列 record_sha256」,每次 append 同步更新。"""
        self.assertIsNone(holdout.read_checkpoint())
        for i in range(3):
            rec = self.reveal(os_start="2026-09-01", os_end="2026-10-31",
                              strategy_hash=f"hash_{i}",
                              now=datetime(2026, 8, 15, 9, i, 0))
            cp = holdout.read_checkpoint()
            self.assertEqual(cp["rows"], i + 1)
            self.assertEqual(cp["last_record_sha256"], rec["record_sha256"])
        self.assertEqual(len(self.lines()), 3)

    def test_corrupt_checkpoint_fails_closed(self):
        """指紋壞掉不得當成「沒有指紋」放行 —— 否則刪揭露紀錄只要順手弄壞指紋。"""
        self.reveal(os_start="2026-09-01", os_end="2026-10-31")
        holdout.checkpoint_path().write_text("{ not json", encoding="utf-8")
        with self.assertRaises(holdout.HoldoutLedgerError):
            holdout.read_ledger()

    def test_fresh_clone_without_ledger_or_checkpoint_starts_empty(self):
        """兩份都不存在 = 從來沒揭露過(乾淨 clone),照常回空 list。"""
        self.assertEqual(holdout.read_ledger(), [])
        self.assertEqual(holdout.verify_ledger(), 0)

    def test_concurrent_reveals_do_not_overwrite_each_other(self):
        """併發揭露:每一次都要留下自己的一列,而且整條鏈仍然接得起來。

        沒有檔案鎖時,兩個 process 會同時讀到「揭露紀錄是空的」→ 兩列都寫
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

        揭露紀錄是空的(乾淨 clone、或 outputs/ 被清掉)時尤其重要:consumed 狀態
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
        # 揭露紀錄的 strategy_hash 與 manifest 的 rules_sha256_16 必須是同一個
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

    def test_unresolved_boundaries_make_the_manifest_unreliable(self):
        """邊界沒解析成日期 = 沒釘住 OS → 不是可靠的凍結版本(ok=False)。

        原本這裡只斷言「有 warning、但 ok=True」。實測那條 warning 沒有任何
        擋阻力:`freeze_manifest` 的 CLI 根本沒有傳日曆的選項,所以走正式路徑
        產出的 manifest **一律** `resolved=False`(`is_window`/`os_window` 全是
        null),而 `forward_test.run` 印一行警告就照跑 —— P1-3 的「manifest 固定
        記錄 IS／embargo／OS 邊界」在唯一的正式路徑上等於沒做。
        """
        m = freeze_manifest.build_manifest("x", freeze_date="2026-08-15")
        self.assertFalse(m["holdout"]["resolved"])
        st = freeze_manifest.validate_manifest(m)
        self.assertFalse(st.ok, st.describe())
        self.assertEqual(st.reliability, "incomplete_or_legacy")
        self.assertTrue(any("resolved=False" in p for p in st.problems),
                        st.problems)

    def test_cli_default_path_produces_resolved_boundaries(self):
        """**CLI 的預設路徑**必須產出解得出日期的 holdout 邊界。

        原 bug(2026-08-15 審查實測):`holdout_boundaries(calendar=...)` 是選用
        關鍵字,而 `freeze_manifest.__main__` 沒有任何對應選項 —— 走 CLI **不可能**
        解析出日期。`run("cli_default")` 產出 `holdout.resolved=false`、
        `is_window=null`、`os_window=null`,`validate_manifest` 卻回 ok=True
        (reliability='reliable_with_warnings'),`forward_test.run` 印一行警告
        照跑。閘門又變回「呼叫端要記得傳的關鍵字參數」。

        現在 `run()` 自己去解交易日曆(離線,只讀 TAIEX 一條序列的快取)。
        """
        import data

        cal = pd.bdate_range("2023-01-01", "2026-12-31")
        fake_taiex = pd.DataFrame({"date": cal, "close": 1.0})
        with mock.patch.object(data, "fetch_market_index",
                               return_value=fake_taiex) as fetch:
            path = freeze_manifest.main(["--label", "cli_default"])
        self.assertTrue(fetch.called, "CLI 沒有去解交易日曆")
        self.assertIsNotNone(path)
        m = json.loads(path.read_text(encoding="utf-8"))
        h = m["holdout"]
        self.assertTrue(h["resolved"], "CLI 預設路徑仍然產不出釘住日期的邊界")
        self.assertIsNotNone(h["is_window"])
        self.assertIsNotNone(h["os_window"])
        self.assertEqual(len(h["os_window"]), 2)
        self.assertLess(h["is_window"][1], h["os_window"][0])
        st = freeze_manifest.validate_manifest(m)
        self.assertTrue(st.ok, st.describe())
        # 不斷言 reliability 字串:工作樹 dirty 時本來就會多一條 git 警告
        # (那是環境狀態,不是這條測試要釘的東西)。要釘的是「沒有 holdout 警告」。
        self.assertEqual([w for w in st.warnings if "holdout" in w], [])
        self.assertEqual([p for p in st.problems if "holdout" in p], [])

    def test_calendar_is_clipped_to_the_price_data_window(self):
        """TAIEX 抓的歷史比個股長,不裁的話 IS 起點會落在回測看不到的日期。"""
        import data

        cal = pd.bdate_range("2015-01-01", "2026-12-31")
        with mock.patch.object(data, "fetch_market_index",
                               return_value=pd.DataFrame({"date": cal})):
            days = freeze_manifest.trading_calendar()
        scope = data.cache_scope("price", "CALENDAR")
        self.assertGreaterEqual(str(pd.Timestamp(days[0]).date()), scope.start)
        self.assertLessEqual(str(pd.Timestamp(days[-1]).date()), scope.end)

    def test_calendar_fails_closed_when_market_series_is_empty(self):
        """解不出日曆時 raise,而不是產出一份沒有邊界的 manifest。"""
        import data

        with mock.patch.object(data, "fetch_market_index",
                               return_value=pd.DataFrame()):
            with self.assertRaisesRegex(RuntimeError, "解不出交易日曆"):
                freeze_manifest.trading_calendar()

    def test_previous_schema_is_now_legacy(self):
        """schema 2 沒有 holdout 邊界 → 不得冒充可靠凍結版本。"""
        m = freeze_manifest.build_manifest("x", freeze_date="2026-08-15",
                                           calendar=self.CAL)
        m["manifest_schema"] = 2
        del m["holdout"]
        self.assertFalse(freeze_manifest.validate_manifest(m).ok)

    def test_rules_hash_has_a_single_implementation(self):
        """manifest 的 `rules_sha256_16` 與揭露紀錄的 `strategy_hash` 必須是同一個
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
                 now: datetime, spec=None, label: str = "fwd",
                 resolve_boundaries: bool = True):
        from backtest import event_backtest
        import forward_test

        panel = _panel(panel_start)
        panel.attrs["universe_provider"] = _FakeProvider()
        calls = []

        def fake_bt(**kwargs):
            calls.append(kwargs)
            return {"summary": _fake_summary(len(calls)),
                    "trades": pd.DataFrame(), "equity_curve": pd.DataFrame()}

        with mock.patch.object(config, "SNAPSHOT_END_DATE", snapshot):
            m = freeze_manifest.build_manifest(
                label, spec, freeze_date=freeze_date,
                # holdout 邊界是 manifest 的必要內容(見 P1-3):未解析的 manifest
                # 不是可靠的凍結版本,forward 會拒用(resolve_boundaries=False
                # 就是拿來釘那條拒用的)。
                calendar=(pd.bdate_range("2024-06-24", periods=500)
                          if resolve_boundaries else None))
            path = freeze_manifest.manifest_path(m)
            if not path.exists():
                path.write_text(json.dumps(m, ensure_ascii=False, default=str),
                                encoding="utf-8")
            with (
                mock.patch.object(s19, "build_panel",
                                  return_value=(panel, ["A", "B", "C", "D"])),
                mock.patch.object(event_backtest, "backtest_portfolio",
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

    def test_forward_with_another_rules_hash_over_the_same_window_is_not_fresh(self):
        """同一段 forward 窗被**另一套規則**看過 → 不得再宣稱 fresh OOS。

        這是 A3 的端到端版本:參數研究迴圈每改一個參數就換一個 hash,
        `holdout_previously_seen` 綁在 hash 上,所以同一段未來資料可以被無限次
        報成 fresh。現在 `fresh_oos` 另外要求「任何規則都沒看過」。
        """
        first = self._forward(freeze_date="2026-08-25", panel_start="2026-09-01",
                              snapshot="2026-11-30", now=T0)
        other_spec = s19.SPEC.replace(portfolio={"stop_loss": 0.12})
        second = self._forward(freeze_date="2026-08-25", panel_start="2026-09-01",
                               snapshot="2026-11-30", label="fwd2",
                               spec=other_spec,
                               now=datetime(2026, 8, 15, 11, 0, 0))
        self.assertNotEqual(first["rules_sha256_16"], second["rules_sha256_16"])
        rec = second["holdout"]
        self.assertFalse(rec["holdout_previously_seen"], "這套 hash 確實第一次看")
        self.assertTrue(rec["window_previously_revealed_any_rules"])
        self.assertFalse(second["fresh_oos"])
        self.assertIn("多重檢定", second["evidence_note"])

    def test_forward_refuses_a_manifest_without_resolved_boundaries(self):
        """holdout 邊界沒解析成日期的 manifest 不得被 forward 拿去宣稱 OOS。

        原本只是一行警告:`forward_test.run` 照跑並寫出 payload,而 manifest 的
        `holdout.is_window` / `os_window` 都是 null。
        """
        import forward_test  # noqa: F401  (確保 run 的 import 路徑一致)

        with self.assertRaisesRegex(ValueError, "不是可靠的凍結版本"):
            self._forward(freeze_date="2026-08-25", panel_start="2026-09-01",
                          snapshot="2026-11-30", now=T0, label="unresolved",
                          resolve_boundaries=False)
        self.assertFalse(self.ledger.exists(), "被拒的 forward 不得留下揭露紀錄")

    def test_refused_duplicate_run_records_no_reveal(self):
        """被同名輸出擋下來的重跑什麼都沒揭露,不該在揭露紀錄留下一筆「看過」。"""
        self._forward(freeze_date="2026-08-25", panel_start="2026-09-01",
                      snapshot="2026-11-30", now=T0)
        with self.assertRaises(FileExistsError):
            self._forward(freeze_date="2026-08-25", panel_start="2026-09-01",
                          snapshot="2026-11-30", now=T0)
        self.assertEqual(len(self.lines()), 1)


# ── 6. 正式 IS/OS(run_full)揭露 OS 也要入帳 ─────────────────────────────
class RunFullHoldoutTest(_LedgerCase):
    def _run_full(self):
        from backtest import event_backtest
        import evaluation.splits as evaluation_split

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
            mock.patch.object(event_backtest.uni, "get_universe", return_value=["A"]),
            mock.patch.object(event_backtest, "backtest_portfolio",
                              side_effect=fake_portfolio),
            mock.patch.object(event_backtest, "factor_ic", return_value=pd.DataFrame()),
        ):
            result, _ = event_backtest.run_full(sample=True, top_n=1, rebalance_every=3,
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
        self.assertEqual(rec["source"], "event_backtest.run_full")
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
