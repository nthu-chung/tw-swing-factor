# -*- coding: utf-8 -*-
"""統一相位評估(P1-1)的離線回歸測試。

原本的 bug(這支測試逐條釘住)
------------------------------
「每 N 日再平衡跑滿所有等價相位」這條規則有**三份**各自為政的實作:

1. `backtest.run_full`:`for phase in range(rebalance_every)`,相位數來自 CLI
   參數(預設 5);中位/最小/最差 MaxDD 是印出來時當場算的,沒有進回傳值,
   呼叫端拿不到。
2. `strategies.s19_chip_momentum.evaluate`:`for ph in range(rebalance_days)`
   = 20 個相位;欄位名 `max_dd`,和引擎的 `max_drawdown` 不同。
3. `forward_test._phase_stats`:第三份聚合,而且
   `single_phase_debug = bool(len(df) == 1)` —— 拿**結果**當**意圖**:
   20 相位掃描只有一個相位產出結果時會被誤標成 debug(數字看起來像 debug
   就被降級),而再平衡天數真的是 1 的正式全相位掃描也會被誤標。

三份實作 = 三種相位數慣例 + 兩種 MaxDD 欄名 + 一個會說謊的旗標。現在掃描與
聚合都在 `evaluation/phases.py`,呼叫端只提供「一個相位怎麼跑」。

⚠ 這裡的假 summary 只驗證掃描與聚合的行為,不代表任何策略績效。
"""
from __future__ import annotations

import ast
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

import backtest
import config
import evaluation
import forward_test
import freeze_manifest
from evaluation import phases
from strategies import s19_chip_momentum as s19

REPO_ROOT = Path(__file__).resolve().parent.parent
# 唯一允許出現手寫相位迴圈的檔案(共用實作本身)。
PHASE_LOOP_ALLOWED = {REPO_ROOT / "evaluation" / "phases.py"}
PHASE_LOOP_NAMES = {"phase", "ph", "rebalance_phase"}


def _repo_sources():
    """repo 內受管的 .py(排除測試、虛擬環境與快取)。"""
    for path in sorted(REPO_ROOT.rglob("*.py")):
        parts = set(path.relative_to(REPO_ROOT).parts)
        if parts & {".venv", "__pycache__", "tests", "outputs", "_cache"}:
            continue
        yield path


# ── 1. 掃描本體 ───────────────────────────────────────────────────────────
class PhaseIndicesTest(unittest.TestCase):
    def test_full_sweep_covers_every_equivalent_phase(self):
        self.assertEqual(phases.phase_indices(20), tuple(range(20)))
        self.assertEqual(phases.phase_indices(5), tuple(range(5)))

    def test_single_phase_debug_runs_only_phase_zero(self):
        self.assertEqual(phases.phase_indices(20, single_phase_debug=True), (0,))

    def test_invalid_phase_count_fails_closed(self):
        for bad in (0, -1):
            with self.assertRaises(ValueError):
                phases.phase_indices(bad)

    def test_sweep_runs_each_phase_once_and_records_empty_phases(self):
        seen = []

        def run_phase(p):
            seen.append(p)
            return None if p == 2 else {"sharpe": float(p), "max_drawdown": -0.1}

        sweep = phases.sweep_phases(run_phase, n_phases=4)
        self.assertEqual(seen, [0, 1, 2, 3])
        self.assertEqual(sweep.phases_run, (0, 1, 2, 3))
        self.assertEqual(sweep.phases_without_result, (2,))
        self.assertEqual(list(sweep.rows["phase"]), [0, 1, 3])
        self.assertTrue(sweep.full_sweep)

    def test_sweep_rejects_phase_mismatch_in_row(self):
        with self.assertRaisesRegex(ValueError, "不符"):
            phases.sweep_phases(lambda p: {"phase": p + 1, "sharpe": 1.0,
                                           "max_drawdown": -0.1}, n_phases=2)


# ── 2. 聚合:中位 / 最小 / 最差 MaxDD ─────────────────────────────────────
class PhaseStatsTest(unittest.TestCase):
    ROWS = pd.DataFrame({
        "phase": [0, 1, 2, 3],
        "sharpe": [1.0, 3.0, 2.0, -1.0],
        "max_drawdown": [-0.10, -0.50, -0.20, -0.05],
        "n_trades": [10, 20, 30, 40],
    })

    def test_median_min_max_and_worst_drawdown(self):
        st = phases.phase_stats(self.ROWS, single_phase_debug=False)
        self.assertEqual(st["n_phases"], 4)
        self.assertAlmostEqual(st["sharpe_median"], 1.5)
        self.assertAlmostEqual(st["sharpe_min"], -1.0)
        self.assertAlmostEqual(st["sharpe_max"], 3.0)
        self.assertAlmostEqual(st["n_trades_median"], 25.0)
        self.assertAlmostEqual(st["n_trades_total"], 100.0)

    def test_worst_drawdown_is_the_worst_phase_not_median_or_mean(self):
        """「最差 MaxDD」= 所有相位裡最糟的那一個(最負),不是中位也不是平均。

        取錯的話,單一相位崩掉 -50% 會被 -21.25% 的平均或 -15% 的中位藏起來 ——
        而實際下單只會走一條路徑,那條路徑可能就是最差的那個相位。
        """
        st = phases.phase_stats(self.ROWS, single_phase_debug=False)
        self.assertAlmostEqual(st["worst_max_drawdown"], -0.50)
        dd = self.ROWS["max_drawdown"]
        self.assertNotAlmostEqual(st["worst_max_drawdown"], float(dd.median()))
        self.assertNotAlmostEqual(st["worst_max_drawdown"], float(dd.mean()))
        self.assertNotAlmostEqual(st["worst_max_drawdown"], float(dd.max()))

    def test_positive_drawdown_convention_fails_closed(self):
        """MaxDD 慣例翻成正值時取 min 會回報**最好**的相位 → 直接 raise。"""
        rows = self.ROWS.copy()
        rows["max_drawdown"] = rows["max_drawdown"].abs()
        with self.assertRaisesRegex(ValueError, "MaxDD"):
            phases.phase_stats(rows, single_phase_debug=False)

    def test_missing_drawdown_column_fails_closed(self):
        rows = self.ROWS.drop(columns=["max_drawdown"])
        with self.assertRaises(KeyError):
            phases.phase_stats(rows, single_phase_debug=False)

    def test_legacy_max_dd_column_is_still_readable(self):
        rows = self.ROWS.rename(columns={"max_drawdown": "max_dd"})
        st = phases.phase_stats(rows, single_phase_debug=False)
        self.assertAlmostEqual(st["worst_max_drawdown"], -0.50)


# ── 3. single_phase_debug 來自意圖,不是列數 ──────────────────────────────
class SinglePhaseDebugFlagTest(unittest.TestCase):
    ROW = {"sharpe": 1.0, "max_drawdown": -0.1, "n_trades": 5}

    def test_flag_is_true_only_when_debug_is_requested(self):
        sweep = phases.sweep_phases(lambda p: dict(self.ROW), n_phases=20,
                                    single_phase_debug=True)
        self.assertTrue(sweep.single_phase_debug)
        self.assertTrue(sweep.stats()["single_phase_debug"])
        self.assertEqual(sweep.phases_run, (0,))
        self.assertFalse(sweep.full_sweep)

    def test_full_sweep_of_one_phase_is_not_debug(self):
        """再平衡天數真的是 1 → 一列,但那是**完整**掃描,不是 debug。"""
        sweep = phases.sweep_phases(lambda p: dict(self.ROW), n_phases=1)
        self.assertEqual(len(sweep), 1)
        self.assertFalse(sweep.single_phase_debug)
        self.assertFalse(sweep.stats()["single_phase_debug"])
        self.assertTrue(sweep.full_sweep)

    def test_full_sweep_with_only_one_result_is_not_debug(self):
        """20 相位只有 1 個相位有結果 → 仍不是 debug(舊版會誤標成 True)。"""
        sweep = phases.sweep_phases(
            lambda p: dict(self.ROW) if p == 7 else None, n_phases=20)
        self.assertEqual(len(sweep), 1)
        self.assertFalse(sweep.stats()["single_phase_debug"])
        self.assertEqual(sweep.stats()["n_phases_full"], 20)
        self.assertEqual(sweep.stats()["n_phases_requested"], 20)


# ── 4. 三個入口共用同一份實作 ─────────────────────────────────────────────
class SingleImplementationTest(unittest.TestCase):
    def test_entrypoints_bind_the_same_sweep_function(self):
        self.assertIs(backtest.sweep_phases, phases.sweep_phases)
        self.assertIs(s19.sweep_phases, phases.sweep_phases)
        self.assertIs(evaluation.sweep_phases, phases.sweep_phases)
        # forward 不自己聚合,拿的是策略回傳的 PhaseSweep。
        self.assertIs(forward_test.PhaseSweep, phases.PhaseSweep)
        self.assertFalse(hasattr(forward_test, "_phase_stats"),
                         "forward 不該再有自己的相位聚合實作")

    def test_no_module_hand_writes_another_phase_loop(self):
        """禁止再長出第四份手寫相位迴圈(AST 掃描)。

        兩種形狀都擋:迴圈變數叫 phase/ph/rebalance_phase,或在 `range()` 迴圈
        裡直接餵引擎的 `rebalance_phase=`。要跑相位請用
        `evaluation.phases.sweep_phases`。
        """
        offenders = []
        for path in _repo_sources():
            if path in PHASE_LOOP_ALLOWED:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.For, ast.comprehension)):
                    continue
                target = node.target
                if isinstance(target, ast.Name) and target.id in PHASE_LOOP_NAMES:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:"
                                     f"{getattr(node, 'lineno', '?')} for {target.id}")
                    continue
                iter_node = node.iter
                is_range = (isinstance(iter_node, ast.Call)
                            and isinstance(iter_node.func, ast.Name)
                            and iter_node.func.id == "range")
                if not is_range or not isinstance(node, ast.For):
                    continue
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Call) and any(
                            kw.arg == "rebalance_phase" for kw in sub.keywords):
                        offenders.append(
                            f"{path.relative_to(REPO_ROOT)}:{node.lineno} "
                            "range 迴圈直接餵 rebalance_phase")
                        break
        self.assertEqual(offenders, [], f"出現手寫相位迴圈:{offenders}")


# ── 5. run_full(正式 IS/OS)走共用掃描 ───────────────────────────────────
def _fake_engine(dates):
    def fake_portfolio(*_args, **kwargs):
        start = pd.Timestamp(kwargs.get("start_date") or dates[0])
        end = pd.Timestamp(kwargs.get("end_date") or dates[-1])
        eq_dates = dates[(dates >= start) & (dates <= end)]
        phase = int(kwargs.get("rebalance_phase", 0))
        return {
            "summary": {
                "n_trades": 10 + phase,
                "ann_ret": 0.10,
                "sharpe": [1.0, 3.0, 2.0][phase],
                "max_drawdown": [-0.10, -0.50, -0.20][phase],
                "cum_ret": 0.12,
                "data": {"integrity_bypassed": False},
                "universe": {"survivorship_free": True},
                "eval_audit": {"eval_window": [str(eq_dates[0].date()),
                                               str(eq_dates[-1].date())]},
            },
            "equity_curve": pd.DataFrame({"date": eq_dates, "equity": 1.0}),
            "trades": pd.DataFrame(),
        }
    return fake_portfolio


class RunFullPhaseSweepTest(unittest.TestCase):
    def _run(self, **kwargs):
        dates = pd.bdate_range("2024-01-01", periods=120)
        spy = mock.Mock(side_effect=phases.sweep_phases)
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.object(backtest.uni, "get_universe", return_value=["A"]),
                mock.patch.object(backtest, "backtest_portfolio",
                                  side_effect=_fake_engine(dates)),
                mock.patch.object(backtest, "factor_ic", return_value=pd.DataFrame()),
                mock.patch.object(backtest, "sweep_phases", spy),
                mock.patch.object(config, "OUTPUT_DIR", Path(tmp)),
            ):
                result, _ = backtest.run_full(
                    sample=True, top_n=1, rebalance_every=3,
                    dynamic_enabled=False, **kwargs)
        return result, spy

    def test_run_full_delegates_to_shared_sweep(self):
        """patch 共用函式 → run_full 立刻受影響(代表它真的走那一份)。"""
        result, spy = self._run()
        self.assertEqual(spy.call_count, 2, "IS 與 OS 各一次掃描")
        for call in spy.call_args_list:
            self.assertEqual(call.kwargs["n_phases"], 3)
            self.assertFalse(call.kwargs["single_phase_debug"])
        self.assertEqual(len(result["phases"]), 6)

    def test_run_full_reports_median_min_and_worst_drawdown(self):
        result, _ = self._run()
        for segment in ("IS", "OS"):
            st = result["phase_stats"][segment]
            self.assertEqual(st["n_phases"], 3)
            self.assertAlmostEqual(st["sharpe_median"], 2.0)
            self.assertAlmostEqual(st["sharpe_min"], 1.0)
            self.assertAlmostEqual(st["worst_max_drawdown"], -0.50)
            self.assertFalse(st["single_phase_debug"])
        self.assertFalse(result["single_phase_debug"])

    def test_single_phase_debug_runs_one_phase_and_is_flagged(self):
        """單相位只能 debug,而且必須在結果裡明示(否則會被當成正式績效)。"""
        result, spy = self._run(single_phase_debug=True)
        for call in spy.call_args_list:
            self.assertTrue(call.kwargs["single_phase_debug"])
        self.assertTrue(result["single_phase_debug"])
        self.assertEqual(set(result["phases"]["phase"]), {0})
        self.assertEqual(len(result["phases"]), 2)   # IS/OS 各一個相位
        for segment in ("IS", "OS"):
            self.assertTrue(result["phase_stats"][segment]["single_phase_debug"])


# ── 6. forward 走同一份掃描,且拒絕單相位 ────────────────────────────────
def _forward_panel(n_days: int = 120, sids=("A", "B", "C", "D")) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    rows = []
    for k, sid in enumerate(sids):
        px = 100.0
        for d in pd.bdate_range("2026-01-01", periods=n_days):
            px *= 1.0 + rng.normal(0.001 * (k + 1), 0.02)
            rows.append({"date": d, "stock_id": sid, "name": f"N{sid}",
                         "close": px, "volume": 1e6 * (k + 1),
                         "foreign_net": rng.normal(1e4, 5e3),
                         "trust_net": rng.normal(5e3, 2e3),
                         "in_dynamic_universe": True, "trend_ok": True})
    return pd.DataFrame(rows).sort_values(["date", "stock_id"]).reset_index(drop=True)


class _FakeProvider:
    all_symbols = ["A", "B", "C", "D"]

    def metadata(self):
        return {"candidate_rule": "month_M_uses_only_calendar_month_M_minus_1"}


class ForwardUsesSharedSweepTest(unittest.TestCase):
    FREEZE_DATE = "2026-02-02"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)
        self.panel = _forward_panel()
        self.panel.attrs["universe_provider"] = _FakeProvider()
        # 3 日再平衡 = 3 個相位,測試跑得快;凍結的是這份規格,forward 必須照它跑。
        self.spec = s19.SPEC.replace(portfolio={"rebalance_days": 3})

    def _manifest(self) -> Path:
        with mock.patch.object(config, "OUTPUT_DIR", self.out):
            m = freeze_manifest.build_manifest("phase_sweep", self.spec,
                                               freeze_date=self.FREEZE_DATE)
            path = freeze_manifest.manifest_path(m)
            path.write_text(json.dumps(m, ensure_ascii=False, indent=2, default=str),
                            encoding="utf-8")
        return path

    def _fake_summary(self, n: int) -> dict:
        return {"sharpe": [1.0, 3.0, 2.0][n % 3], "ann_ret": 0.1, "ann_vol": 0.2,
                "max_drawdown": [-0.10, -0.50, -0.20][n % 3],
                "n_trades": 40, "win_rate": 0.5, "payoff_ratio": 2.0,
                "eval_audit": {"days_beyond_last_pick": 0},
                "universe": {"candidate_pool_pit": True}}

    def _run_forward(self, manifest: Path, spy):
        calls = []

        def fake_bt(**kwargs):
            calls.append(kwargs)
            return {"summary": self._fake_summary(len(calls) - 1),
                    "trades": pd.DataFrame(), "equity_curve": pd.DataFrame()}

        with (
            mock.patch.object(config, "OUTPUT_DIR", self.out),
            mock.patch.object(s19, "build_panel",
                              return_value=(self.panel, ["A", "B", "C", "D"])),
            mock.patch.object(backtest, "backtest_portfolio",
                              side_effect=lambda **kw: fake_bt(**kw)),
            mock.patch.object(s19, "sweep_phases", spy),
        ):
            return forward_test.run(str(manifest))

    def test_forward_goes_through_the_shared_sweep(self):
        """patch 共用函式 → forward 也受影響:它沒有自己的第二份掃描。"""
        spy = mock.Mock(side_effect=phases.sweep_phases)
        payload = self._run_forward(self._manifest(), spy)
        self.assertEqual(spy.call_count, 1)
        self.assertEqual(spy.call_args.kwargs["n_phases"], 3)
        self.assertFalse(spy.call_args.kwargs["single_phase_debug"])
        st = payload["phase_stats"]
        self.assertEqual(st["n_phases"], 3)
        self.assertEqual(st["n_phases_full"], 3)
        self.assertAlmostEqual(st["sharpe_median"], 2.0)
        self.assertAlmostEqual(st["sharpe_min"], 1.0)
        self.assertAlmostEqual(st["worst_max_drawdown"], -0.50)
        self.assertFalse(st["single_phase_debug"])

    def test_forward_refuses_a_single_phase_debug_sweep(self):
        """單相位掃描不得產出 forward OOS 數字(挑路徑)。"""
        debug_sweep = phases.sweep_phases(
            lambda p: {"sharpe": 1.0, "max_drawdown": -0.1, "n_trades": 40,
                       "days_beyond_last_pick": 0, "candidate_pool_pit": True},
            n_phases=3, single_phase_debug=True)
        with self.assertRaisesRegex(RuntimeError, "single_phase_debug"):
            with (
                mock.patch.object(config, "OUTPUT_DIR", self.out),
                mock.patch.object(s19, "build_panel",
                                  return_value=(self.panel, ["A", "B", "C", "D"])),
                mock.patch.object(s19, "evaluate_sweep", return_value=debug_sweep),
            ):
                forward_test.run(str(self._manifest()))


if __name__ == "__main__":
    unittest.main()
