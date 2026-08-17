# -*- coding: utf-8 -*-
"""Python ``make_signals`` 到事件回測成果的黑箱驗收。

本測試是交給下一位實作者的 contract-first 驗收，交接時**預期 availability 紅燈**；
實作者要建立正式 runner 並讓本檔全綠，不得刪除、skip 或把它改成只 mock engine。
synthetic fixture 必須離線，但仍要走真正的 SignalFrame validator、
StrategyPositionPolicy 與唯一事件引擎。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import pandas as pd


try:
    RUNNER_AVAILABLE = importlib.util.find_spec("research.golden_path") is not None
except ModuleNotFoundError:
    RUNNER_AVAILABLE = False


class GoldenPathAvailabilityTest(unittest.TestCase):
    def test_public_runner_module_exists(self):
        self.assertTrue(
            RUNNER_AVAILABLE,
            "尚未實作 research.golden_path；請依 "
            "研究流程需要一個公開的 runner 入口",
        )


@unittest.skipUnless(RUNNER_AVAILABLE, "等待實作者新增 research.golden_path")
class GoldenPathBlackBoxTest(unittest.TestCase):
    def test_s19_reference_runs_event_engine_and_writes_auditable_result(self):
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as td:
            cmd = [
                sys.executable, "-m", "research.golden_path",
                "--strategy", "s19_reference_make_signals",
                "--fixture", "synthetic",
                "--capital", "research",
                "--output-dir", td,
            ]
            proc = subprocess.run(
                cmd, cwd=repo, text=True, capture_output=True, timeout=120,
            )
            self.assertEqual(
                proc.returncode, 0,
                f"runner 失敗\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}",
            )

            run_dirs = [p for p in Path(td).iterdir() if p.is_dir()]
            self.assertEqual(len(run_dirs), 1, "一次執行必須產生唯一 run directory")
            run = run_dirs[0]
            required = {
                "manifest.json", "summary.json", "audit.json",
                "signals.csv", "phase_results.csv", "decisions.csv",
                "orders.csv", "trades.csv", "equity_curve.csv",
                "candidate_screen.csv", "candidate_screen.txt",
            }
            self.assertTrue(required.issubset({p.name for p in run.iterdir()}))

            manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
            summary = json.loads((run / "summary.json").read_text(encoding="utf-8"))
            audit = json.loads((run / "audit.json").read_text(encoding="utf-8"))

            self.assertEqual(manifest["strategy_id"], "s19_reference_make_signals")
            self.assertEqual(manifest["fixture"], "synthetic")
            self.assertEqual(manifest["capital_scenario"], "research")
            self.assertIn("strategy_rule_hash", manifest)
            self.assertIn("evaluation_run_hash", manifest)

            metric_keys = {
                "initial_capital", "final_capital", "net_profit",
                "cum_return", "ann_return", "ann_volatility", "sharpe",
                "sortino", "max_drawdown", "turnover", "n_trades",
                "win_rate", "benchmark", "excess_vs_benchmark",
            }
            self.assertTrue(metric_keys.issubset(summary))
            self.assertAlmostEqual(
                float(summary["net_profit"]),
                float(summary["final_capital"]) - float(summary["initial_capital"]),
                places=6,
            )
            self.assertAlmostEqual(
                float(summary["cum_return"]),
                float(summary["final_capital"]) / float(summary["initial_capital"]) - 1.0,
                places=9,
            )

            signals = pd.read_csv(run / "signals.csv")
            phases = pd.read_csv(run / "phase_results.csv")
            decisions = pd.read_csv(run / "decisions.csv")
            equity = pd.read_csv(run / "equity_curve.csv")
            candidate_screen = pd.read_csv(run / "candidate_screen.csv")
            self.assertGreater(len(signals), 0)
            self.assertGreater(len(decisions), 0)
            self.assertGreater(len(equity), 1)
            self.assertGreater(len(candidate_screen), 0)
            self.assertTrue((candidate_screen["list_type"]
                             == "research_candidate_not_order").all())
            self.assertEqual(len(phases), 5, "weekly 正式比較必須跑滿五個等價 phase")
            self.assertTrue({"sharpe", "sortino", "max_drawdown"}.issubset(phases))

            self.assertTrue(audit["pipeline_complete"])
            self.assertTrue(audit["real_event_engine_used"])
            self.assertTrue(audit["signal_validator_passed"])
            self.assertTrue(audit["all_weekly_phases_ran"])
            self.assertFalse(
                audit["formal_evidence_ready"],
                "合成資料只證明管線，絕對不能被標成策略有效或 clean OOS",
            )
            self.assertEqual(audit["performance_claim"], "none")


if __name__ == "__main__":
    unittest.main()
