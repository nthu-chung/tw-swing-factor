# -*- coding: utf-8 -*-
"""freeze / forward 正式驗證路徑的離線回歸測試(P0-1)。

原本的 bug(全部實測過,這支測試逐條釘住):

1. **label 既不進檔名也不進 hash**:`build_manifest('strat_A')` 與 `('strat_B')`
   產生同一個 `rules_sha256_16`(f2c27945b02bbe24)與同一個檔名
   `FROZEN_MANIFEST_{date}.json` → 兩套研究互相覆寫、也無法辨識。
2. **手維護的 `FROZEN_KEYS` 只有 34 個**,config 有 92 個大寫參數;缺席的正好
   是最會改變結果的那些(`SELF_ADJUST_PRICES`、`ALLOW_UNADJUSTED_BACKTEST`、
   `BT_ORDER_SIZE_MODE`、漲跌停/處置模型、`BT_STALE_EXIT_DAYS`、IS-OS/embargo…)。
3. **S19 真正決定績效的參數根本沒被凍**:視窗/權重/持股數 10/再平衡 20 日/
   MA60 出場/-15% 停損是模組常數,投組那半還是在 manifest 產生**之後**才被
   `_apply_portfolio_config()` 寫進 config。
4. **forward_test.py**:沒有基準(docstring 卻說有)、只跑單一相位、吃引擎簽章
   預設 `rebalance_every=5 / top_n=3`(不是凍結的 20/10)、傳 symbols 因此
   繞過策略與 PIT、每次重跑覆寫同名輸出、零測試。

⚠ 這裡的假 summary 只用來驗證「參數與候選池有沒有正確傳遞」,不代表任何策略
績效;S19 的證據等級仍是 blocked。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

import config
import freeze_manifest
from strategy_kit.spec import StrategySpec
from strategies import s19_chip_momentum as s19


# ── 測試素材 ──────────────────────────────────────────────────────────────
def _panel(n_days: int = 120, sids=("A", "B", "C", "D"), seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    dates = pd.bdate_range("2026-01-01", periods=n_days)
    for k, sid in enumerate(sids):
        px = 100.0
        for d in dates:
            px *= 1.0 + rng.normal(0.001 * (k + 1), 0.02)
            rows.append({
                "date": d, "stock_id": sid, "name": f"N{sid}",
                "close": px, "volume": 1e6 * (k + 1),
                "foreign_net": rng.normal(1e4 * (k + 1), 5e3),
                "trust_net": rng.normal(5e3, 2e3),
                "in_dynamic_universe": True, "trend_ok": True,
            })
    return pd.DataFrame(rows).sort_values(["date", "stock_id"]).reset_index(drop=True)


# 凍結資料的交易日曆。manifest 的 holdout 邊界(IS／embargo／OS 日期)是必要
# 內容:沒有它 `validate_manifest` 會判 `ok=False`,forward 也拒用 —— 所以每一份
# 要被 validate / apply / forward 使用的 manifest 都得帶日曆。
CAL = pd.bdate_range("2024-06-24", periods=500)


class _FakeProvider:
    """假的 PIT provider(只要能被辨識並帶著走就夠;引擎在此被 mock)。"""

    all_symbols = ["A", "B", "C", "D"]

    def metadata(self):
        return {"candidate_rule": "month_M_uses_only_calendar_month_M_minus_1"}


def _fake_summary(n: int, *, pit: bool = True, beyond: int = 0) -> dict:
    return {
        "sharpe": 0.5 + 0.01 * n, "ann_ret": 0.1, "ann_vol": 0.2,
        "max_drawdown": -0.10 - 0.001 * n, "n_trades": 40 + n,
        "win_rate": 0.5, "payoff_ratio": 2.0,
        "eval_audit": {"days_beyond_last_pick": beyond},
        "universe": {"candidate_pool_pit": pit},
    }


def _signal_panel(n_days: int = 140, sids=("A", "B", "C", "D", "E"),
                  seed: int = 11) -> pd.DataFrame:
    """給訊號測試用的稠密 panel:**量能也要隨機**。

    舊的 `_panel()` 每檔股票的 volume 是常數,`ts_mean(volume, w)` 換視窗只差在
    暖身期的 NaN —— 用它來測 `vol_window` 幾乎測不到東西。
    """
    rng = np.random.default_rng(seed)
    rows = []
    for k, sid in enumerate(sids):
        px = 100.0
        for d in pd.bdate_range("2025-01-01", periods=n_days):
            px *= 1.0 + rng.normal(0.001 * (k + 1), 0.02)
            rows.append({
                "date": d, "stock_id": sid, "name": f"N{sid}", "close": px,
                "volume": float(rng.lognormal(13 + 0.2 * k, 0.6)),
                "foreign_net": rng.normal(1e4 * (k + 1), 2e4),
                "trust_net": rng.normal(5e3, 1e4),
                "in_dynamic_universe": True, "trend_ok": True,
            })
    return pd.DataFrame(rows).sort_values(["date", "stock_id"]).reset_index(drop=True)


def _series_differs(a: pd.Series, b: pd.Series) -> bool:
    """兩條分數序列是否不同(NaN 用哨兵值比對,NaN 位置變了也算不同)。"""
    sentinel = -1e9
    return bool((a.fillna(sentinel).values != b.fillna(sentinel).values).any())


def _legacy_manifest() -> dict:
    """schema 1 的舊 manifest(扁平 rules、沒有策略段、只有 34 個 key)。"""
    return {
        "label": "momentum_only_baseline",
        "freeze_date": "2026-07-24",
        "data_snapshot_at_freeze": "2026-06-22",
        "git_commit": "87a1d5ab4dd009cbc325e2f96020a52e453576d5",
        "rules_sha256_16": "f2c27945b02bbe24",
        "rules": {"FACTOR_WEIGHTS": {"momentum": 1.0}, "MIN_COMPOSITE": 50.0},
    }


# ── 1. hash / 檔名 ────────────────────────────────────────────────────────
class ManifestHashAndFilenameTest(unittest.TestCase):
    def test_same_rules_different_label_same_hash_but_different_filename(self):
        """相同規則、不同 label:hash 必須相同(同一套規則),檔名必須不同。

        原本兩者的 hash 與檔名都相同 —— 第二次凍結會被當成「已存在,不可覆寫」
        而靜默放棄,兩套研究共用一個名字。
        """
        a = freeze_manifest.build_manifest("strat_A", freeze_date="2026-08-15")
        b = freeze_manifest.build_manifest("strat_B", freeze_date="2026-08-15")
        self.assertEqual(a["rules_sha256_16"], b["rules_sha256_16"],
                         "label 不可進規則 hash")
        self.assertNotEqual(freeze_manifest.manifest_path(a).name,
                            freeze_manifest.manifest_path(b).name)
        self.assertIn("strat_A", freeze_manifest.manifest_path(a).name)

    def test_changing_any_strategy_param_changes_hash(self):
        """策略參數改任一個(訊號或投組)→ hash 必須改變。

        原本 S19 的參數完全不在 manifest 裡:10 檔改 3 檔、20 日改 5 日,
        hash 一個字都不會變。
        """
        base = freeze_manifest.build_manifest("x", freeze_date="2026-08-15")
        h0 = base["rules_sha256_16"]
        variants = [
            s19.SPEC.replace(portfolio={"max_positions": 3}),
            s19.SPEC.replace(portfolio={"rebalance_days": 5}),
            s19.SPEC.replace(portfolio={"ma_exit": 20}),
            s19.SPEC.replace(portfolio={"stop_loss": 0.08}),
            s19.SPEC.replace(signal={"mom_window": 60}),
            s19.SPEC.replace(signal={"w_momentum": 0.7, "w_flow": 0.3}),
        ]
        for spec in variants:
            m = freeze_manifest.build_manifest("x", spec, freeze_date="2026-08-15")
            self.assertNotEqual(h0, m["rules_sha256_16"],
                                f"改了 {spec.rules()} 卻沒改變 hash")

    def test_changing_config_param_changes_hash(self):
        h0 = freeze_manifest.build_manifest("x", freeze_date="2026-08-15")["rules_sha256_16"]
        with mock.patch.object(config, "BT_ORDER_SIZE_MODE", "regular_lot"):
            h1 = freeze_manifest.build_manifest(
                "x", freeze_date="2026-08-15")["rules_sha256_16"]
        with mock.patch.object(config, "EMBARGO_DAYS", 40):
            h2 = freeze_manifest.build_manifest(
                "x", freeze_date="2026-08-15")["rules_sha256_16"]
        self.assertNotEqual(h0, h1, "張數模式改了 hash 必須變")
        self.assertNotEqual(h0, h2, "embargo 改了 hash 必須變")

    def test_illegal_label_is_rejected(self):
        for bad in ["", "../evil", "has space", "a" * 70]:
            with self.assertRaises(ValueError):
                freeze_manifest.manifest_filename("2026-08-15", bad)


# ── 2. 凍結覆蓋率(反向 allowlist)────────────────────────────────────────
class FrozenCoverageTest(unittest.TestCase):
    LOAD_BEARING = (
        "SELF_ADJUST_PRICES", "ALLOW_UNADJUSTED_BACKTEST", "ALLOW_FUTURE_POOL",
        "BT_MODEL_LIMIT_LOCK", "BT_MODEL_DISPOSITION", "BT_ORDER_SIZE_MODE",
        "BT_PRICE_LIMIT_SOURCE", "BT_STALE_EXIT_DAYS", "BT_DELIST_RECOVERY",
        "BT_MIN_COMMISSION", "BT_INITIAL_CAPITAL", "BT_REGULAR_LOT_SHARES",
        "PRICE_INTEGRITY_RETURN_THRESHOLD", "PRICE_DATASET", "HISTORY_DAYS",
        "EVAL_SPLIT_MODE", "IS_OS_SPLIT", "IS_WEEKS", "OS_WEEKS", "EMBARGO_DAYS",
        "FACTOR_WEIGHTS", "DYNAMIC_UNIVERSE_MONTHLY_MIN_OBS",
        "BT_FEE", "BT_TAX", "MIN_AVG_VOLUME_LOTS",
    )

    def test_previously_missing_load_bearing_params_are_now_frozen(self):
        """上一版 FROZEN_KEYS 缺的那批必須都在 manifest 裡。"""
        cfg = freeze_manifest.build_manifest(
            "x", freeze_date="2026-08-15")["rules"]["config"]
        missing = [k for k in self.LOAD_BEARING if k not in cfg]
        self.assertEqual(missing, [], f"仍未凍結:{missing}")

    def test_every_config_param_is_either_frozen_or_explicitly_excluded(self):
        """反向規則:config 的每個大寫參數都必須被凍結,或明確寫進 NOT_FROZEN
        並附理由。手維護 allowlist 正是這次 bug 的根因。"""
        names = [k for k in vars(config) if k.isupper() and not k.startswith("_")]
        frozen = set(freeze_manifest.frozen_config_keys())
        for k in names:
            self.assertTrue(
                k in frozen or k in freeze_manifest.NOT_FROZEN,
                f"{k} 既沒被凍結也沒被分類 —— 這正是漏凍的路徑",
            )
        for k, reason in freeze_manifest.NOT_FROZEN.items():
            self.assertTrue(reason.strip(), f"{k} 的排除理由不可為空")

    def test_new_config_param_is_frozen_automatically(self):
        """新增 config 參數不需要有人記得加名單 —— 預設就被凍結。"""
        setattr(config, "BT_BRAND_NEW_KNOB", 0.42)
        try:
            cfg = freeze_manifest.frozen_config()
            self.assertEqual(cfg.get("BT_BRAND_NEW_KNOB"), 0.42)
        finally:
            delattr(config, "BT_BRAND_NEW_KNOB")

    def test_unserializable_new_param_fails_closed(self):
        """無法序列化又沒分類 → raise(不可靜默漏掉)。"""
        setattr(config, "BT_WEIRD_SET_KNOB", {1, 2, 3})
        try:
            with self.assertRaisesRegex(RuntimeError, "無法序列化"):
                freeze_manifest.frozen_config_keys()
        finally:
            delattr(config, "BT_WEIRD_SET_KNOB")

    def test_secret_is_never_written_into_manifest(self):
        m = freeze_manifest.build_manifest("x", freeze_date="2026-08-15")
        self.assertNotIn("FINMIND_TOKEN", m["rules"]["config"])

    def test_strategy_params_are_in_manifest_and_git_state_recorded(self):
        m = freeze_manifest.build_manifest("x", freeze_date="2026-08-15")
        strat = m["rules"]["strategy"]
        self.assertEqual(strat["name"], "s19_chip_momentum")
        self.assertEqual(strat["portfolio"],
                         {"ma_exit": 60, "max_positions": 10,
                          "rebalance_days": 20, "stop_loss": 0.15})
        self.assertEqual(strat["signal"]["mom_window"], 20)
        self.assertEqual(strat["signal"]["w_momentum"], 0.5)
        # git 狀態必須被記錄(dirty 工作樹代表這份凍結無法重現)
        for k in ("git_commit", "git_branch", "git_dirty", "git_dirty_file_count"):
            self.assertIn(k, m)


# ── 2.5 凍結的訊號參數確實 load-bearing(不是只進 hash 的裝飾)─────────────
class FrozenSignalParamsTest(unittest.TestCase):
    """六個 load-bearing 的凍結參數必須真的決定分數與投組行為。

    原本的破口(2026-08-15 審查用突變測試證明):`mom_window` / `flow_window` /
    `vol_window` / `w_momentum` / `w_flow` / `stop_loss` 六個參數**進得了 hash**,
    卻沒有任何測試證明 forward 真的用凍結值在跑。實測三個突變全套 340 測試零
    新增失敗:

      - `o.ts_ir(ret1, spec.sig("mom_window"))` → 讀模組常數 `SIGNAL_MOM_WINDOW`
      - `spec.sig("w_momentum")/spec.sig("w_flow")` → 讀 `W_MOMENTUM`/`W_FLOW`
      - `_apply_portfolio_config` 裡的
        `config.BT_TREND_STOP_LOSS = spec.port("stop_loss")` 整行刪掉

    也就是「凍結參數確實傳入 forward evaluator」這條 P0-1 回歸測試,對訊號那半
    與停損實質上沒有達成 —— 換成模組常數之後,forward 驗證的是「今天的參數」而
    不是「當時凍結的規則」,而 hash 仍然對得上。
    """

    PANEL = _signal_panel()
    # 每個訊號參數各給一個與預設(20/20/20/0.5/0.5)不同的值。
    VARIANTS = {
        "mom_window": 60,
        "flow_window": 60,
        "vol_window": 60,
        "w_momentum": 0.9,
        "w_flow": 0.9,
    }
    MODULE_CONSTANTS = {
        "SIGNAL_MOM_WINDOW": 3, "SIGNAL_FLOW_WINDOW": 3, "SIGNAL_VOL_WINDOW": 3,
        "W_MOMENTUM": 0.05, "W_FLOW": 0.95,
    }

    def test_every_frozen_signal_param_changes_the_score(self):
        """改凍結規格的任一訊號參數 → 分數序列必須不同。

        若 `build_signal` 改讀模組常數,換規格就完全不影響輸出,這裡會變紅。
        """
        base = s19.build_signal(self.PANEL)
        for key, value in self.VARIANTS.items():
            with self.subTest(param=key):
                spec = s19.SPEC.replace(signal={key: value})
                other = s19.build_signal(self.PANEL, spec=spec)
                self.assertTrue(
                    _series_differs(base, other),
                    f"凍結的 {key}={value} 沒有影響訊號 —— build_signal 沒在讀 spec")

    def test_frozen_spec_beats_the_module_constant_projection(self):
        """模組常數只是 SPEC 的投影:改常數不得改變 `build_signal` 的輸出。

        這是同一件事的反向斷言 —— 上一條證明「spec 有效」,這條證明
        「常數無效」,兩條合起來才排除「其實在讀常數」。
        """
        base = s19.build_signal(self.PANEL)
        for name, bogus in self.MODULE_CONSTANTS.items():
            with self.subTest(constant=name):
                with mock.patch.object(s19, name, bogus):
                    self.assertFalse(
                        _series_differs(base, s19.build_signal(self.PANEL)),
                        f"改模組常數 {name} 竟然改變了訊號 —— 那代表 build_signal "
                        "讀的是常數,forward 會用今天的參數驗證當時凍結的規則")

    def test_signal_fails_closed_when_a_frozen_param_is_missing(self):
        """規格缺參數時不得用預設值頂替(那就是「凍結不完整卻照跑」)。"""
        partial = StrategySpec(
            name=s19.SPEC.name,
            signal={k: v for k, v in s19.SPEC.signal.items()
                    if k != "vol_window"},
            portfolio=dict(s19.SPEC.portfolio),
        )
        with self.assertRaisesRegex(KeyError, "vol_window"):
            s19.build_signal(self.PANEL, spec=partial)


# ── 3. manifest 驗證:legacy / 不完整不得冒充 ─────────────────────────────
class ManifestValidationTest(unittest.TestCase):
    def test_fresh_manifest_is_reliable(self):
        m = freeze_manifest.build_manifest("x", freeze_date="2026-08-15",
                                           calendar=CAL)
        st = freeze_manifest.validate_manifest(m)
        self.assertTrue(st.ok, st.describe())
        self.assertIn("reliable", st.reliability)
        self.assertIsInstance(st.spec, StrategySpec)

    def test_legacy_schema_is_flagged_and_refused(self):
        st = freeze_manifest.validate_manifest(_legacy_manifest())
        self.assertFalse(st.ok)
        self.assertEqual(st.reliability, "incomplete_or_legacy")
        self.assertTrue(any("manifest_schema" in p for p in st.problems))

    def test_missing_required_config_key_fails_closed(self):
        m = freeze_manifest.build_manifest("x", freeze_date="2026-08-15")
        del m["rules"]["config"]["SELF_ADJUST_PRICES"]
        st = freeze_manifest.validate_manifest(m)
        self.assertFalse(st.ok)
        self.assertIn("SELF_ADJUST_PRICES", st.missing_config_keys)
        with self.assertRaisesRegex(ValueError, "拒絕套用"):
            freeze_manifest.apply_rules(m)

    def test_missing_strategy_param_fails_closed(self):
        """策略段少一個必要參數 → 不得用預設值頂替。"""
        m = freeze_manifest.build_manifest("x", freeze_date="2026-08-15")
        del m["rules"]["strategy"]["portfolio"]["max_positions"]
        m["rules_sha256_16"] = freeze_manifest.rules_hash(m["rules"])
        st = freeze_manifest.validate_manifest(m)
        self.assertFalse(st.ok)
        self.assertTrue(any("strategy" in p for p in st.problems), st.problems)

    def test_missing_strategy_section_fails_closed(self):
        m = freeze_manifest.build_manifest("x", freeze_date="2026-08-15")
        del m["rules"]["strategy"]
        st = freeze_manifest.validate_manifest(m)
        self.assertFalse(st.ok)

    def test_tampered_rules_are_detected(self):
        """manifest 是 immutable;事後改 rules 而不改 hash 必須被抓到。"""
        m = freeze_manifest.build_manifest("x", freeze_date="2026-08-15")
        m["rules"]["config"]["BT_TREND_STOP_LOSS"] = 0.30
        st = freeze_manifest.validate_manifest(m)
        self.assertFalse(st.ok)
        self.assertTrue(any("immutable" in p for p in st.problems), st.problems)

    def test_apply_rules_refuses_renamed_config_key(self):
        """config 已無此參數時不可靜默略過(舊版 `if hasattr` 就是這個 bug)。"""
        m = freeze_manifest.build_manifest("x", freeze_date="2026-08-15",
                                           calendar=CAL)
        m["rules"]["config"]["BT_PARAM_REMOVED_LONG_AGO"] = 1
        m["rules_sha256_16"] = freeze_manifest.rules_hash(m["rules"])
        with self.assertRaisesRegex(ValueError, "不存在"):
            freeze_manifest.apply_rules(m)

    def test_apply_rules_restores_frozen_values(self):
        m = freeze_manifest.build_manifest("x", freeze_date="2026-08-15",
                                           calendar=CAL)
        m["rules"]["config"]["BT_TREND_STOP_LOSS"] = 0.30
        m["rules_sha256_16"] = freeze_manifest.rules_hash(m["rules"])
        before = config.BT_TREND_STOP_LOSS
        try:
            spec = freeze_manifest.apply_rules(m)
            self.assertEqual(config.BT_TREND_STOP_LOSS, 0.30)
            self.assertEqual(spec.port("max_positions"), 10)
        finally:
            config.BT_TREND_STOP_LOSS = before

    def test_immutable_output_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(config, "OUTPUT_DIR", Path(tmp)):
                p1 = freeze_manifest.run("dup_label", calendar=CAL)
                self.assertIsNotNone(p1)
                body = p1.read_text(encoding="utf-8")
                p2 = freeze_manifest.run("dup_label", calendar=CAL)
                self.assertIsNone(p2, "同名 manifest 不可覆寫")
                self.assertEqual(body, p1.read_text(encoding="utf-8"))


# ── 4. forward:凍結參數、全相位、PIT、基準、不可覆寫 ─────────────────────
class ForwardTestPathTest(unittest.TestCase):
    FREEZE_DATE = "2026-02-02"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.panel = _panel()
        self.panel.attrs["universe_provider"] = _FakeProvider()
        self.calls: list = []
        # 每次進引擎時 config 長什麼樣。凍結的投組參數有一半是**副作用**
        # (`_apply_portfolio_config` 把 stop_loss / ma_exit / max_positions 寫進
        # config),不看 config 就完全驗不到它們有沒有被套用。
        self.cfg_seen: list = []

    def _write_manifest(self, spec=None, *, label="fwd",
                        calendar=CAL) -> Path:
        with mock.patch.object(config, "OUTPUT_DIR", self.out):
            m = freeze_manifest.build_manifest(label, spec,
                                               freeze_date=self.FREEZE_DATE,
                                               calendar=calendar)
            path = freeze_manifest.manifest_path(m)
            path.write_text(json.dumps(m, ensure_ascii=False, indent=2, default=str),
                            encoding="utf-8")
        return path

    def _run(self, manifest: Path, *, now=None, summary_factory=None,
             panel=None, baseline=None):
        from backtest import event_backtest
        import forward_test

        def fake_bt(**kwargs):
            self.calls.append(kwargs)
            self.cfg_seen.append({
                "BT_TREND_STOP_LOSS": config.BT_TREND_STOP_LOSS,
                "BT_MA_EXIT": config.BT_MA_EXIT,
                "BT_MAX_POSITIONS": config.BT_MAX_POSITIONS,
            })
            make = summary_factory or _fake_summary
            return {"summary": make(len(self.calls) - 1),
                    "trades": pd.DataFrame(), "equity_curve": pd.DataFrame()}

        patches = [
            mock.patch.object(config, "OUTPUT_DIR", self.out),
            mock.patch.object(s19, "build_panel",
                              return_value=(self.panel if panel is None else panel,
                                            ["A", "B", "C", "D"])),
            mock.patch.object(event_backtest, "backtest_portfolio",
                              side_effect=lambda **kw: fake_bt(**kw)),
        ]
        if baseline is not None:
            patches.append(mock.patch.object(s19, "equal_weight_baseline",
                                             return_value=baseline))
        with patches[0], patches[1], patches[2]:
            if baseline is not None:
                with patches[3]:
                    return forward_test.run(str(manifest), now=now)
            return forward_test.run(str(manifest), now=now)

    # 4.1 凍結的策略/投組參數確實被套用
    def test_frozen_20day_10position_params_reach_the_engine(self):
        """S19 的 20 日 / 10 檔必須傳進 forward evaluator。

        原本 forward 直接呼叫 `backtest_portfolio(**pit.backtest_kwargs())`,
        吃的是簽章預設 `rebalance_every=5 / top_n=3` —— 驗證的規則跟凍結的
        規則是兩套。
        """
        payload = self._run(self._write_manifest())
        self.assertTrue(self.calls)
        for kw in self.calls:
            self.assertEqual(kw["rebalance_every"], 20)
            self.assertEqual(kw["top_n"], 10)
        self.assertEqual(payload["strategy"]["portfolio"]["max_positions"], 10)

    def test_frozen_stop_loss_and_ma_exit_reach_the_engine_config(self):
        """凍結的 stop_loss / ma_exit / max_positions 必須在引擎跑的當下生效。

        這三個不是簽章參數,是 `_apply_portfolio_config()` 寫進**全域 config**
        的副作用 —— 只斷言 `rebalance_every` / `top_n` 完全驗不到它們。實測突變
        (刪掉 `config.BT_TREND_STOP_LOSS = spec.port("stop_loss")` 這一行)
        全套測試零新增失敗:凍結的停損進得了 hash,卻沒有任何測試證明 forward
        真的用它跑。
        """
        frozen = s19.SPEC.replace(portfolio={"stop_loss": 0.07, "ma_exit": 33,
                                             "max_positions": 6,
                                             "rebalance_days": 2})
        before = (config.BT_TREND_STOP_LOSS, config.BT_MA_EXIT,
                  config.BT_MAX_POSITIONS)
        # 前提:凍結值必須與現行 config 不同,否則斷言只是碰巧相等。
        self.assertNotEqual(before, (0.07, 33, 6))
        self._run(self._write_manifest(frozen, label="cfg"))
        self.assertEqual(len(self.cfg_seen), 2, "2 日再平衡 = 2 個相位")
        for seen in self.cfg_seen:
            self.assertAlmostEqual(seen["BT_TREND_STOP_LOSS"], 0.07)
            self.assertEqual(seen["BT_MA_EXIT"], 33)
            self.assertEqual(seen["BT_MAX_POSITIONS"], 6)
        # 跑完要還原(否則凍結參數會外洩到同一個 process 的其他研究)
        self.assertNotEqual(config.BT_TREND_STOP_LOSS, 0.07)
        self.assertNotEqual(config.BT_MA_EXIT, 33)

    def test_forward_uses_frozen_spec_not_current_module_default(self):
        """凍結值與現在的模組預設不同時,forward 必須用**凍結**的那個。"""
        frozen = s19.SPEC.replace(portfolio={"max_positions": 7,
                                             "rebalance_days": 4})
        self._run(self._write_manifest(frozen, label="frozen7"))
        self.assertTrue(self.calls)
        for kw in self.calls:
            self.assertEqual(kw["top_n"], 7)
            self.assertEqual(kw["rebalance_every"], 4)
        self.assertEqual(len(self.calls), 4, "相位數要跟著凍結的再平衡天數")

    # 4.2 跑滿所有相位
    def test_forward_runs_every_equivalent_phase(self):
        """forward 必須跑滿所有等價相位(20 個),不是只跑 phase 0。

        單相位的 Sharpe 實測可以從 -0.09 擺到 +1.09,只報一條路徑等於挑路徑。
        """
        payload = self._run(self._write_manifest())
        self.assertEqual(len(self.calls), 20)
        self.assertEqual(payload["phase_stats"]["n_phases"], 20)
        self.assertFalse(payload["phase_stats"]["single_phase_debug"])
        self.assertEqual(sorted(r["phase"] for r in payload["phases"]),
                         list(range(20)))
        # 相位確實位移了執行路徑(picks 起點不同)
        starts = {min(kw["picks_by_date"]) for kw in self.calls}
        self.assertGreater(len(starts), 1)
        # 中位數與最小值都要在輸出裡(不是只有最大值)
        for k in ("sharpe_median", "sharpe_min", "worst_max_drawdown"):
            self.assertIn(k, payload["phase_stats"])

    def test_forward_rejects_a_sweep_that_skips_phases(self):
        """策略只回傳 3 個相位、manifest 凍的是 20 → forward 必須 raise。

        原 bug(2026-08-15 修):`_assert_forward_integrity` 的 docstring 說相位
        是「在結果層面驗證」,但它只看 `single_phase_debug`,從沒比對過相位數;
        `PhaseSweep.full_sweep` 註明是「正式證據的必要條件」,forward 卻一次都
        沒引用。實測把 `evaluate_sweep` 換成只掃 3 個相位(旗標仍是 False),
        forward 完整跑完並寫出 payload,零警告 —— 等於相位數靠策略模組自律,
        而 `STRATEGY_PROTOCOL` 只檢查有沒有 `evaluate_sweep` 這個名字。
        """
        from evaluation.phases import sweep_phases

        def _row(ph: int) -> dict:
            return {"phase": ph, "sharpe": 1.0, "ann_ret": 0.1, "ann_vol": 0.1,
                    "max_drawdown": -0.1, "n_trades": 40, "win_rate": 0.5,
                    "payoff": 2.0, "days_beyond_last_pick": 0,
                    "candidate_pool_pit": True}

        def short_sweep(*_a, **_kw):
            return sweep_phases(_row, n_phases=3)   # 凍結的是 20

        manifest = self._write_manifest()
        with mock.patch.object(s19, "evaluate_sweep", side_effect=short_sweep):
            with self.assertRaisesRegex(RuntimeError, "相位數"):
                self._run(manifest)
        # 被擋下來的 forward 不得留下任何輸出(否則等於挑路徑挑到好看再留)
        self.assertEqual(list(self.out.glob("forward_test_*.json")), [])

    def test_forward_rejects_a_partial_sweep_even_with_right_phase_count(self):
        """宣稱 20 相位但只實跑 2 個 → `full_sweep` 為 False,一樣要 raise。"""
        from evaluation.phases import PhaseSweep

        rows = pd.DataFrame([{"phase": p, "sharpe": 1.0, "max_drawdown": -0.1,
                              "n_trades": 40, "days_beyond_last_pick": 0,
                              "candidate_pool_pit": True} for p in (0, 1)])
        partial = PhaseSweep(rows=rows, n_phases_full=20, phases_run=(0, 1))
        with mock.patch.object(s19, "evaluate_sweep", return_value=partial):
            with self.assertRaisesRegex(RuntimeError, "跑滿"):
                self._run(self._write_manifest())

    # 4.3 PIT provider
    def test_forward_passes_pit_provider_to_engine(self):
        payload = self._run(self._write_manifest())
        for kw in self.calls:
            self.assertIsInstance(kw["universe_provider"], _FakeProvider)
            self.assertIs(kw["sample"], False)
        self.assertTrue(all(r["candidate_pool_pit"] for r in payload["phases"]))

    def test_forward_fails_closed_without_provider(self):
        """策略 panel 沒帶 provider → 不得繼續(候選池無法證明是 PIT)。"""
        bare = self.panel.copy()
        bare.attrs.clear()
        with self.assertRaisesRegex(RuntimeError, "universe_provider"):
            self._run(self._write_manifest(), panel=bare)

    def test_forward_fails_closed_when_engine_reports_non_pit_pool(self):
        """任一相位的 summary 說候選池不是 PIT → 拒絕產出 forward 數字。"""
        with self.assertRaisesRegex(RuntimeError, "不是 PIT"):
            self._run(self._write_manifest(),
                      summary_factory=lambda n: _fake_summary(n, pit=False))

    def test_forward_fails_closed_on_eval_window_overflow(self):
        with self.assertRaisesRegex(RuntimeError, "溢出"):
            self._run(self._write_manifest(),
                      summary_factory=lambda n: _fake_summary(n, beyond=12))

    # 4.4 基準
    def test_forward_reports_benchmark(self):
        """必須和基準比,不是和零比(AGENTS.md 研究紀律)。"""
        payload = self._run(self._write_manifest())
        b = payload["benchmark_equal_weight_hold"]
        self.assertIn("sharpe", b)
        self.assertIn("ann_ret", b)
        self.assertIsNotNone(payload["excess_sharpe_vs_benchmark_median"])

    def test_forward_fails_closed_without_benchmark(self):
        with self.assertRaisesRegex(RuntimeError, "基準"):
            self._run(self._write_manifest(), baseline={})

    # 4.5 輸出不可覆寫
    def test_same_named_output_is_never_overwritten(self):
        """同名輸出不可覆寫:forward 紀錄是 append-only。

        原本每次重跑都覆寫 `forward_test_{freeze_date}.json` → 可以重跑到
        好看的那次再留下來。
        """
        manifest = self._write_manifest()
        t0 = datetime(2026, 8, 15, 10, 0, 0)
        first = self._run(manifest, now=t0)
        files = sorted(p.name for p in self.out.glob("forward_test_*.json"))
        self.assertEqual(len(files), 1)
        body = (self.out / files[0]).read_text(encoding="utf-8")

        with self.assertRaises(FileExistsError):
            self._run(manifest, now=t0)
        self.assertEqual(body, (self.out / files[0]).read_text(encoding="utf-8"))

        self._run(manifest, now=datetime(2026, 8, 15, 11, 0, 0))
        files2 = sorted(p.name for p in self.out.glob("forward_test_*.json"))
        self.assertEqual(len(files2), 2, "每一次 forward 都要留下自己的紀錄")
        ledger = (self.out / "forward_test_runs.jsonl").read_text(encoding="utf-8")
        self.assertEqual(len([ln for ln in ledger.splitlines() if ln.strip()]), 2)
        self.assertEqual(first["rules_sha256_16"],
                         json.loads(ledger.splitlines()[0])["rules_sha256_16"])

    # 4.6 legacy / 不完整 manifest
    def test_forward_refuses_legacy_manifest(self):
        """legacy manifest 不得冒充可靠凍結版本被 forward 使用。"""
        import forward_test
        path = self.out / "FROZEN_MANIFEST_2026-07-24_legacy.json"
        path.write_text(json.dumps(_legacy_manifest(), ensure_ascii=False),
                        encoding="utf-8")
        with mock.patch.object(config, "OUTPUT_DIR", self.out):
            with self.assertRaisesRegex(ValueError, "不是可靠的凍結版本"):
                forward_test.run(str(path))

    def test_forward_refuses_manifest_missing_load_bearing_param(self):
        import forward_test
        with mock.patch.object(config, "OUTPUT_DIR", self.out):
            m = freeze_manifest.build_manifest("gap", freeze_date=self.FREEZE_DATE,
                                               calendar=CAL)
            del m["rules"]["config"]["BT_ORDER_SIZE_MODE"]
            m["rules_sha256_16"] = freeze_manifest.rules_hash(m["rules"])
            path = freeze_manifest.manifest_path(m)
            path.write_text(json.dumps(m, ensure_ascii=False, default=str),
                            encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "不是可靠的凍結版本"):
                forward_test.run(str(path))

    def test_forward_records_provenance(self):
        payload = self._run(self._write_manifest())
        for k in ("rules_sha256_16", "freeze_git_commit", "run_git_commit",
                  "forward_start", "forward_end", "manifest_reliability",
                  "strategy", "evidence_note"):
            self.assertIn(k, payload)
        self.assertEqual(payload["forward_start"], "2026-02-03")


if __name__ == "__main__":
    unittest.main()
