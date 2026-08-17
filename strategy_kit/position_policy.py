# -*- coding: utf-8 -*-
"""StrategyPositionPolicy v1 — 單策略的持有、退出與資金槽決策層。

為什麼要多這一層
----------------
在這一層出現以前,「今天可以買誰」(訊號)與「今天實際要變成什麼部位」(決策)
是同一件事:引擎拿 `picks_by_date` 的前 N 名,有空位就買、`_check_exit` 說出就出。
於是三件會造成假績效的事沒有任何地方擋得住:

1. **退出理由不可稽核**。`exit_reason` 只有引擎內建的那幾種(ma_exit / stop_loss /
   max_hold),策略自己的「排名掉出去了」無處可放,事後沒有人分得出「這筆虧損是
   風控停損還是排名衰退」——而這兩者要用完全不同的方式改進。
2. **desired 與 realized 混為一談**。跌停賣不掉、現金不夠、處置禁新倉時,舊路徑
   只是「跳過」,沒有任何紀錄。於是回測看起來永遠是「想買就買到」,而真正會吃掉
   報酬的成交摩擦一筆都看不見。
3. **資金情境被寫進全域 config**。100 萬研究情境與 50 萬個人情境要用同一組規則
   重跑時,舊做法是就地改 `config.BT_INITIAL_CAPITAL`,兩次執行會互相污染。

因此這一層只負責 **desired state**(要變成什麼樣子),完全不猜成交:它不知道
價格能不能成交、現金夠不夠、股數要不要湊整張。那些一律留在事件引擎,重用既有的
台股成本、tick、漲跌停、處置與整張/零股規則(規格 §8:不得在 policy 內另做一套
execution)。

v1 刻意凍結的語意(見 STRATEGY_POSITION_POLICY_SPEC.md §3、§4)
--------------------------------------------------------------
* 進場 rank<=10、續抱 rank<=20,排名退出只在**每週決策日**發生。
* hard stop 用**收盤確認**,下一個交易時點才嘗試退出。**不得**用日內 low 假裝
  已在理論停損價成交 —— 手動投資人沒有掛在那裡的單,那是回測獨有的樂觀偏誤。
* 固定 10 個 10% 資金槽;候選不足就保留現金,不把剩下的放大到滿倉。
* 權重漂移不交易:只有進出場、regime 層級改變或單檔超過 15% cap 才 resize。
* rank 沒有預期報酬尺度,所以 `raw_score` 高不代表配更多錢。

這一層**不**做:GA 調參、signal decay、take profit、多策略配置、regime 公式。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

import numpy as np
import pandas as pd

from evaluation.holdout import rules_fingerprint

# 退出理由的優先序(規格 §3.4)。同一天可能同時成立多個觸發條件,但**主要**
# reason_code 只能有一個,否則 exit 統計會重複計數、也沒辦法回答「這批虧損主要
# 是被什麼規則賣掉的」。次要觸發原因另存 `secondary_reasons`,不丟掉資訊。
EXIT_PRIORITY: tuple = (
    "forced_exit",      # 失去合法交易資格 / 下市 / stale
    "risk_stop",        # close-confirmed hard stop
    "regime_reduce",    # 外部 PIT regime 要求降曝險
    "thesis_break",     # 策略自己宣告論點破壞
    "rank_decay",       # 排名掉出 hold buffer
    "not_ranked",       # 完整 snapshot 下已不在 eligible/ranking universe
    "max_hold",         # 殭屍部位時間上限
)

# 進場/續抱/調整的理由碼。`new_top_k` 與 `rank_decay` 是「一般排名換股」的標記,
# 非決策日**絕對不能**出現(contract test 直接釘住這兩個字串)。
REASON_NEW_TOP_K = "new_top_k"
REASON_HOLD = "within_hold_buffer"
REASON_HOLD_OFF_DAY = "off_decision_day"
REASON_CAP = "concentration_cap"
REASON_STOP_BREACHED_EARLIER = "stop_breached_earlier_exit_pending"
# 觸發過累積災難停損的標的,在「完整快照中掉出 top exit_rank」之前不得買回。
# 沒有這條鎖,一檔剛因為 -20% 累積虧損被賣掉的股票,只要下一個決策日還在
# top 10 就會立刻被買回來 —— 停損等於沒發生,只是多付一次來回成本。
REASON_STOP_NOT_REARMED = "catastrophic_stop_not_rearmed"

VALID_REGIMES: tuple = ("risk_on", "caution", "risk_off")

# 台股普通股單日漲跌幅上限。這個常數在這裡的用途**不是**算合法價(那在
# execution/taiwan_rules.py),而是判斷「今天有沒有可能是 hard stop 的跨越日」,
# 見下方 `_hard_stop_state` 的說明。
DAILY_PRICE_LIMIT_PCT = 0.10

_WEIGHT_TOL = 1e-9


@dataclass(frozen=True)
class StrategyPositionPolicySpec:
    """v1 凍結的 policy 規則。

    frozen 是刻意的:這些值每一個都會改變績效,所以它們必須整組進 rules hash
    (規格 §6 的最後一條 fail-closed:規則變了 hash 一定要變)。可變的執行期
    狀態一律放在 `StrategyPositionPolicy._state`,不得回寫 spec。
    """

    decision_frequency: str = "weekly"
    entry_rank: int = 10
    exit_rank: int = 20
    max_slots: int = 10
    slot_weight: float = 0.10
    single_name_cap: float = 0.15
    # v1.1(2026-08-16 owner 決議):**累積災難損失上限**,不是單日跌幅。
    # 語意是「相對實際進場成本的累積收盤報酬」:單日 -8% 或吃一根跌停都不構成
    # 退出,累積跌到 -20% 才形成 T+1 退出意圖。owner 明確否決了原本 0.08 的
    # 「單日跌一根就走」語意 —— 那在台股會把正常波動當成災難。
    hard_stop_pct: float = 0.20
    max_hold_days: int = 120
    risk_on_slots: int = 10
    caution_slots: int = 5
    risk_off_slots: int = 0

    def __post_init__(self) -> None:
        # 全部 fail-closed:一個矛盾的規則組合(例如 entry_rank == exit_rank)
        # 不會 crash,只會安靜地產生一套「每週把剛買的股票賣掉」的績效。
        if self.decision_frequency not in ("weekly", "daily"):
            raise ValueError(
                f"decision_frequency 只支援 weekly/daily,目前為 {self.decision_frequency!r}")
        if int(self.entry_rank) < 1:
            raise ValueError("entry_rank 必須 >= 1(rank 由 1 開始)")
        if int(self.exit_rank) <= int(self.entry_rank):
            raise ValueError(
                f"exit_rank({self.exit_rank}) 必須嚴格大於 entry_rank({self.entry_rank});"
                "相等等於沒有 hold buffer,排名微幅震盪就會每週來回換股")
        if int(self.max_slots) < 1:
            raise ValueError("max_slots 必須 >= 1")
        if not 0.0 < float(self.slot_weight) <= 1.0:
            raise ValueError("slot_weight 必須落在 (0, 1]")
        if float(self.single_name_cap) < float(self.slot_weight):
            raise ValueError(
                f"single_name_cap({self.single_name_cap}) 不得小於 slot_weight"
                f"({self.slot_weight}):新進場當下就違反上限,等於每次買完立刻要 resize")
        if float(self.single_name_cap) > 1.0:
            raise ValueError("single_name_cap 不得大於 1.0(long-only 不加槓桿)")
        if int(self.max_slots) * float(self.slot_weight) > 1.0 + _WEIGHT_TOL:
            raise ValueError(
                f"max_slots × slot_weight = {self.max_slots * self.slot_weight:.4f} > 1;"
                "long-only 不得靠槽位設定隱性加槓桿")
        if float(self.hard_stop_pct) <= 0.0 or float(self.hard_stop_pct) >= 1.0:
            raise ValueError("hard_stop_pct 必須落在 (0, 1)")
        if int(self.max_hold_days) < 1:
            raise ValueError("max_hold_days 必須 >= 1")
        if not 0 <= int(self.risk_on_slots) <= int(self.max_slots):
            raise ValueError(
                f"risk_on_slots({self.risk_on_slots}) 必須在 [0, max_slots={self.max_slots}]")
        if not 0 <= int(self.caution_slots) < int(self.risk_on_slots):
            raise ValueError(
                f"caution_slots({self.caution_slots}) 必須嚴格小於 "
                f"risk_on_slots({self.risk_on_slots}):caution 不降曝險就不是 caution")
        if not 0 <= int(self.risk_off_slots) <= int(self.caution_slots):
            raise ValueError(
                f"risk_off_slots({self.risk_off_slots}) 必須在 [0, caution_slots="
                f"{self.caution_slots}]")

    def rules(self) -> Dict[str, Any]:
        """回傳**新的** dict(每次呼叫都是新物件)。

        故意不快取:呼叫端(summary、rules hash、台帳)拿到的若是同一個 dict,
        任何一處就地改一個 key,其他所有地方的 provenance 都會被靜默改寫,
        而那正是 hash 存在的意義。
        """
        return {
            "decision_frequency": str(self.decision_frequency),
            "entry_rank": int(self.entry_rank),
            "exit_rank": int(self.exit_rank),
            "max_slots": int(self.max_slots),
            "slot_weight": float(self.slot_weight),
            "single_name_cap": float(self.single_name_cap),
            "hard_stop_pct": float(self.hard_stop_pct),
            "max_hold_days": int(self.max_hold_days),
            "risk_on_slots": int(self.risk_on_slots),
            "caution_slots": int(self.caution_slots),
            "risk_off_slots": int(self.risk_off_slots),
        }

    def slots_for_regime(self, regime: str) -> int:
        table = {
            "risk_on": int(self.risk_on_slots),
            "caution": int(self.caution_slots),
            "risk_off": int(self.risk_off_slots),
        }
        if regime not in table:
            raise ValueError(
                f"未知 regime {regime!r};只接受 {VALID_REGIMES}。"
                "regime 分類公式不屬於本層,但未知值不得當成 risk_on 放行")
        return min(table[regime], int(self.max_slots))


@dataclass(frozen=True)
class RegimeProvenance:
    """外部 market-regime 的 PIT 出處(規格 §4.3)。

    為什麼 policy 需要這個物件
    --------------------------
    §4.3 明文:「policy 只接受**已帶 PIT provenance** 的 regime」、「regime 必須有
    hysteresis／來源時間戳」、「不得用今天資料回寫歷史 regime」。但介面上傳進來的
    只是 `"risk_on"` 這種裸字串,而字串沒有辦法自證任何一條。實測後果:拿今天的
    大盤走勢去標歷史每一天的 regime(risk_off 那幾週剛好避開崩盤),回測會照跑,
    summary 還會寫 `regime_pit_provenance: True` —— 因為舊版那一格只是
    `bool(regime_by_date)`,「有傳東西」被當成「有 provenance」。

    這一層**不**做 regime 的判定演算法(§8 明文那是另一份規格)。它只要求:
    沒有 provenance 就不能假裝有。缺這個物件時結果標 `unverified`,
    且不得標成 formal-evidence-eligible。

    欄位:
      `source`     — 誰算的(模組/規則名),事後要能找回同一份計算。
      `as_of`      — 這個 regime 標籤所用資料的截止時間。必須 <= 決策日,
                     否則就是用未來資料回寫歷史。
      `hysteresis` — 遲滯設定的描述或指紋。沒有遲滯的 regime 會在門檻附近
                     每天翻面,那種「regime」只是雜訊的另一個名字。
    """

    source: str
    as_of: pd.Timestamp
    hysteresis: str

    def __post_init__(self) -> None:
        for name in ("source", "hysteresis"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"RegimeProvenance.{name} 必須是非空字串;"
                    "空白的來源等於沒有 provenance,不得放行")
        object.__setattr__(self, "source", str(self.source).strip())
        object.__setattr__(self, "hysteresis", str(self.hysteresis).strip())
        stamp = pd.Timestamp(self.as_of)
        if pd.isna(stamp):
            raise ValueError("RegimeProvenance.as_of 必須是可解析的時間戳")
        object.__setattr__(self, "as_of", stamp)

    def rules(self) -> Dict[str, Any]:
        return {"source": self.source, "as_of": str(self.as_of),
                "hysteresis": self.hysteresis}


@dataclass(frozen=True)
class RegimeState:
    """一個 regime 標籤 + 它的出處(可能沒有)。

    `provenance is None` = 裸字串 = **unverified**:policy 照樣照它調整 slots
    (擋掉整條路徑不是本次的任務),但結果必須標記,且不得作為正式證據。
    """

    label: str
    provenance: Optional[RegimeProvenance] = None

    @property
    def verified(self) -> bool:
        return self.provenance is not None

    def rules(self) -> Dict[str, Any]:
        return {
            "label": self.label,
            "verified": self.verified,
            "provenance": (self.provenance.rules()
                           if self.provenance is not None else None),
        }


def normalize_regime(regime) -> RegimeState:
    """把 `str` 或 `RegimeState` 正規化成 `RegimeState`。

    裸字串**不會**被拒絕(v1 還沒有 regime 判定規格,拒絕等於讓 policy 無法使用),
    但它一定是 `verified=False`。要 verified 就得自己帶 `RegimeProvenance`。
    """
    if isinstance(regime, RegimeState):
        return regime
    if isinstance(regime, RegimeProvenance):
        raise TypeError("regime 需要 RegimeState(label + provenance),不是單獨的 "
                        "RegimeProvenance")
    return RegimeState(label=str(regime), provenance=None)


@dataclass(frozen=True)
class StrategyPositionDecision:
    """一次 policy snapshot 的完整 desired state。

    刻意保留 `as_of` 與 `earliest_execution`:T 日收盤形成的決策最早只能在 T+1
    成交,這個限制若只存在引擎裡,decision_log 事後就無法自證沒有前視。
    """

    as_of: pd.Timestamp
    actions: pd.DataFrame
    targets: pd.DataFrame
    target_cash_weight: float
    snapshot_complete: bool
    policy_rules: Dict[str, Any]
    regime: str
    is_decision_day: bool
    equity: float
    available_slots: int
    earliest_execution: pd.Timestamp
    fingerprint: str
    # regime 的出處(規格 §4.3)。`regime_verified=False` = 呼叫端給的是裸字串,
    # 沒有來源/as-of/hysteresis 可查 —— 結果只能標 unverified,不得作正式證據。
    regime_verified: bool = False
    regime_provenance: Optional[Dict[str, Any]] = None

    def exits(self) -> Dict[str, str]:
        """`{stock_id -> 主要 reason_code}`,供引擎排退出順序。"""
        if self.actions.empty:
            return {}
        rows = self.actions[self.actions["action"] == "exit"]
        return {str(r.stock_id): str(r.reason_code) for r in rows.itertuples()}

    def target_map(self) -> Dict[str, float]:
        if self.targets.empty:
            return {}
        return {str(r.stock_id): float(r.target_weight)
                for r in self.targets.itertuples()}

    def notional_map(self) -> Dict[str, float]:
        if self.targets.empty:
            return {}
        return {str(r.stock_id): float(r.target_notional)
                for r in self.targets.itertuples()}


def _as_frame(obj, columns: Sequence[str]) -> pd.DataFrame:
    """把 DataFrame / list-of-mapping / None 正規化成 DataFrame。"""
    if obj is None:
        return pd.DataFrame(columns=list(columns))
    if isinstance(obj, pd.DataFrame):
        return obj.copy()
    frame = pd.DataFrame(obj)
    if frame.empty:
        return pd.DataFrame(columns=list(columns))
    return frame


def _empty_signal_frame() -> pd.DataFrame:
    return pd.DataFrame({
        "stock_id": pd.Series([], dtype=object),
        "rank": pd.Series([], dtype=float),
        "raw_score": pd.Series([], dtype=float),
    })


def _normalize_signals(signals, as_of: pd.Timestamp):
    """回傳 `(sig_frame, snapshot_complete, snapshot_date)`。

    `snapshot_date` 是這份快照實際的日期(`None` = frame 沒帶 `date` 欄,
    也就是呼叫端自己保證「這就是今天的快照」)。它必須傳出來,因為
    「截至 as_of 的最新快照」可能是**好幾天前**的:那種 stale 快照可以拿來
    維持部位,但不足以當成「這檔已經掉出 top 20」的證據去解除停損鎖定。

    這裡有三刀,每一刀都擋掉一種曾經真的發生、而且不會報錯的假績效:

    1. **未來訊號不得改變過去決策**:若 signals 帶 `date` 欄,只保留
       `date <= as_of` 的列。沒有這一刀,把整段歷史訊號表直接丟進來就會用未來的
       排名決定過去的部位。
    2. **只採用截至 as_of 的最新那一個快照日,不跨快照日合併 rank**
       (規格 §9B.2)。舊版是
       `sort_values("_asof").drop_duplicates("stock_id", keep="last")`,取的是
       **每檔各自的最新列**:一檔在最新快照裡已經掉出榜外(整列消失),卻會沿用
       它上一個快照的 rank 繼續被當成有效訊號 —— 既可能被當成 top-10 買進,也會
       因為「還在名單裡」而躲掉 `not_ranked`。輸出裡完全看不出那個 rank 是舊的。
    3. **完整性未知一律當成不完整**(`snapshot_complete=False`,規格 §9B.1)。
       舊版預設 True,於是「持有 B、今天訊號只有 A、frame 沒帶旗標」會把 B 判成
       `exit / not_ranked` 賣掉 —— 用「我沒看到它」當成「它已經掉出母體」的證據。
       缺旗標時正確語意是 unknown,不可自動賣。空表、或截至 as_of 沒有任何有效
       快照時同理:v1 沒有獨立於資料列之外的完整性 metadata 通道,拿不到旗標就是
       不知道,一律 False。
    """
    frame = _as_frame(signals, ("stock_id", "rank", "raw_score", "eligible"))
    if frame.empty:
        return (_empty_signal_frame(), False, None)
    if "stock_id" not in frame.columns or "rank" not in frame.columns:
        raise ValueError("signals 至少要有 stock_id 與 rank 欄")

    snapshot_date: Optional[pd.Timestamp] = None
    if "date" in frame.columns:
        dates = pd.to_datetime(frame["date"])
        keep = dates <= as_of
        if not bool(keep.any()):
            return (_empty_signal_frame(), False, None)
        frame = frame[keep].copy()
        asof_dates = dates[keep]
        # 「截至 as_of 的最新一個快照日」= 這一天的**全部**列,其他快照日一列都不用。
        snapshot_date = pd.Timestamp(asof_dates.max())
        frame = frame[asof_dates == asof_dates.max()].copy()

    snapshot_complete = False
    if "snapshot_complete" in frame.columns:
        snapshot_complete = bool(
            frame["snapshot_complete"].fillna(False).astype(bool).all())

    if "eligible" in frame.columns:
        frame = frame[frame["eligible"].fillna(False).astype(bool)]

    out = pd.DataFrame({
        "stock_id": frame["stock_id"].astype(str).values,
        "rank": pd.to_numeric(frame["rank"], errors="coerce").values,
        "raw_score": (pd.to_numeric(frame["raw_score"], errors="coerce").values
                      if "raw_score" in frame.columns else np.nan),
    })
    out = out[out["rank"].notna()]
    # 同一個快照日同一檔兩個 rank → raise。舊版靠 drop_duplicates 靜默留下最後一列,
    # 於是決策取決於列順序;現在不再跨快照日合併,同一天的重複只可能是上游算錯,
    # 放行的話還會讓同一檔佔掉兩個資金槽(規格 §9A.3 對引擎的同一條 fail-closed)。
    dupes = sorted(set(out.loc[out["stock_id"].duplicated(), "stock_id"]))
    if dupes:
        raise ValueError(
            f"[fail-closed] 同一個快照日有重複的 stock_id {dupes};"
            "同一檔兩個 rank 會讓決策取決於列順序")
    # 同 rank 時用 stock_id 當第二鍵:決策必須 deterministic,否則同一份輸入
    # 重跑會挑到不同的股票,任何 parity 或 forward 比對都失去意義。
    out = out.sort_values(["rank", "stock_id"], kind="mergesort").reset_index(drop=True)
    return out, snapshot_complete, snapshot_date


def _normalize_holdings(holdings) -> pd.DataFrame:
    cols = ("stock_id", "weight", "entry_price", "close", "holding_days")
    frame = _as_frame(holdings, cols)
    if frame.empty:
        return pd.DataFrame({
            "stock_id": pd.Series([], dtype=object),
            "weight": pd.Series([], dtype=float),
            "entry_price": pd.Series([], dtype=float),
            "close": pd.Series([], dtype=float),
            "holding_days": pd.Series([], dtype=float),
            "forced_exit": pd.Series([], dtype=bool),
            "thesis_break": pd.Series([], dtype=bool),
        })
    missing = [c for c in cols if c not in frame.columns]
    if missing:
        raise ValueError(f"holdings 缺欄位 {missing}(至少要有 {list(cols)})")
    out = pd.DataFrame({
        "stock_id": frame["stock_id"].astype(str).values,
        "weight": pd.to_numeric(frame["weight"], errors="coerce").fillna(0.0).values,
        "entry_price": pd.to_numeric(frame["entry_price"], errors="coerce").values,
        "close": pd.to_numeric(frame["close"], errors="coerce").values,
        "holding_days": pd.to_numeric(frame["holding_days"], errors="coerce")
                          .fillna(0).values,
    })
    for flag in ("forced_exit", "thesis_break"):
        out[flag] = (frame[flag].fillna(False).astype(bool).values
                     if flag in frame.columns else False)
    # `exit_pending` = 引擎手上「已經送出但還沒成交」的退出意圖(跌停賣不掉、
    # 停牌…)。缺這一欄時預設 **False**(規格 §9A.1,2026-08-15 由 True 改回)。
    #
    # 為什麼不是 True:`snapshot_complete` 與 `exit_pending` 的「安全方向」相反。
    # 前者缺值代表「不知道這檔還在不在母體」,保守解讀是**不賣**;後者缺值若當成
    # True,代表「假設退出意圖早就送過了」→ 今天不再產生 `risk_stop`,那是**漏掉
    # 停損**的方向。fail-closed 的定義是「資訊不足時不得放過風險控制」,不是
    # 「資訊不足時一律不動作」。§5 的最小 holdings 契約又不含這一欄,所以照契約
    # 呼叫的人在舊預設下會拿到「跌 30% 也只回 hold、一筆退出意圖都沒有」。
    # 代價是重複產生退出意圖(同一次停損被記成多天),但那是**看得見**的重複;
    # 漏掉的停損在任何輸出裡都看不見。
    #
    # `exit_pending_known` 仍然保留:重複的 risk_stop 有沒有可能是這個預設造成的,
    # 事後要分得出來(規格 §7 的可重建要求)。事件引擎一律顯式提供這一欄,因此
    # 正式回測路徑永遠是 known=True。
    has_exit_pending = "exit_pending" in frame.columns
    out["exit_pending"] = (
        frame["exit_pending"].fillna(False).astype(bool).values
        if has_exit_pending else False)
    out["exit_pending_known"] = bool(has_exit_pending)
    # `intraday_low` 之類的日內欄位刻意**不讀**:v1 的 hard stop 是收盤確認,
    # 用日內最低價觸發等於假設手動投資人剛好掛在理論停損價並成交(規格 §3.4)。
    return out


def _hard_stop_state(entry_price: float, close: float, hard_stop_pct: float,
                     exit_pending: bool) -> str:
    """回傳 `"none"` / `"fresh_cross"` / `"breached_earlier"`。

    為什麼不是單純的 `close/entry - 1 <= -hard_stop_pct`
    ------------------------------------------------------
    hard stop 是**收盤確認**的:它在「收盤第一次跌破停損價」的那一天成立,隔天
    才嘗試成交。policy 本身無狀態,每天都會重看同一批持股,所以必須有辦法分辨
    「今天才跌破」與「早就跌破、退出意圖已經在路上」——否則同一次停損會被重複
    當成新事件,exit 統計會膨脹,而且會蓋掉真正的原因(例如那檔其實是跌停賣不掉)。

    台股單日漲跌幅上限 ±10% 給了一個乾淨的判準:若昨天收盤還在停損價之上
    (報酬 > -hard_stop_pct),今天最差也只能再跌一根跌停,所以今天的報酬必然
    > 0.9 × (1 - hard_stop_pct) - 1 = -(0.10 + 0.9 × hard_stop_pct),
    這個下界永遠落在 -(hard_stop_pct + 10%) 之內。反過來說,**跌幅已經超過
    hard_stop_pct + 一根跌停的部位,今天不可能是它的跨越日**。

    這種部位只有兩種來源:
      1. 停損意圖早就形成、只是還沒成交(跌停、停牌)→ `exit_pending=True`,
         引擎會繼續嘗試,policy 不需要也不該再產生一次新的 risk_stop 事件。
      2. 資料斷層(長期停牌後跳空重開)導致 policy 從沒看到跨越日 →
         `exit_pending=False`,這時仍然要 fail-closed 地產生 risk_stop,
         不能因為「錯過跨越日」就永遠不停損。

    `exit_pending` 缺值時預設 **False**(規格 §9A.1):不知道有沒有待成交的退出
    意圖時,寧可再產生一次 `risk_stop`(重複、但看得見),也不要靜默不停損
    (漏掉、而且任何輸出裡都看不出來)。這與 `snapshot_complete` 缺值取 False
    是同一條原則 —— 兩者都是「缺資訊時不得放過風險控制」,不是「缺資訊時一律
    不動作」;方向剛好相反是因為兩個旗標的 True 各自代表不同的事。
    """
    if not (math.isfinite(entry_price) and entry_price > 0
            and math.isfinite(close)):
        return "none"
    ret = close / entry_price - 1.0
    if ret > -float(hard_stop_pct) + 1e-12:
        return "none"
    if not _is_beyond_fresh_cross_zone(entry_price, close, hard_stop_pct):
        return "fresh_cross"
    return "breached_earlier" if exit_pending else "fresh_cross"


def _is_beyond_fresh_cross_zone(entry_price: float, close: float,
                                hard_stop_pct: float) -> bool:
    """今天**不可能**是這個部位的 hard stop 跨越日嗎?

    跌幅已經超過 `hard_stop_pct + 一根跌停` 時為 True(推導見
    `_hard_stop_state`)。只有落在這個區間,`exit_pending` 的值才會改變結果 ——
    所以「呼叫端沒給 `exit_pending`」的稽核計數也只在這裡才有意義。
    """
    if not (math.isfinite(entry_price) and entry_price > 0
            and math.isfinite(close)):
        return False
    ret = close / entry_price - 1.0
    return ret < -(float(hard_stop_pct) + DAILY_PRICE_LIMIT_PCT) - 1e-12


class StrategyPositionPolicy:
    """把「今天的排名快照」轉成「今天想要的部位」,不猜成交。

    用法(規格 §5 的最小公共契約):

    ```python
    policy = StrategyPositionPolicy(StrategyPositionPolicySpec())
    decision = policy.decide(as_of=..., signals=..., holdings=...,
                             equity=..., regime="risk_on", is_decision_day=True)
    ```
    """

    def __init__(self, spec: Optional[StrategyPositionPolicySpec] = None) -> None:
        self._spec = spec if spec is not None else StrategyPositionPolicySpec()
        if not isinstance(self._spec, StrategyPositionPolicySpec):
            raise TypeError("spec 必須是 StrategyPositionPolicySpec")
        # 執行期統計只放這裡。spec 是 frozen 的,任何回寫都會讓 rules hash
        # 與實際跑的規則分岔 —— contract test 直接釘住這一點。
        self._state: Dict[str, Any] = {"n_decisions": 0, "n_decision_days": 0}
        # 觸發過累積災難停損、尚未重新武裝的標的(見 REASON_STOP_NOT_REARMED)。
        # 這是單次回測內的路徑相依狀態,不是規則;不進 rules hash,但會出現在
        # `state()` 快照裡,讓「這一輪擋掉了哪些買回」看得見。
        self._stop_locked: set = set()

    # ── 唯讀存取 ────────────────────────────────────────────────────────
    @property
    def spec(self) -> StrategyPositionPolicySpec:
        return self._spec

    def rules(self) -> Dict[str, Any]:
        return self._spec.rules()

    def state(self) -> Dict[str, Any]:
        """執行期統計的唯讀快照(回傳 copy,呼叫端改不到內部狀態)。

        引擎會把它併進 summary 的稽核區塊:像
        `n_stop_repeated_unknown_exit_pending` 這種「這份結果有沒有被推定值影響」
        的計數,不放進結果就只能靠讀程式碼相信。
        """
        return dict(self._state)

    def rules_hash(self) -> str:
        """規則指紋。與 holdout 台帳／freeze manifest 共用同一份實作。"""
        return rules_fingerprint(self._spec.rules())

    # ── 決策 ────────────────────────────────────────────────────────────
    def decide(self, as_of, signals, holdings, equity, regime,
               is_decision_day, *, next_execution=None) -> StrategyPositionDecision:
        spec = self._spec
        as_of = pd.Timestamp(as_of)
        equity = float(equity)
        if not math.isfinite(equity) or equity < 0:
            raise ValueError(f"equity 必須是非負有限數,目前為 {equity!r}")
        # regime 可以是裸字串(→ unverified)或帶 provenance 的 RegimeState。
        # 規格 §4.3:policy 只接受帶 PIT provenance 的 regime;v1 不拒絕裸字串
        # (還沒有 regime 判定規格),但一定要標記,不得讓它冒充已驗證。
        regime_state = normalize_regime(regime)
        regime = regime_state.label
        slots = spec.slots_for_regime(regime)
        is_decision_day = bool(is_decision_day)
        if regime_state.provenance is not None:
            prov_asof = pd.Timestamp(regime_state.provenance.as_of)
            if prov_asof > as_of:
                raise ValueError(
                    f"[fail-closed] regime provenance 的 as_of({prov_asof}) 晚於決策日"
                    f"({as_of}):那是用未來資料回寫歷史 regime(規格 §4.3)")

        # T 日收盤決策最早 T+1 執行。呼叫端(引擎)知道真正的下一個交易日;
        # 沒給就退回「隔一個日曆日」——只用來標記「不是今天」,不假裝知道日曆。
        earliest = (pd.Timestamp(next_execution) if next_execution is not None
                    else as_of + pd.Timedelta(days=1))
        if earliest <= as_of:
            raise ValueError(
                f"[fail-closed] earliest_execution({earliest}) 不得早於或等於 "
                f"as_of({as_of}):T 日收盤資訊不可能在 T 日成交")

        sig, snapshot_complete, snapshot_date = _normalize_signals(signals, as_of)
        held = _normalize_holdings(holdings)
        rank_of = dict(zip(sig["stock_id"], sig["rank"]))

        # ── 重新武裝(規格 §3.6)────────────────────────────────────────────
        # 觸發過累積災難停損的標的,必須先在一份**夠格作證的快照**裡離開
        # top exit_rank,才允許重新買回。夠格 = 同時滿足三件事:
        #
        #   1. `is_decision_day` —— 換股名次只在決策日有意義。非決策日的快照
        #      不是拿來做進出場排序的,拿它解鎖等於偷偷多給了幾次解鎖機會。
        #   2. `snapshot_complete` —— 不完整的快照裡「沒看到它」不等於「它掉出
        #      去了」,那正是這個旗標存在的理由。
        #   3. 不是 stale —— `_normalize_signals` 取的是「截至 as_of 的最新快照」,
        #      那可能是好幾天前的。舊快照可以拿來維持部位(總比沒有好),但不足以
        #      當成「它現在已經掉出 top 20」的證據。frame 沒帶 date 欄時視為當日
        #      (呼叫端自己保證),這是引擎與既有 owner contract 的用法。
        #
        # 三者皆備時,**完全沒出現在快照裡**也算離開 —— 完整快照列的是當日
        # eligible 母體,不在其中代表它連被排名的資格都沒有,那比「名次掉到
        # 第 21」更徹底。舊版把這種情況當成「不確定」而永久鎖住,等於一檔股票
        # 只要停損後就離開 universe,就再也回不來,而輸出裡看不出原因。
        stale = (snapshot_date is not None
                 and pd.Timestamp(snapshot_date) < as_of)
        rearm_evidence_ok = bool(is_decision_day and snapshot_complete
                                 and not stale and len(sig) > 0)
        n_rearmed = 0
        if rearm_evidence_ok and self._stop_locked:
            for locked_sid in sorted(self._stop_locked):
                r = rank_of.get(locked_sid)
                left_universe = r is None or not math.isfinite(float(r))
                if left_universe or float(r) > float(spec.exit_rank):
                    self._stop_locked.discard(locked_sid)
                    n_rearmed += 1
        score_of = dict(zip(sig["stock_id"], sig["raw_score"]))
        held_ids = set(held["stock_id"])

        actions: List[Dict[str, Any]] = []
        exits: Dict[str, List[str]] = {}
        stale_stop_breaches: List[str] = []
        # 呼叫端沒帶 `exit_pending`(§5 的最小 holdings 契約)時,預設 False 會
        # 讓深跌部位每天都重新產生一次 `risk_stop`。那是刻意選的方向(寧可重複
        # 也不要漏),但重複次數必須可稽核 —— 事後要分得出「這批 risk_stop 是
        # 真的多次跨越」還是「呼叫端沒給欄位」。
        unknown_exit_pending: List[str] = []

        def _add_exit(sid: str, reason: str) -> None:
            exits.setdefault(sid, []).append(reason)

        # ── (1) 每日都成立的強制/風險退出(規格 §3.2)──────────────────
        for row in held.itertuples():
            sid = str(row.stock_id)
            if bool(row.forced_exit):
                _add_exit(sid, "forced_exit")
            # 收盤確認的 hard stop;不看日內 low(規格 §3.4)。
            stop_state = _hard_stop_state(
                float(row.entry_price), float(row.close),
                float(spec.hard_stop_pct), bool(row.exit_pending))
            if stop_state == "fresh_cross":
                _add_exit(sid, "risk_stop")
                self._stop_locked.add(sid)
                if not bool(row.exit_pending_known) and _is_beyond_fresh_cross_zone(
                        float(row.entry_price), float(row.close),
                        float(spec.hard_stop_pct)):
                    # 只有深跌區間的部位才會受 `exit_pending` 預設影響;這一筆
                    # risk_stop 有可能是「昨天也產生過一次」的重複。
                    unknown_exit_pending.append(sid)
            elif stop_state == "breached_earlier":
                stale_stop_breaches.append(sid)
            if slots <= 0:
                # risk_off 是緊急降曝險,允許每日形成退出意圖(不保證成交)。
                _add_exit(sid, "regime_reduce")
            if bool(row.thesis_break) and is_decision_day:
                _add_exit(sid, "thesis_break")
            if int(row.holding_days) >= int(spec.max_hold_days):
                _add_exit(sid, "max_hold")

        # ── (2) 一般排名換股:只在決策日(規格 §3.1)────────────────────
        if is_decision_day:
            for row in held.itertuples():
                sid = str(row.stock_id)
                rank = rank_of.get(sid)
                if rank is None or not math.isfinite(float(rank)):
                    if snapshot_complete:
                        # 只有**明確宣告完整**的 snapshot,「不在名單裡」才等於
                        # 已不在 eligible universe。沒有旗標時 snapshot_complete
                        # 是 False(規格 §5、§9B.1),什麼都不做 —— 未列出只能
                        # 解讀為 unknown,不可拿「我沒看到它」當賣出的證據。
                        _add_exit(sid, "not_ranked")
                    continue
                if float(rank) > float(spec.exit_rank):
                    _add_exit(sid, "rank_decay")

        # ── (3) 決定 keepers,必要時依 regime 層級縮到可用 slots ─────────
        keepers = [str(r.stock_id) for r in held.itertuples()
                   if str(r.stock_id) not in exits]
        keepers.sort(key=lambda s: (float(rank_of.get(s, np.inf)), s))
        if is_decision_day and len(keepers) > slots:
            # risk_on → caution:保留規則允許下排名最好的 N 檔,其餘 regime_reduce。
            # 不要求「十檔各賣一半」——那會製造十筆成本卻沒有真的降低單檔風險。
            for sid in keepers[slots:]:
                _add_exit(sid, "regime_reduce")
            keepers = keepers[:slots]

        weight_of = {str(r.stock_id): float(r.weight) for r in held.itertuples()}

        # ── (4) 目標權重:等權資金槽,候選不足保留現金(規格 §4.2)────────
        targets: List[Dict[str, Any]] = []
        used: List[float] = []

        def _available() -> float:
            return 1.0 - math.fsum(used)

        for sid in keepers:
            current = weight_of.get(sid, 0.0)
            if current > float(spec.single_name_cap) + _WEIGHT_TOL:
                target_w = float(spec.single_name_cap)
                action, reason = "resize", REASON_CAP
            else:
                # 權重漂移不交易:目標就是現在的權重。若在這裡寫 slot_weight,
                # 引擎每天都會為了「補回 10%」而攤平下跌部位(規格 §4.2 明文禁止)。
                target_w = current
                action = "hold"
                if sid in stale_stop_breaches:
                    # 早已跌破停損價、退出意圖仍卡在成交端(跌停/停牌)。誠實標記,
                    # 不要偽裝成一般續抱,也不要重複產生一次新的 risk_stop 事件。
                    # 走到這裡必然是引擎**顯式**說 `exit_pending=True`(缺值預設
                    # False 會走 fresh_cross → risk_stop),所以這個標記是事實。
                    reason = REASON_STOP_BREACHED_EARLIER
                else:
                    reason = (REASON_HOLD if is_decision_day
                              else REASON_HOLD_OFF_DAY)
            used.append(target_w)
            targets.append({"stock_id": sid, "target_weight": target_w,
                            "target_notional": round(target_w * equity, 2)})
            actions.append({
                "stock_id": sid, "action": action, "reason_code": reason,
                "decision_rank": float(rank_of.get(sid, np.nan)),
                "raw_score": float(score_of.get(sid, np.nan)),
                "current_weight": current, "target_weight": target_w,
                "earliest_execution": earliest,
                "secondary_reasons": "",
            })

        n_free = max(0, slots - len(keepers))
        n_skipped_no_room = 0
        n_blocked_not_rearmed = 0
        if is_decision_day and n_free > 0:
            for row in sig.itertuples():
                if n_free <= 0:
                    break
                sid = str(row.stock_id)
                if sid in held_ids:
                    continue      # 已持有(含正在退出的)不重複買
                if float(row.rank) > float(spec.entry_rank):
                    break         # sig 已依 rank 排序,後面只會更差
                if sid in self._stop_locked:
                    # 剛因為累積災難虧損被賣掉,還沒掉出 top exit_rank →
                    # 不得買回。不佔 slot、不消耗現金,但要留下紀錄。
                    n_blocked_not_rearmed += 1
                    actions.append({
                        "stock_id": sid, "action": "skip",
                        "reason_code": REASON_STOP_NOT_REARMED,
                        "decision_rank": float(row.rank),
                        "raw_score": float(row.raw_score),
                        "current_weight": 0.0, "target_weight": 0.0,
                        "earliest_execution": earliest,
                        "secondary_reasons": "",
                    })
                    continue
                if _available() < float(spec.slot_weight) - _WEIGHT_TOL:
                    # 既有持股已把淨值佔滿(例如漲上去逼近 cap)→ 保留現金,
                    # 不縮小 slot 硬塞進去。
                    n_skipped_no_room += 1
                    continue
                used.append(float(spec.slot_weight))
                targets.append({
                    "stock_id": sid,
                    "target_weight": float(spec.slot_weight),
                    "target_notional": round(float(spec.slot_weight) * equity, 2),
                })
                actions.append({
                    "stock_id": sid, "action": "enter",
                    "reason_code": REASON_NEW_TOP_K,
                    "decision_rank": float(row.rank),
                    "raw_score": float(row.raw_score),
                    "current_weight": 0.0,
                    "target_weight": float(spec.slot_weight),
                    "earliest_execution": earliest,
                    "secondary_reasons": "",
                })
                n_free -= 1

        # ── (5) 退出動作(單一主要 reason_code + 次要觸發)────────────────
        for sid, reasons in exits.items():
            ordered = [r for r in EXIT_PRIORITY if r in reasons]
            primary = ordered[0] if ordered else reasons[0]
            secondary = [r for r in ordered[1:]]
            actions.append({
                "stock_id": sid, "action": "exit", "reason_code": primary,
                "decision_rank": float(rank_of.get(sid, np.nan)),
                "raw_score": float(score_of.get(sid, np.nan)),
                "current_weight": weight_of.get(sid, 0.0),
                "target_weight": 0.0,
                "earliest_execution": earliest,
                "secondary_reasons": "|".join(secondary),
            })

        targets_frame = pd.DataFrame(
            targets, columns=["stock_id", "target_weight", "target_notional"])
        actions_frame = pd.DataFrame(actions, columns=[
            "stock_id", "action", "reason_code", "decision_rank", "raw_score",
            "current_weight", "target_weight", "earliest_execution",
            "secondary_reasons"])

        total_w = math.fsum(used)
        cash_w = 1.0 - total_w
        if cash_w < -_WEIGHT_TOL:
            raise ValueError(
                f"[fail-closed] target weights 合計 {total_w:.6f} > 1(cash {cash_w:.6f});"
                "long-only 不得隱性加槓桿")
        cash_w = max(0.0, cash_w)
        if abs(total_w + cash_w - 1.0) > 1e-9:
            raise ValueError(
                f"[fail-closed] target weights + cash = {total_w + cash_w:.9f} != 1")
        if not targets_frame.empty and (targets_frame["target_weight"] < 0).any():
            raise ValueError("[fail-closed] long-only 不得出現負目標權重")

        self._state["n_decisions"] = int(self._state.get("n_decisions", 0)) + 1
        if is_decision_day:
            self._state["n_decision_days"] = int(
                self._state.get("n_decision_days", 0)) + 1
        # 累積災難停損的鎖:擋掉幾次買回、解鎖幾檔、目前還鎖著幾檔。
        # 不放進 summary 就只能靠讀程式碼相信這條規則有生效。
        self._state["n_entries_blocked_not_rearmed"] = int(
            self._state.get("n_entries_blocked_not_rearmed", 0)) + n_blocked_not_rearmed
        self._state["n_stop_rearmed"] = int(
            self._state.get("n_stop_rearmed", 0)) + n_rearmed
        self._state["n_stop_locked_now"] = len(self._stop_locked)
        # 「這一天的快照夠不夠格解鎖」也要看得見:某個 run 若從頭到尾都不夠格,
        # 停損鎖就永遠不會解開,而那不會報錯,只會表現成「怎麼都不再買回」。
        if self._stop_locked and not rearm_evidence_ok:
            self._state["n_rearm_evidence_rejected"] = int(
                self._state.get("n_rearm_evidence_rejected", 0)) + 1
        self._state["n_entries_skipped_no_room"] = int(
            self._state.get("n_entries_skipped_no_room", 0)) + n_skipped_no_room
        self._state["n_stop_breached_earlier"] = int(
            self._state.get("n_stop_breached_earlier", 0)) + len(stale_stop_breaches)
        # 這一項若不是 0,代表有 risk_stop 是在「呼叫端沒給 exit_pending」的深跌
        # 區間產生的 —— 同一次停損可能被記成多天。正式回測路徑(事件引擎)一律
        # 顯式帶欄位,所以它應該恆為 0;不為 0 時停損次數統計不可直接採信。
        self._state["n_stop_repeated_unknown_exit_pending"] = int(
            self._state.get("n_stop_repeated_unknown_exit_pending", 0)
        ) + len(unknown_exit_pending)
        if not regime_state.verified:
            # 裸字串 regime 的次數:summary 要靠它決定能不能標正式證據。
            self._state["n_unverified_regime_decisions"] = int(
                self._state.get("n_unverified_regime_decisions", 0)) + 1

        rules = self._spec.rules()
        fingerprint = rules_fingerprint({
            "rules": rules,
            "as_of": str(as_of),
            # regime 的出處進指紋:同一天同一個 label,一個有 PIT provenance、
            # 一個沒有,那是兩份不同可信度的決策,不該有相同指紋。
            "regime": regime_state.rules(),
            "is_decision_day": is_decision_day,
            "snapshot_complete": snapshot_complete,
            "targets": [(t["stock_id"], round(t["target_weight"], 10))
                        for t in targets],
            "exits": sorted((sid, [r for r in EXIT_PRIORITY if r in rs][:1] or rs)
                            for sid, rs in exits.items()),
        })
        return StrategyPositionDecision(
            as_of=as_of,
            actions=actions_frame,
            targets=targets_frame,
            target_cash_weight=float(cash_w),
            snapshot_complete=bool(snapshot_complete),
            policy_rules=rules,
            regime=regime,
            is_decision_day=is_decision_day,
            equity=equity,
            available_slots=int(slots),
            earliest_execution=earliest,
            fingerprint=fingerprint,
            regime_verified=bool(regime_state.verified),
            regime_provenance=(regime_state.provenance.rules()
                               if regime_state.provenance is not None else None),
        )


__all__ = [
    "EXIT_PRIORITY",
    "REASON_STOP_BREACHED_EARLIER",
    "RegimeProvenance",
    "RegimeState",
    "StrategyPositionDecision",
    "StrategyPositionPolicy",
    "StrategyPositionPolicySpec",
    "VALID_REGIMES",
    "normalize_regime",
]
