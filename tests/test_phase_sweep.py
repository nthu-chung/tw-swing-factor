# -*- coding: utf-8 -*-
"""統一相位評估(P1-1)的離線回歸測試。

原本的 bug(這支測試逐條釘住)
------------------------------
「每 N 日再平衡跑滿所有等價相位」這條規則有**三份**各自為政的實作:

1. `event_backtest.run_full`:`for phase in range(rebalance_every)`,相位數來自 CLI
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

from backtest import event_backtest
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
# 所有「會重複執行 body」的節點。舊版只認 `for`,所以 `while` 與推導式的
# 手寫相位迴圈可以整份繞過去。
LOOP_NODES = (ast.For, ast.AsyncFor, ast.While,
              ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)


def _repo_sources():
    """repo 內受管的 .py(排除測試、虛擬環境與快取)。"""
    for path in sorted(REPO_ROOT.rglob("*.py")):
        parts = set(path.relative_to(REPO_ROOT).parts)
        if parts & {".venv", "__pycache__", "tests", "outputs", "_cache"}:
            continue
        yield path


# 「自己挑相位降頻」的呼叫名。policy 路徑的相位在降頻時就決定了。
PHASE_SELECTION_CALLS = {"select_decision_snapshots"}
# 事件引擎入口。判準是「同一個迴圈 body 裡既挑相位又跑引擎」——
# 只挑相位不跑引擎是合法的(例如先建相位→日期對照表來檢查各相位不重複,
# 再把單一相位包成 callback 交給 `sweep_phases`,見 `backtest_policy_phases`)。
ENGINE_CALLS = {"backtest_portfolio"}


def _callee_name(call: ast.Call) -> str:
    """`f(...)` → "f";`mod.f(...)` → "f"。只要函式名,不管它掛在誰底下。"""
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _calls_named(node: ast.AST, names):
    """在 body 裡找指定名字的呼叫,**不進入巢狀 def / lambda**(理由同下)。"""
    stack = [node]
    while stack:
        cur = stack.pop()
        for child in ast.iter_child_nodes(cur):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.Lambda)):
                continue
            if isinstance(child, ast.Call) and _callee_name(child) in names:
                yield child
            stack.append(child)


def _calls_with_keyword(node: ast.AST, keyword: str):
    """在 `node` 的子樹裡找帶 `keyword=` 的呼叫,**不進入巢狀的 def / lambda**。

    巢狀函式是「一個相位怎麼跑」的 callback(`event_backtest.run_full` 的 `_run_phase`
    就長在 `for segment ...` 迴圈裡),那是共用掃描的正確用法;真正要抓的是
    「迴圈 body 直接餵 `rebalance_phase=`」,也就是自己掃相位。
    """
    stack = [node]
    while stack:
        cur = stack.pop()
        for child in ast.iter_child_nodes(cur):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.Lambda)):
                continue
            if isinstance(child, ast.Call) and any(
                    kw.arg == keyword for kw in child.keywords):
                yield child
            stack.append(child)


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
        """找不到 MaxDD 欄要由 `_resolve_drawdown_column` **明確**擋下。

        原本只斷言 `assertRaises(KeyError)`,而 pandas 取不存在的欄位本來就會丟
        KeyError —— 實測把 `_resolve_drawdown_column` 改成「找不到就靜默回
        'max_drawdown'」,全套測試仍然全綠,fail-closed 訊息形同不存在。
        """
        rows = self.ROWS.drop(columns=["max_drawdown"])
        with self.assertRaisesRegex(KeyError, "找不到 MaxDD 欄"):
            phases.phase_stats(rows, single_phase_debug=False)

    def test_explicit_missing_drawdown_column_fails_closed(self):
        """呼叫端指名一個不存在的欄位時同樣不得靜默改用別欄。"""
        with self.assertRaisesRegex(KeyError, "沒有 MaxDD 欄"):
            phases.phase_stats(self.ROWS, single_phase_debug=False,
                               drawdown_col="dd_pct")

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
        self.assertIs(event_backtest.sweep_phases, phases.sweep_phases)
        self.assertIs(s19.sweep_phases, phases.sweep_phases)
        self.assertIs(evaluation.sweep_phases, phases.sweep_phases)
        # forward 不自己聚合,拿的是策略回傳的 PhaseSweep。
        self.assertIs(forward_test.PhaseSweep, phases.PhaseSweep)
        self.assertFalse(hasattr(forward_test, "_phase_stats"),
                         "forward 不該再有自己的相位聚合實作")

    @staticmethod
    def _phase_loop_offenders(tree: ast.AST, where: str):
        """回傳這棵語法樹裡的手寫相位迴圈(兩種形狀)。

        1. 迴圈變數叫 phase / ph / rebalance_phase(含 `for` 與推導式)。
        2. **任何**會重複執行的 body(`for` / `while` / 推導式)直接餵引擎的
           `rebalance_phase=`,不限迴圈變數名、不限 `range()`。
        3. 任何迴圈 body 在**降頻**時自己指定相位,也就是呼叫
           `select_decision_snapshots(..., phase=...)`。

        第 2 條原本只認「`for ... in range(...)`」,所以
        `offsets = list(range(n))` + `for off in offsets:` 或改寫成 `while`
        就整份繞過去 —— 閘門靠的是**形狀慣例**而不是行為。

        第 3 條是 2026-08-16 補的:`research/golden_path.py` 當時寫成

            for idx in range(event_backtest.WEEKLY_PHASES):
                picked = event_backtest.select_decision_snapshots(..., phase=idx)
                res = event_backtest.backtest_portfolio(signal_frame=sub, ...)

        迴圈變數叫 `idx`(不在名單裡)、引擎收的是 `signal_frame` 而不是
        `rebalance_phase=`,所以前兩條都抓不到,測試全綠但 repo 裡確實有第四份
        手寫相位掃描。policy 路徑的相位是**在降頻時**決定的,所以要擋的是
        「在迴圈裡自己挑星期幾」這個行為。
        """
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
                target = node.target
                if isinstance(target, ast.Name) and target.id in PHASE_LOOP_NAMES:
                    offenders.append(f"{where}:{getattr(node, 'lineno', '?')} "
                                     f"for {target.id}")
                    continue
            if not isinstance(node, LOOP_NODES):
                continue
            for call in _calls_with_keyword(node, "rebalance_phase"):
                offenders.append(
                    f"{where}:{getattr(node, 'lineno', '?')} "
                    f"{type(node).__name__} body 直接餵 rebalance_phase")
                break
            picks_phase = any(
                _callee_name(c) in PHASE_SELECTION_CALLS
                for c in _calls_with_keyword(node, "phase"))
            runs_engine = any(True for _ in _calls_named(node, ENGINE_CALLS))
            if picks_phase and runs_engine:
                offenders.append(
                    f"{where}:{getattr(node, 'lineno', '?')} "
                    f"{type(node).__name__} body 既挑相位又跑引擎"
                    "(select_decision_snapshots(phase=...) + backtest_portfolio)")
        return offenders

    def test_no_module_hand_writes_another_phase_loop(self):
        """禁止再長出第四份手寫相位迴圈(AST 掃描)。

        要跑相位請用 `evaluation.phases.sweep_phases`。

        **未解決的偶發誤報(2026-08-16 登記)**:本測試在某一輪 7 次完整
        `unittest discover` 中失敗 1 次,而且失敗當下沒有留下 offender 清單;
        事後以相同 repo 內容單獨重跑掃描結果為空,連續多次完整套件重跑亦全綠。
        掃描本身是決定性的(`sorted(rglob)` + AST),所以失敗當下 repo 樹的內容
        必然與事後不同 —— 最可能是同套件內某個測試以 `subprocess.run(cwd=repo)`
        跑子行程時,`rglob` 正好走到瞬間存在的檔案。**此為推測,未證實。**

        影響方向是「假警報」而非「漏放行」,但會偶爾誤報的閘門遲早會被 skip 掉。
        待辦:讓掃描先把檔案列表快照下來再解析,並在失敗訊息中印出 offender 的
        絕對路徑與 mtime。**查清楚之前不得放寬或 skip 此測試。**
        """
        offenders = []
        for path in _repo_sources():
            if path in PHASE_LOOP_ALLOWED:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            offenders += self._phase_loop_offenders(
                tree, str(path.relative_to(REPO_ROOT)))
        self.assertEqual(offenders, [], f"出現手寫相位迴圈:{offenders}")

    def test_scanner_catches_the_golden_path_shape_that_slipped_through(self):
        """2026-08-16 實際漏抓的形狀:迴圈變數 `idx` + 在降頻時自己挑相位。

        這正是 `research/golden_path.py` 當時的寫法。它不叫 phase、也沒有把
        `rebalance_phase=` 餵進引擎,所以原本兩條規則都放行,測試全綠但 repo 裡
        確實存在第四份手寫相位掃描。
        """
        golden_path_shape = (
            "for idx in range(event_backtest.WEEKLY_PHASES):\n"
            "    picked = set(event_backtest.select_decision_snapshots(\n"
            "        all_days, decision_frequency='weekly', phase=idx))\n"
            "    sub = signals[signals['date'].isin(picked)]\n"
            "    res = event_backtest.backtest_portfolio(signal_frame=sub)\n")
        found = self._phase_loop_offenders(
            ast.parse(golden_path_shape), "golden_path_shape")
        self.assertTrue(found, "守衛必須抓到『在迴圈裡自己挑相位降頻』")

        # 反向一:把單一相位包成 callback 交給共用掃描,不得被誤報。
        callback_shape = (
            "def _run_phase(phase):\n"
            "    picked = event_backtest.select_decision_snapshots(\n"
            "        all_days, decision_frequency='weekly', phase=phase)\n"
            "    return event_backtest.backtest_portfolio(signal_frame=picked)\n"
            "sweep = sweep_phases(_run_phase, n_phases=5)\n")
        self.assertEqual(
            self._phase_loop_offenders(ast.parse(callback_shape), "callback"),
            [], "把單一相位包成 callback 交給 sweep_phases 是正確用法")

        # 反向二:只建「相位 → 決策日」對照表(不跑引擎)也不得被誤報。
        # `backtest_policy_phases` 就是這樣先檢查各相位是否選到同一批日子;
        # 把它擋掉會逼人拿掉那道退化檢查,反而更糟。
        precompute_shape = (
            "selections = {\n"
            "    idx: select_decision_snapshots(days, phase=idx)\n"
            "    for idx in range(n_phases)\n"
            "}\n"
            "if len({tuple(v) for v in selections.values()}) < n_phases:\n"
            "    raise ValueError('相位退化')\n")
        self.assertEqual(
            self._phase_loop_offenders(ast.parse(precompute_shape), "precompute"),
            [], "只建相位對照表、不跑引擎,是合法用法")

    def test_scanner_catches_loops_that_are_not_for_x_in_range(self):
        """掃描器本身的回歸測試:它擋的是**行為**,不是某一種寫法。

        原 bug(2026-08-15 審查實測):偵測只認迴圈變數名在
        {phase, ph, rebalance_phase} 或 `for ... in range(...)` 兩種形狀,
        所以下面四種寫法都能把手寫相位迴圈原封搬回來而測試全綠。
        """
        evasions = {
            "list_var": (
                "offsets = list(range(n))\n"
                "for off in offsets:\n"
                "    backtest_portfolio(rebalance_phase=off)\n"),
            "while_loop": (
                "i = 0\n"
                "while i < n:\n"
                "    backtest_portfolio(rebalance_phase=i)\n"
                "    i += 1\n"),
            "listcomp": (
                "rows = [backtest_portfolio(rebalance_phase=o) "
                "for o in offsets]\n"),
            "map_over_enumerate": (
                "for idx, _ in enumerate(offsets):\n"
                "    run(rebalance_phase=idx)\n"),
        }
        for name, src in evasions.items():
            with self.subTest(shape=name):
                found = self._phase_loop_offenders(ast.parse(src), name)
                self.assertTrue(found, f"{name} 這種寫法沒被偵測到")

    def test_scanner_allows_a_callback_defined_inside_an_unrelated_loop(self):
        """反向:`for segment in ...:` 裡定義的單相位 callback 不是手寫相位迴圈。

        `event_backtest.run_full` 就長這樣(掃描交給 `sweep_phases`,迴圈跑的是 IS/OS
        兩段)。掃描器若把它判成違規,大家只會把它加進白名單,閘門就白做了。
        """
        src = ("for segment in ('IS', 'OS'):\n"
               "    def _run_phase(phase):\n"
               "        return backtest_portfolio(rebalance_phase=phase)\n"
               "    sweep_phases(_run_phase, n_phases=20)\n")
        self.assertEqual(self._phase_loop_offenders(ast.parse(src), "ok"), [])


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
                mock.patch.object(event_backtest.uni, "get_universe", return_value=["A"]),
                mock.patch.object(event_backtest, "backtest_portfolio",
                                  side_effect=_fake_engine(dates)),
                mock.patch.object(event_backtest, "factor_ic", return_value=pd.DataFrame()),
                mock.patch.object(event_backtest, "sweep_phases", spy),
                mock.patch.object(config, "OUTPUT_DIR", Path(tmp)),
            ):
                result, _ = event_backtest.run_full(
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
            m = freeze_manifest.build_manifest(
                "phase_sweep", self.spec, freeze_date=self.FREEZE_DATE,
                # holdout 邊界是 manifest 的必要內容(未解析 = 不可靠的凍結版本)
                calendar=pd.bdate_range("2024-06-24", periods=500))
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
            mock.patch.object(event_backtest, "backtest_portfolio",
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
