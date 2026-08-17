# -*- coding: utf-8 -*-
"""假說策略的共用骨架。

把「怎麼變成合格 SignalFrame」抽出來,讓每支假說只需要回答一件事:
**這一天,哪一檔比較好?**(`score()`)。

四條共用規則:
  1. 分數一律用 `factor_engine.operators` 的因果算子算,不自己讀 cache 或網路。
  2. `score()` 收到的 `ops` **已經綁好排名母體**(見下),所以子類別裡的
     `ops.cs_rank(...)` 不可能排到當日不該進母體的股票上 —— 這件事被拿掉,
     不再是每支假說要自己記得的紀律。
  3. 輸出的 `rank` 只在當日 eligible universe 內重排成 1..N,所以**名次**的母體
     是「今天真的能買的股票」,與 `score()` 的排名母體是兩件事(見下)。
  4. 輸出 rank 用 `(score desc, stock_id asc)` 雙鍵固定 ties,否則同一份輸入
     重跑會因為 pandas 內部列順序而換掉第 10 名。

排名母體 `ranking_universe`(2026-08-16 修;原缺陷見下)
------------------------------------------------------
稠密 panel 為了 `ts_` 保留全部列,而正式路徑的 panel 是**所有月份候選池的聯集**
(實測 753 檔),不是任何一天真實存在的橫斷面。三個合法母體:

  `pool`      當日 `in_candidate_pool` —— 當月 PIT 候選池(約 300 檔)。**預設**。
              「相對於整個流動性池有多強」的水準資訊會保留下來,再從中挑可買的。
  `eligible`  當日這支策略真的能買的那些 —— `in_dynamic_universe`,若該策略開了
              `trend_guard` 則再 ∧ `trend_ok`(見下)。
  `panel`     全 panel(753 檔聯集)—— **只保留作對照**,不可作正式證據:
              這個母體是「整段回測期間曾經進過池」的聯集,用到了當時還不知道的
              未來資訊,而且會隨快照與回測區間漂移。

參數會進 `default_parameters()`,所以自動被凍結進 rules hash;母體換了而 hash
不變,forward 驗的就會是另一套規則。

趨勢閘門 `trend_guard`(2026-08-16 從硬編碼改成參數)
--------------------------------------------------
`trend_ok = (MA20>MA60) ∧ (MA60 斜率>0) ∧ (收盤>MA60)`,定義在
`factor_engine.legacy_factors`。它原本是 legacy 九因子選股器**自己的**一條規則,
在三層重構時被搬進這裡的 `_member_mask()`,於是每支假說都無條件繼承 —— 策略作者
沒有宣告過它,也沒有辦法關掉它。

那是把**策略規則裝進 universe 層**。兩個後果都實際發生了:

  1. 反向假說根本無法被測。「跌深反轉」按定義就是買在均線之下,大部分跌深標的
     過不了 `trend_ok`,連槽位都填不滿。假說被基礎設施改寫,而不是被證據否定。
  2. 它是一個**沒有人宣告過的看法**。三個條件都是落後指標,ANDed 之後下跌關門
     太慢、上漲開門太慢 —— 這個機制對不同假說的影響方向與大小都不同,所以
     不能由共用層替所有策略決定。

**預設仍是 `True`。** 不是因為它好,而是因為改預設值等於一次改掉所有既有策略的
定義;那該是逐支重測後的結論,不是這次重構的副作用。想關的策略自己宣告
`trend_guard = False`,它會進 rules hash,所以凍結過的規則不會被靜默換掉。

(這個閘門的量化影響已在本 repo 之外評估。公開這一層只記錄**機制與架構決定**,
不記錄任何策略的績效數字 —— 見 `STRATEGY_REGISTRY.md` 開頭的公開範圍說明。)

原缺陷(2026-08-16 稽核發現,現已修):`make_signals()` 曾先對整個稠密 panel 呼叫
`score()`,`ops.cs_rank(...)` 因此在含 86.7% 非成員的母體上排名,違反規格 §3.1。
影響**只**及於用**兩個以上 cs_ 算子做加權組合**的策略:單一 cs_rank 是同日單調
轉換,最終 rank 又會在 eligible 內重排,結果不變;兩個 cs_rank 相加時,非成員在
兩個因子的分布位置不同,會不對稱地扭曲組合順序(S19 同型缺陷實測 top10 有
**76.7%** 的日子會不同,H4 為 72.9%)。因此 H1／H2／H4 在此修正之前產生的數字
一律作廢重跑;H3(單一 cs_rank)不受影響。
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, Mapping, Optional

import numpy as np
import pandas as pd

import factor_engine.operators as op

#: 排名母體參數名。放模組層是為了讓凍結、測試與文件都指同一個字串。
RANKING_PARAM = "ranking_universe"

#: 趨勢閘門參數名。同上,而且它進 rules hash —— 關掉閘門 = 換了一套規則。
TREND_GUARD_PARAM = "trend_guard"


class HypothesisStrategy:
    """假說策略基底。子類別只需定義 name/version/參數與 `score()`。"""

    name: str = "unnamed_hypothesis"
    version: str = "0.1.0"
    evidence_status: str = "active_hypothesis_no_performance_claim"
    thesis: str = ""
    kill_criterion: str = ""

    required_columns: tuple = ("date", "stock_id", "close", "volume",
                               "in_dynamic_universe")
    defaults: Dict[str, Any] = {}
    bounds: Dict[str, tuple] = {}

    #: 合法的排名母體(語意見模組 docstring)。刻意不放進 `defaults`:子類別會
    #: 整個覆寫 `defaults`,放進去就會被一支忘了帶的假說靜默拿掉。
    RANKING_UNIVERSES: tuple = ("pool", "eligible", "panel")
    ranking_universe: str = "pool"

    #: 趨勢閘門開關(語意與證據見模組 docstring)。與 `ranking_universe` 同樣
    #: 刻意不放進 `defaults`,理由相同。預設 True = 維持既有策略的定義不變。
    trend_guard: bool = True

    # ── 契約 ──────────────────────────────────────────────────────────
    def data_requirements(self) -> Dict[str, Any]:
        return {
            "required_columns": list(self.required_columns),
            # `in_candidate_pool` 是 optional 而不是 required:只有
            # ranking_universe="pool" 才需要它,而那是參數不是欄位需求。缺了會在
            # `_ranking_scope()` fail-closed 並說出是哪個母體要的。
            "optional_columns": ["trend_ok", "foreign_net", "trust_net",
                                 "in_candidate_pool"],
            "warmup_bars": int(max([1, *[v for k, v in self.defaults.items()
                                         if k.endswith("_window")]])),
            "price_adjustment_requirement": "adjusted_total_return_compatible",
            "minimum_cross_section": 2,
        }

    def default_parameters(self) -> Dict[str, Any]:
        values = deepcopy(dict(self.defaults))
        values[RANKING_PARAM] = str(self.ranking_universe)
        values[TREND_GUARD_PARAM] = bool(self.trend_guard)
        return values

    def parameter_space(self) -> Dict[str, Any]:
        space = {k: {"type": "int" if isinstance(v, int) else "float",
                     "min": self.bounds.get(k, (None, None))[0],
                     "max": self.bounds.get(k, (None, None))[1]}
                 for k, v in self.defaults.items()}
        # categorical:GA 不能對它做數值變異,只能在列舉值之間跳。
        space[RANKING_PARAM] = {"type": "categorical",
                                "choices": list(self.RANKING_UNIVERSES)}
        space[TREND_GUARD_PARAM] = {"type": "categorical",
                                    "choices": [True, False]}
        return space

    # ── 子類別實作 ────────────────────────────────────────────────────
    def score(self, panel: pd.DataFrame, ops: op.PanelOps,
              params: Mapping[str, Any]) -> pd.Series:
        raise NotImplementedError

    # ── 共用流程 ──────────────────────────────────────────────────────
    def _normalized(self, params: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
        values = self.default_parameters()
        supplied = dict(params or {})
        unknown = sorted(set(supplied) - set(values))
        if unknown:
            raise ValueError(f"{self.name} 收到未知參數:{unknown}")
        values.update(supplied)
        scope = str(values.pop(RANKING_PARAM))
        if scope not in self.RANKING_UNIVERSES:
            raise ValueError(
                f"[fail-closed] {self.name}:未知的 {RANKING_PARAM}={scope!r};"
                f"只接受 {self.RANKING_UNIVERSES}(語意見 strategy_kit."
                "signal_builder 的模組 docstring 與規格 §3.1)")
        # 一定要在數值迴圈之前 pop:`isinstance(True, int)` 在 Python 是 True,
        # 留在 values 裡會被當成整數參數去查 `self.defaults[key]` 而 KeyError。
        guard = values.pop(TREND_GUARD_PARAM)
        if not isinstance(guard, (bool, np.bool_)):
            raise ValueError(
                f"[fail-closed] {self.name}:{TREND_GUARD_PARAM} 必須是 bool,"
                f"收到 {guard!r}({type(guard).__name__})。這個參數會進 rules "
                "hash,用 0/1 或 'false' 之類的等價值會讓同一套規則有兩個 hash")
        guard = bool(guard)
        for key, val in values.items():
            lo, hi = self.bounds.get(key, (None, None))
            num = int(val) if isinstance(self.defaults[key], int) else float(val)
            if lo is not None and num < lo:
                raise ValueError(f"{key}={num} 小於下界 {lo}")
            if hi is not None and num > hi:
                raise ValueError(f"{key}={num} 大於上界 {hi}")
            values[key] = num
        values[RANKING_PARAM] = scope
        values[TREND_GUARD_PARAM] = guard
        return values

    # ── 排名母體 ──────────────────────────────────────────────────────
    def _member_mask(self, work: pd.DataFrame, trend_guard: bool) -> pd.Series:
        """當日這支策略真的能買的股票。

        `trend_guard` 是**呼叫端傳進來的**,不是讀 class attr —— 它已經過
        `_normalized()` 並且會寫進 rules hash,從參數走才保證「跑的規則」與
        「hash 記的規則」是同一個。
        """
        mask = work["in_dynamic_universe"].fillna(False).astype(bool)
        if trend_guard:
            if "trend_ok" not in work.columns:
                raise ValueError(
                    f"[fail-closed] {self.name}:{TREND_GUARD_PARAM}=True 但 panel "
                    "沒有 trend_ok 欄。以前這裡是靜默略過 —— 那等於策略宣告了趨勢"
                    "閘門、實際卻沒套上,而 rules hash 仍記著 True。要不補上欄位,"
                    f"要不明確設 {TREND_GUARD_PARAM}=False")
            mask &= work["trend_ok"].fillna(True).astype(bool)
        return mask

    def _ranking_scope(self, work: pd.DataFrame, scope: str,
                       member: pd.Series) -> Optional[pd.Series]:
        """把 `ranking_universe` 解成給 `PanelOps` 的遮罩(`panel` 為 None)。

        兩道 fail-closed:
        1. 要 `pool` 卻沒有 `in_candidate_pool` 欄 → raise。缺欄位時退回全 panel
           等於靜默換掉母體,而換掉母體只會讓分數變成另一套,不會報錯。
        2. 有可買股票落在排名母體之外 → raise。那種列的 `cs_` 分數會是 NaN,
           接著被 `raw.notna()` 濾掉 —— 也就是**能買的股票被無聲丟掉**,而輸出
           看起來只是「那天入選的比較少」。
        """
        if scope == "panel":
            return None
        if scope == "pool":
            if "in_candidate_pool" not in work.columns:
                raise ValueError(
                    f"[fail-closed] {self.name}:{RANKING_PARAM}='pool' 需要 panel 帶"
                    " in_candidate_pool 欄(當月 PIT 候選池)。正式路徑由"
                    " universes.dynamic.add_membership(candidate_mask=...) 產生;"
                    "手寫 panel 請自行補上,不要改用 'panel' 母體繞過")
            mask = work["in_candidate_pool"].fillna(False).astype(bool)
        else:
            mask = member
        outside = int((member & ~mask).sum())
        if outside:
            raise ValueError(
                f"[fail-closed] {self.name}:有 {outside} 列可買股票落在"
                f" {RANKING_PARAM}={scope!r} 的排名母體之外。可買集合必須是排名母體的"
                "子集,否則那些股票會拿到 NaN 分數而被靜默排除出選股")
        return mask

    @staticmethod
    def _ctx(context: Any, key: str):
        if context is None:
            return None
        if isinstance(context, Mapping):
            return context.get(key)
        return getattr(context, key, None)

    def make_signals(self, panel: pd.DataFrame,
                     params: Optional[Mapping[str, Any]] = None,
                     context: Any = None) -> pd.DataFrame:
        if not isinstance(panel, pd.DataFrame) or panel.empty:
            raise ValueError(f"{self.name}:panel 為空")
        missing = [c for c in self.required_columns if c not in panel.columns]
        if missing:
            raise ValueError(f"{self.name}:panel 缺欄位 {missing}")

        work = panel.copy(deep=True)
        work["date"] = pd.to_datetime(work["date"])
        if work.duplicated(["date", "stock_id"]).any():
            raise ValueError(f"{self.name}:(date, stock_id) 必須唯一")
        work = work.sort_values(["stock_id", "date"]).reset_index(drop=True)

        values = self._normalized(params)
        scope = values[RANKING_PARAM]
        trend_guard = bool(values[TREND_GUARD_PARAM])
        member = self._member_mask(work, trend_guard)
        ranking_mask = self._ranking_scope(work, scope, member)

        # 母體綁在 ops 上,不是綁在紀律上:子類別的 score() 拿不到未 scope 的算子,
        # 所以「排到不該排的股票」這件事在這一層就不可能發生。
        ops = op.PanelOps(work["date"], work["stock_id"],
                          ranking_mask=ranking_mask, ranking_universe=scope)
        raw = self.score(work, ops, values)

        eligible = member & pd.Series(raw, index=work.index).notna()

        start = self._ctx(context, "start_date") or self._ctx(context, "start")
        end = self._ctx(context, "end_date") or self._ctx(context, "end")
        if start is not None:
            eligible &= work["date"] >= pd.Timestamp(start)
        if end is not None:
            eligible &= work["date"] <= pd.Timestamp(end)
        if not eligible.any():
            raise ValueError(f"{self.name}:窗內沒有任何 eligible 列")

        out = work.loc[eligible, ["date", "stock_id"]].copy()
        out["raw_score"] = pd.Series(raw, index=work.index).loc[eligible].astype(float).values
        out["alpha_score"] = out["raw_score"]
        out["eligible"] = True
        out = out.sort_values(["date", "raw_score", "stock_id"],
                              ascending=[True, False, True], kind="mergesort")
        out["rank"] = out.groupby("date", sort=False).cumcount() + 1
        # `ranking_universe_count` 是 §6.1 契約欄位,語意固定為「rank 的母體」=
        # 當日輸出的列數(validator 會檢查 rank ∈ 1..N 且與它一致),不可挪作他用。
        # `score_universe*` 才是 cs_ 算子的母體 —— 兩者是不同的東西,分開記,
        # 否則事後看不出當時的分數是在哪個母體上算的。
        out["ranking_universe_count"] = out.groupby("date")["stock_id"].transform("size")
        out["score_universe"] = scope
        score_pop = (pd.Series(True, index=work.index)
                     if ranking_mask is None else ranking_mask)
        per_day = work.loc[score_pop].groupby("date")["stock_id"].size()
        out["score_universe_count"] = (out["date"].map(per_day)
                                       .fillna(0).astype(int))
        out["rank_pct"] = ((out["ranking_universe_count"] - out["rank"] + 1)
                           / out["ranking_universe_count"])
        out["thesis_ok"] = True
        out["hard_exit"] = False
        out["reason_codes"] = f"{self.name}_eligible"
        # 規則變了就換 id:兩支 rule_id 相同、可買集合卻不同的訊號檔,事後無從分辨。
        out["eligibility_rule_id"] = ("dynamic_universe_and_trend_v1"
                                      if trend_guard else "dynamic_universe_v1")
        out["snapshot_complete"] = True
        out["strategy_id"] = self.name
        out["strategy_version"] = self.version
        return out.reset_index(drop=True)
