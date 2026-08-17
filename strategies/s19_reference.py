# -*- coding: utf-8 -*-
"""S19 的 ``make_signals`` 參考 adapter。

用途只有一個：提供一個不依賴 YAML、可重現、已知演算法來源的 Python strategy，
讓研究 runner 的黃金路徑能驗證：

    dense panel -> make_signals -> validator -> position policy -> event engine

這不是新 alpha，也不修正 S19 已知的 ranking-universe 問題。adapter 刻意重用
``strategies.s19_chip_momentum.build_signal``，先保存 mechanical parity；任何訊號
語意改善都必須另開 strategy version，不能在架構搬遷時偷偷改績效。
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, Mapping, Optional

import numpy as np
import pandas as pd

from strategies.s19_chip_momentum import SPEC, build_signal


class S19ReferenceStrategy:
    """供 golden-path 驗收的最小 Python-first strategy。

    目前回傳普通 ``DataFrame``，讓尚未完成的正式 ``SignalFrame`` 型別也能使用
    這個 fixture。runner 完成後必須由同一個 validator 驗證這份輸出，不能替
    reference strategy 開特例。
    """

    name = "s19_reference_make_signals"
    version = "1.0.0-mechanical-reference"
    evidence_status = "pipeline_fixture_no_performance_claim"

    _required_columns = (
        "date", "stock_id", "close", "volume", "foreign_net", "trust_net",
        "in_dynamic_universe", "trend_ok",
    )

    def data_requirements(self) -> Dict[str, Any]:
        """以可序列化形式宣告需求；正式 runner 可轉成 typed contract。"""
        return {
            "required_columns": list(self._required_columns),
            "optional_columns": ["name"],
            "warmup_bars": max(
                int(SPEC.sig("mom_window")),
                int(SPEC.sig("flow_window")),
                int(SPEC.sig("vol_window")),
            ),
            "price_adjustment_requirement": "adjusted_total_return_compatible",
            "minimum_cross_section": 1,
        }

    def default_parameters(self) -> Dict[str, Any]:
        return deepcopy(dict(SPEC.signal))

    def parameter_space(self) -> Dict[str, Any]:
        """P1 只描述參數型別，不啟動 grid／GA。"""
        return {
            "mom_window": {"type": "int", "min": 2},
            "flow_window": {"type": "int", "min": 2},
            "vol_window": {"type": "int", "min": 2},
            "w_momentum": {"type": "float", "min": 0.0, "max": 1.0},
            "w_flow": {"type": "float", "min": 0.0, "max": 1.0},
        }

    @staticmethod
    def _context_value(context: Any, key: str) -> Optional[Any]:
        if context is None:
            return None
        if isinstance(context, Mapping):
            return context.get(key)
        return getattr(context, key, None)

    def _normalized_parameters(
        self, params: Optional[Mapping[str, Any]],
    ) -> Dict[str, Any]:
        values = self.default_parameters()
        supplied = dict(params or {})
        unknown = sorted(set(supplied) - set(values))
        if unknown:
            raise ValueError(f"S19 reference 收到未知 signal params: {unknown}")
        values.update(supplied)
        for key in ("mom_window", "flow_window", "vol_window"):
            if int(values[key]) < 2:
                raise ValueError(f"{key} 必須 >= 2")
            values[key] = int(values[key])
        for key in ("w_momentum", "w_flow"):
            values[key] = float(values[key])
            if not 0.0 <= values[key] <= 1.0:
                raise ValueError(f"{key} 必須落在 [0, 1]")
        if not np.isclose(values["w_momentum"] + values["w_flow"], 1.0):
            raise ValueError("w_momentum + w_flow 必須等於 1")
        return values

    def make_signals(
        self,
        panel: pd.DataFrame,
        params: Optional[Mapping[str, Any]] = None,
        context: Any = None,
    ) -> pd.DataFrame:
        """產生日頻、完整且 deterministic 的 eligible ranking snapshots。

        日頻輸出是刻意的：正式週頻 runner 必須從日頻候選訊號選出五個等價 weekly
        phases；若 strategy 自己先降成單一星期幾，就已經把執行相位偷選掉了。
        """
        if not isinstance(panel, pd.DataFrame):
            raise TypeError("panel 必須是 pandas.DataFrame")
        missing = [c for c in self._required_columns if c not in panel.columns]
        if missing:
            raise ValueError(f"S19 reference panel 缺必要欄位: {missing}")
        if panel.empty:
            raise ValueError("S19 reference 不接受空 panel")

        work = panel.copy(deep=True)
        work["date"] = pd.to_datetime(work["date"])
        if work.duplicated(["date", "stock_id"]).any():
            raise ValueError("panel 的 (date, stock_id) 必須唯一")

        start = self._context_value(context, "start")
        end = self._context_value(context, "end")
        if start is None:
            start = self._context_value(context, "start_date")
        if end is None:
            end = self._context_value(context, "end_date")

        normalized = self._normalized_parameters(params)
        score_spec = SPEC.replace(signal=normalized)
        score = build_signal(work, spec=score_spec)

        eligible = (
            work["in_dynamic_universe"].fillna(False).astype(bool)
            & work["trend_ok"].fillna(False).astype(bool)
            & score.notna()
        )
        if start is not None:
            eligible &= work["date"] >= pd.Timestamp(start)
        if end is not None:
            eligible &= work["date"] <= pd.Timestamp(end)

        out = work.loc[eligible, ["date", "stock_id"]].copy()
        out["raw_score"] = score.loc[eligible].astype(float).to_numpy()
        out["alpha_score"] = out["raw_score"]
        out["eligible"] = True
        # 先用 mergesort 固定 ties，再用 stock_id 作明確第二鍵；同輸入重跑不得因
        # pandas 內部列順序不同而換掉第 10 名。
        out = out.sort_values(
            ["date", "raw_score", "stock_id"],
            ascending=[True, False, True], kind="mergesort",
        )
        out["rank"] = out.groupby("date", sort=False).cumcount() + 1
        out["ranking_universe_count"] = out.groupby("date")["stock_id"].transform("size")
        out["rank_pct"] = (
            out["ranking_universe_count"] - out["rank"] + 1
        ) / out["ranking_universe_count"]
        out["thesis_ok"] = True
        out["hard_exit"] = False
        out["reason_codes"] = "s19_reference_eligible"
        out["eligibility_rule_id"] = "s19_dynamic_universe_and_trend_v1"
        out["snapshot_complete"] = True
        out["strategy_id"] = self.name
        out["strategy_version"] = self.version
        return out.reset_index(drop=True)
