# -*- coding: utf-8 -*-
"""run directory 與 atomic write 的責任測試。

三條刻意的限制,每一條都對應一個「事後查不出真相」的失敗:
  - run dir 可覆寫 → 被覆蓋掉的那一份可能已被引用,而覆蓋不留痕跡
  - 非 atomic write → 中斷時留下半份 JSON,下游讀成完整結果
  - 空表不寫檔 → 「檔案不存在」與「這次真的沒有交易」變成同一件事
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from research import artifacts


class RunDirectoryTest(unittest.TestCase):
    def test_existing_run_directory_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as td:
            artifacts.create_run_directory(td, "r1")
            with self.assertRaises(FileExistsError):
                artifacts.create_run_directory(td, "r1")

    def test_run_id_is_deterministic_for_the_same_inputs(self):
        a = artifacts.build_run_id(strategy_id="s", run_hash="h", stamp="t")
        b = artifacts.build_run_id(strategy_id="s", run_hash="h", stamp="t")
        self.assertEqual(a, b)

    def test_run_id_rejects_path_separators_instead_of_sanitising(self):
        """2026-08-16 收緊:由消毒改為拒絕。

        原本 `a/../b` 會被靜默改寫成 `a-..-b` 然後照樣產生 run 目錄。消毒的問題
        不是安全性 —— 是**呼叫端拿到的目錄名跟他要求的不同,而且沒人會發現**,
        run id 同時要進檔名與報告,名字被偷改掉就等於結果對不回請求。
        拒絕比消毒強:這些輸入現在連 run id 都產不出來。
        """
        for bad in ("a/../b", "../escape", "a\\b", "with/slash"):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    artifacts.build_run_id(strategy_id=bad, run_hash="h",
                                           stamp="t")
                with self.assertRaises(ValueError):
                    artifacts.build_run_id(strategy_id="s", run_hash="h",
                                           stamp=bad)

    def test_legitimate_ids_still_never_contain_path_separators(self):
        """原斷言的不變式仍然成立:合法輸入產出的 run id 不含路徑分隔。"""
        rid = artifacts.build_run_id(strategy_id="s19 chip.momentum",
                                     run_hash="h", stamp="t")
        self.assertNotIn("/", rid)
        self.assertNotIn("\\", rid)


class WriteRunTest(unittest.TestCase):
    def _tables(self, **over):
        base = {name: pd.DataFrame({"x": [1]}) for name in artifacts.TABLES}
        base.update(over)
        return base

    def test_all_documents_and_tables_are_written(self):
        with tempfile.TemporaryDirectory() as td:
            run = artifacts.create_run_directory(td, "r1")
            artifacts.write_run(run, manifest={"a": 1}, summary={"b": 2},
                                audit={"c": 3}, tables=self._tables())
            names = {p.name for p in run.path.iterdir()}
            for doc in artifacts.DOCUMENTS:
                self.assertIn(f"{doc}.json", names)
            for tbl in artifacts.TABLES:
                self.assertIn(f"{tbl}.csv", names)

    def test_empty_table_is_still_written(self):
        """空表也要有檔案:否則「沒有交易」與「產物遺失」分不出來。"""
        with tempfile.TemporaryDirectory() as td:
            run = artifacts.create_run_directory(td, "r1")
            artifacts.write_run(run, manifest={}, summary={}, audit={},
                                tables=self._tables(trades=pd.DataFrame()))
            self.assertTrue((run.path / "trades.csv").exists())

    def test_no_temp_files_are_left_behind(self):
        with tempfile.TemporaryDirectory() as td:
            run = artifacts.create_run_directory(td, "r1")
            artifacts.write_run(run, manifest={"a": 1}, summary={}, audit={},
                                tables=self._tables())
            self.assertEqual([p for p in run.path.iterdir()
                              if p.name.endswith(".tmp")], [])

    def test_json_round_trips(self):
        with tempfile.TemporaryDirectory() as td:
            run = artifacts.create_run_directory(td, "r1")
            artifacts.write_json(run, "manifest", {"k": [1, 2], "n": None})
            loaded = json.loads((run.path / "manifest.json").read_text("utf-8"))
            self.assertEqual(loaded["k"], [1, 2])


if __name__ == "__main__":
    unittest.main()
