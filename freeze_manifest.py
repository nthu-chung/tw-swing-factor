# -*- coding: utf-8 -*-
"""
凍結研究規則 manifest(clean-OOS 的第一步)
==========================================
本專案沒有真正乾淨的 OOS:所有權重/exit/門檻都反覆看過同一段 2024-2026 資料。
唯一能長出真 OOS 的方法 = 現在把「一整套規則」凍結成 immutable manifest,之後
只用 forward_test.py 對「凍結日之後才發生」的新資料 forward-only 驗證。

manifest 一旦寫出就**不可覆寫**(避免偷改規則再宣稱樣本外)。要改規則 = 開新 manifest、
重新累積 forward 期。

2026-08-15 修掉的三個 bug(schema 1 → 2)
----------------------------------------
1. **label 既不進檔名、也不進 hash**:`build_manifest('strat_A')` 與
   `('strat_B')` 產生同一個 `rules_sha256_16` 與同一個檔名
   `FROZEN_MANIFEST_{date}.json` → 兩套研究互相覆寫/冒名。
   修法:label 進**檔名**(同日多份可共存),**不進** hash(同規則換名字不該
   變成另一套規則,否則沒人能證明兩次凍結是同一條規則)。
2. **手維護的 `FROZEN_KEYS` 只有 34 個**,config 有 92 個大寫參數,缺席的正好是
   最會改變結果的那些:`SELF_ADJUST_PRICES`、`ALLOW_UNADJUSTED_BACKTEST`、
   `BT_ORDER_SIZE_MODE`、漲跌停/處置模型、`BT_STALE_EXIT_DAYS`、
   IS-OS/embargo 切割…。手維護清單的失敗模式是「新增參數沒人記得加」,而且
   失敗時**靜默**(hash 照樣算得出來,看起來一切正常)。
   修法:反向 —— config 的每個大寫參數預設都是 load-bearing、一律凍結;要排除
   必須寫進 `NOT_FROZEN` 並附理由。無法序列化又沒被分類 → raise。
3. **策略自己的參數完全沒被凍**:S19 的視窗/權重/持股數/再平衡天數/MA 出場/
   停損是模組常數,投組那半還是在 manifest 產生**之後**才被寫進 config。
   修法:`strategies/spec.py` 的 `StrategySpec` 進 manifest 的 `rules["strategy"]`。

2026-08-15 再修(schema 2 → 3):manifest 沒有釘住 holdout 邊界
--------------------------------------------------------------
manifest 只凍了切割的**參數**(`EVAL_SPLIT_MODE`/`IS_OS_SPLIT`/`EMBARGO_DAYS`…),
沒有記下它們解出來的**日期**。而 IS/OS 切點完全由凍結資料自身的首尾日決定
(`evaluation/splits.py` 錨在 `dts[-1]`),資料視窗兩端又隨 `SNAPSHOT_END_DATE`
滑動(`start = end - HISTORY_DAYS`)。實測快照 2026-06-22 的 OS 是
2025-11-19~2026-06-18,推進到 2026-08-06 之後 OS 起點變成 2026-01-05 ——
2025-11-19~2026-01-04 這段**從 OS 變成 IS**,而同一份 manifest 的參數一個字都
沒變。所以邊界必須跟著 manifest 一起釘住(`manifest["holdout"]`),揭露時再由
`evaluation/holdout.py` 的 append-only 揭露紀錄記「這段被誰在何時看過」。

`holdout` 刻意**不進 `rules` / hash**:解出來的日期是資料的函數,放進 hash 會讓
同一套規則在不同快照下變成不同規則(SNAPSHOT_END_DATE 不進 hash 是同一個理由)。

2026-08-15 再修:邊界「有欄位」不等於「有釘住」
------------------------------------------------
`holdout_boundaries(calendar=...)` 是選用關鍵字,而 CLI **沒有**對應選項 ——
走正式路徑產出的 manifest 一律 `resolved=False`(`is_window`/`os_window` 都是
null),`validate_manifest` 只給一個 warning、`forward_test` 印一行就照跑。閘門
又變回「呼叫端要記得傳的關鍵字參數」。修法兩件事:(1) `run()`(CLI 的唯一
路徑)預設自己用 `trading_calendar()` 解日曆(離線只讀 TAIEX 一條序列);
(2) `resolved=False` 從 warning 升成 `ok=False` —— 沒有邊界的 manifest 不是
可靠的凍結版本,不得被 forward 拿去宣稱 OOS。

輸出:outputs/FROZEN_MANIFEST_<freeze_date>_<label>.json(immutable)。
用法:.venv/bin/python freeze_manifest.py --label momentum_only_v1
      .venv/bin/python freeze_manifest.py --strategy s19_chip_momentum --label s19_v1
"""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

import config
import provenance
from evaluation import holdout as holdout_ledger
from evaluation.splits import build_evaluation_split
from strategy_kit.spec import KNOWN_STRATEGIES, StrategySpec, load_spec

# manifest 格式版本。schema < 3 的 manifest 缺策略參數、大半 load-bearing 設定,
# 或缺 holdout 邊界,屬 legacy/不完整,forward 必須拒用(見 validate_manifest)。
MANIFEST_SCHEMA = 3

DEFAULT_STRATEGY = "s19_chip_momentum"

# 檔名裡的 label 會變成路徑的一部分,限制字元集(避免 ../ 與空白造成的意外)。
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


# ── 反向 allowlist:預設全凍,排除要寫理由 ────────────────────────────────
# 這裡每一行都必須是「不影響結果」或「另有專屬欄位」的理由。想排除一個會改變
# 績效的參數 = 讓 hash 對它盲目 = 這次要修的 bug 重演。
NOT_FROZEN: Dict[str, str] = {
    "ROOT": "路徑,與研究規則無關",
    "CACHE_DIR": "路徑,與研究規則無關",
    "OUTPUT_DIR": "路徑,與研究規則無關",
    "FINMIND_TOKEN": "密鑰,絕不可寫進產出物",
    "FINMIND_BASE": "端點位址,不改變規則",
    "FINMIND_SLEEP": "抓取節流,不改變資料內容",
    "FINMIND_MAX_RETRIES": "抓取重試,不改變資料內容",
    "FINMIND_RETRY_BACKOFF": "抓取重試,不改變資料內容",
    "CACHE_TTL_HOURS_DEFAULT": "快取新鮮度;真正決定資料邊界的是快照日",
    "SAMPLE_UNIVERSE": "smoke test 用的小集合,正式回測不使用",
    "FACTOR_WEIGHTS_LEGACY_9": "歷史備查權重,未被任何路徑讀取",
    "FACTOR_WEIGHTS_LEGACY_MOMQ": "歷史備查權重,未被任何路徑讀取",
    # 快照日刻意不進 rules:forward 的前提就是「推進快照抓新資料」,把它放進
    # hash 會讓凍結規則在推進快照後對不上自己,forward 永遠無法累積。
    # 它另外記在 manifest 的 data_snapshot_at_freeze。
    "SNAPSHOT_END_DATE": "資料邊界,另存 data_snapshot_at_freeze(forward 必須推進它)",
}

# forward 要信任一份 manifest 的最低門檻:這些 key 少任何一個就視為不完整。
# 這是「舊 manifest 冒充可靠凍結版本」的擋點,所以列的是**已知會改變結果**的
# 那批(含上一版 FROZEN_KEYS 缺席的全部)。
REQUIRED_CONFIG_KEYS: Tuple[str, ...] = (
    # 訊號與選股
    "FACTOR_WEIGHTS", "MIN_COMPOSITE", "TOP_N", "TREND_GUARD_ENABLED",
    "MA_SHORT", "MA_LONG", "MOM_LOOKBACK", "HIGH_LOOKBACK",
    # 價格資料集與自建還原
    "PRICE_DATASET", "SELF_ADJUST_PRICES", "ALLOW_UNADJUSTED_BACKTEST",
    "PRICE_INTEGRITY_RETURN_THRESHOLD", "HISTORY_DAYS", "MARKET_HISTORY_DAYS",
    # PIT 候選池與每日 universe
    "DYNAMIC_UNIVERSE_ENABLED", "DYNAMIC_UNIVERSE_TOP_N",
    "DYNAMIC_UNIVERSE_CANDIDATE_POOL", "DYNAMIC_UNIVERSE_MONTHLY_MIN_OBS",
    "DYNAMIC_UNIVERSE_LOOKBACK", "DYNAMIC_UNIVERSE_MIN_OBS",
    "DYNAMIC_UNIVERSE_MIN_AVG_VOLUME_LOTS", "DYNAMIC_UNIVERSE_MIN_AVG_TURNOVER",
    "ALLOW_FUTURE_POOL", "EXCLUDE_FINANCE", "EXCLUDE_ETF_PREFIX0",
    "MIN_AVG_VOLUME_LOTS",
    # 出場與持有
    "BT_EXIT_MODE", "BT_MA_EXIT", "BT_TREND_STOP_LOSS", "BT_MAX_HOLD_DAYS",
    "BT_HOLD_DAYS", "BT_TAKE_PROFIT", "BT_STOP_LOSS", "BT_MAX_POSITIONS",
    "BT_ENTRY_NEXT_OPEN", "BT_STALE_EXIT_DAYS", "BT_DELIST_RECOVERY",
    # 成本、張數模式、漲跌停與處置
    "BT_FEE", "BT_TAX", "BT_MIN_COMMISSION", "BT_ORDER_SIZE_MODE",
    "BT_INITIAL_CAPITAL", "BT_REGULAR_LOT_SHARES", "BT_MODEL_LIMIT_LOCK",
    "BT_PRICE_LIMIT_SOURCE", "BT_MODEL_DISPOSITION",
    # 市場濾網
    "MARKET_FILTER_ENABLED", "MARKET_FILTER_RULE", "MARKET_FILTER_RISKOFF_WEIGHT",
    # IS / embargo / OS 與 IC
    "EVAL_SPLIT_MODE", "IS_OS_SPLIT", "IS_WEEKS", "OS_WEEKS", "EMBARGO_DAYS",
    "BT_IC_HORIZON",
)


def _config_param_names() -> List[str]:
    """config 裡所有「看起來是參數」的名字(大寫、非私有)。"""
    return sorted(k for k in vars(config)
                  if k.isupper() and not k.startswith("_"))


def frozen_config_keys() -> List[str]:
    """要凍結的 config key(反向 allowlist;不明型別 fail-closed)。"""
    names = _config_param_names()
    stale = sorted(set(NOT_FROZEN) - set(names))
    if stale:
        raise RuntimeError(
            f"NOT_FROZEN 列了 config 已經不存在的參數 {stale};請清掉,"
            "否則排除理由會替一個不存在的東西背書"
        )
    keys = [k for k in names if k not in NOT_FROZEN]
    unserializable = []
    for k in keys:
        try:
            json.dumps(getattr(config, k))
        except (TypeError, ValueError):
            unserializable.append(k)
    if unserializable:
        raise RuntimeError(
            f"[fail-closed] config 參數 {unserializable} 無法序列化進 manifest。"
            "請把它改成可序列化的值,或寫進 NOT_FROZEN 並附上「不影響結果」的理由。"
            "沉默略過就是上一版 FROZEN_KEYS 漏掉 58 個參數的同一個失敗模式。"
        )
    return keys


def frozen_config() -> Dict[str, Any]:
    return {k: getattr(config, k) for k in frozen_config_keys()}


def rules_payload(spec: StrategySpec) -> Dict[str, Any]:
    """進 hash 的完整規則 = config 參數 + 策略規格。**不含 label**。"""
    return {"config": frozen_config(), "strategy": spec.rules()}


def rules_hash(rules: Dict[str, Any]) -> str:
    """規則 hash。實作在 `evaluation.holdout.rules_fingerprint`(唯一一份)。

    為什麼要共用:holdout 揭露紀錄記的 `strategy_hash` 必須和 manifest 的
    `rules_sha256_16` 是同一個東西,否則「這段 OS 是哪一套規則看的」對不起來。
    """
    return holdout_ledger.rules_fingerprint(rules)


# ── holdout(IS / embargo / OS)邊界 ──────────────────────────────────────
HOLDOUT_SLIDING_NOTE = (
    "IS/OS 切點由凍結資料自身的首尾日決定(evaluation/splits.py 錨在 dts[-1]),"
    "而資料視窗兩端隨 SNAPSHOT_END_DATE 滑動(start = end - HISTORY_DAYS)。"
    "實測:快照 2026-06-22 的 OS 是 2025-11-19~2026-06-18;推進到 2026-08-06 之後"
    "OS 起點變成 2026-01-05 —— 2025-11-19~2026-01-04 從 OS 變成 IS。"
    "所以邊界要釘在 manifest 裡,揭露紀錄見 outputs/holdout_ledger.jsonl。"
)


def trading_calendar(history_days: Optional[int] = None) -> Any:
    """解出凍結資料的交易日曆(**離線、只讀一條序列**)。

    為什麼要有這個函式(2026-08-15 審查抓到的洞)
    ----------------------------------------------
    `holdout_boundaries(calendar=...)` 一直是**選用關鍵字**,而 `__main__` 沒有
    對應的 CLI 選項 —— 也就是說走 CLI **不可能**解出日期。實測 `run("cli_default")`
    產出 `holdout.resolved=False`、`is_window=null`、`os_window=null`,而
    `validate_manifest` 只回一個 warning、`forward_test.run` 印一行就照跑:
    P1-3 的「manifest 固定記錄 IS／embargo／OS 邊界」在唯一的正式路徑上等於沒做,
    閘門又變回「呼叫端要記得傳的關鍵字參數」。

    這裡取的是大盤(TAIEX)一條序列(`data.fetch_market_index`,快取優先),
    再裁到個股價格的資料視窗 —— 不觸發全市場抓取,所以凍結仍然是便宜的動作;
    真的抓不到就 raise,而不是產出一份沒有邊界的 manifest。
    """
    import data

    market = data.fetch_market_index()
    if market is None or len(market) == 0 or "date" not in market.columns:
        raise RuntimeError(
            "[fail-closed] 解不出交易日曆:大盤(TAIEX)序列為空。"
            "manifest 必須固定記錄 IS／embargo／OS 邊界(只記切割參數不算 ——"
            "同一組參數在不同快照下解出不同的 OS)。請先跑 prefetch.py 取得 "
            "TAIEX 快取,再重新凍結。"
        )
    days = pd.DatetimeIndex(
        sorted(set(pd.to_datetime(market["date"], errors="coerce").dropna()))
    )
    # 裁到個股價格的視窗:TAIEX 抓的是 MARKET_HISTORY_DAYS(較長,MA200 暖身),
    # 直接拿去切 IS/OS 會把 IS 起點推到回測根本看不到的日期。
    scope = data.cache_scope("price", "CALENDAR", history_days)
    lo, hi = pd.Timestamp(scope.start), pd.Timestamp(scope.end)
    days = days[(days >= lo) & (days <= hi)]
    if len(days) < 2:
        raise RuntimeError(
            f"[fail-closed] 交易日曆在資料視窗 {scope.start}~{scope.end} 內只有 "
            f"{len(days)} 天,解不出 IS／embargo／OS 邊界"
        )
    return days


def holdout_boundaries(calendar: Optional[Any] = None) -> Dict[str, Any]:
    """凍結時的 IS / embargo / OS 邊界。

    `calendar` = 凍結資料的交易日序列(例如全期回測 `equity_curve["date"]`,
    或 `trading_calendar()`)。有它才解得出**日期**;沒有就只記切割**規則**並標
    `resolved=False` —— 而 `resolved=False` 的 manifest **不是**可靠的凍結版本
    (`validate_manifest` 會判 `ok=False`),正式路徑 `run()` 預設會自己去解。
    """
    out: Dict[str, Any] = {
        "boundaries_schema": 1,
        "split_mode": getattr(config, "EVAL_SPLIT_MODE", None),
        "is_os_split": getattr(config, "IS_OS_SPLIT", None),
        "is_weeks": getattr(config, "IS_WEEKS", None),
        "os_weeks": getattr(config, "OS_WEEKS", None),
        "embargo_days": getattr(config, "EMBARGO_DAYS", None),
        "minimum_embargo_days": getattr(config, "BT_IC_HORIZON", None),
        "history_days": getattr(config, "HISTORY_DAYS", None),
        "snapshot_end_at_freeze": getattr(config, "SNAPSHOT_END_DATE", ""),
        "resolved": False,
        "is_window": None,
        "os_window": None,
        "embargo_trading_days": None,
        "n_is": None,
        "n_os": None,
        "n_total": None,
        "note": HOLDOUT_SLIDING_NOTE,
    }
    if calendar is None:
        out["unresolved_reason"] = (
            "凍結時沒有提供交易日曆,只釘住切割規則;實際 OS 日期由揭露當下的"
            "資料視窗決定,會記進 holdout 揭露紀錄"
        )
        return out
    split = build_evaluation_split(
        calendar, minimum_embargo_days=getattr(config, "BT_IC_HORIZON", 0)
    )
    d = split.to_dict()
    out.update({
        "resolved": True,
        "is_window": list(split.is_window),
        "os_window": list(split.os_window),
        "embargo_trading_days": d["n_embargo"],
        "n_is": d["n_is"],
        "n_os": d["n_os"],
        "n_total": d["n_total"],
    })
    return out


def _git_state() -> Dict[str, Any]:
    """記錄 git 狀態。dirty 的工作樹代表 manifest 對不到任何 commit ——
    那份凍結**無法重現**,必須看得見(不是靜默當成乾淨)。

    實作在 `provenance.git_state`(回測 summary 用的是同一份)。凍結是一次性
    動作,所以不吃 process 快取,一律重讀當下的工作樹狀態。
    """
    return provenance.git_state(use_cache=False)


def manifest_filename(freeze_date: str, label: str) -> str:
    """label 一定要進檔名:同一天凍兩套規則不可互相覆寫。"""
    if not _LABEL_RE.match(label or ""):
        raise ValueError(
            f"label {label!r} 不合法(只允許英數與 . _ -,長度 1~64);"
            "label 會變成檔名的一部分"
        )
    return f"FROZEN_MANIFEST_{freeze_date}_{label}.json"


def build_manifest(label: str, spec: Optional[StrategySpec] = None, *,
                   strategy: str = DEFAULT_STRATEGY,
                   freeze_date: Optional[str] = None,
                   calendar: Optional[Any] = None) -> Dict[str, Any]:
    """組出 manifest。`rules_sha256_16` 只由 `rules` 決定(label、holdout 不在內)。

    `calendar` 傳入凍結資料的交易日序列時,`holdout` 段會帶解出來的 IS/embargo/OS
    **日期**;不傳就只釘住切割規則(見 `holdout_boundaries`)。
    """
    manifest_filename(freeze_date or "0000-00-00", label)   # 先驗 label
    spec = spec if spec is not None else load_spec(strategy)
    freeze_date = freeze_date or datetime.now().strftime("%Y-%m-%d")
    rules = rules_payload(spec)
    manifest = {
        "manifest_schema": MANIFEST_SCHEMA,
        "label": label,                     # 人類標籤:不進 hash
        "freeze_date": freeze_date,
        "data_snapshot_at_freeze": getattr(config, "SNAPSHOT_END_DATE", ""),
        "rules_sha256_16": rules_hash(rules),
        "strategy_name": spec.name,
        "rules": rules,
        # holdout 邊界不進 rules/hash:解出來的日期是**資料**的函數,進 hash 會
        # 讓同一套規則在不同快照下變成不同規則(與 SNAPSHOT_END_DATE 同理)。
        "holdout": holdout_boundaries(calendar),
        "frozen_config_key_count": len(rules["config"]),
        "not_frozen": dict(sorted(NOT_FROZEN.items())),
        "note": (
            "IMMUTABLE。forward_test.py 只驗證 freeze_date 之後(且 SNAPSHOT_END_DATE > "
            "data_snapshot_at_freeze 時新抓到)的資料。規則要改請開新 manifest,勿覆寫。"
            "hash 不含 label:同一組規則換 label 仍是同一套規則。"
        ),
    }
    manifest.update(_git_state())
    return manifest


def manifest_path(m: Dict[str, Any]) -> Path:
    return config.OUTPUT_DIR / manifest_filename(m["freeze_date"], m["label"])


# ── 驗證:legacy / 不完整的 manifest 不得冒充可靠凍結版本 ────────────────
@dataclass
class ManifestStatus:
    ok: bool
    reliability: str
    problems: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    spec: Optional[StrategySpec] = None
    missing_config_keys: List[str] = field(default_factory=list)

    def describe(self) -> str:
        parts = [f"reliability={self.reliability}"]
        if self.problems:
            parts.append("problems=" + "; ".join(self.problems))
        if self.warnings:
            parts.append("warnings=" + "; ".join(self.warnings))
        return " | ".join(parts)


def validate_manifest(m: Any) -> ManifestStatus:
    """判斷一份 manifest 能否作為 forward 的凍結基準。

    `ok=False` 的 manifest 就是「不可靠」:legacy schema、缺 load-bearing 參數、
    缺策略規格,或 rules 被事後改過(hash 對不上)。forward 必須拒用而不是
    照跑然後產出看起來像 clean OOS 的數字。
    """
    problems: List[str] = []
    warnings: List[str] = []
    spec: Optional[StrategySpec] = None
    missing: List[str] = []

    if not isinstance(m, dict):
        return ManifestStatus(False, "invalid", ["manifest 不是 JSON object"])

    schema = m.get("manifest_schema")
    if schema != MANIFEST_SCHEMA:
        problems.append(
            f"manifest_schema={schema!r} 不是 {MANIFEST_SCHEMA}:"
            "legacy 格式缺策略參數與大半 load-bearing 設定,不可冒充可靠凍結版本"
        )

    if not isinstance(m.get("label"), str) or not m.get("label"):
        problems.append("缺 label")
    if not m.get("freeze_date"):
        problems.append("缺 freeze_date")

    # holdout 邊界:少了它,forward 事後無法說明「當時凍住的 OS 是哪一段」,
    # 而 OS 邊界會隨快照滑動(見 HOLDOUT_SLIDING_NOTE)。
    hold = m.get("holdout")
    if not isinstance(hold, dict):
        problems.append(
            "缺 holdout 段:manifest 必須固定記錄 IS／embargo／OS 邊界"
            "(OS 切點隨資料視窗滑動,只記切割參數等於沒記)"
        )
    else:
        missing_hold = [k for k in ("split_mode", "embargo_days", "resolved")
                        if k not in hold]
        if missing_hold:
            problems.append(f"holdout 段缺 {missing_hold}")
        elif not hold.get("resolved"):
            # 2026-08-15:原本只是 warning,於是「manifest 固定記錄 IS／embargo／
            # OS 邊界」實際上從沒發生過 —— CLI 根本沒有傳日曆的路徑,產出的
            # manifest 一律 resolved=False,而 forward 印一行警告就照跑。
            # 只凍切割**參數**是不夠的:同一組參數在不同快照下解出不同的 OS。
            problems.append(
                "holdout 邊界未解析成日期(resolved=False):只釘住切割規則等於"
                "沒釘住 OS —— 同一組切割參數在不同快照下解出不同的 OS 區間。"
                "請用 freeze_manifest.run()(預設自己解交易日曆)或傳 "
                "build_manifest(calendar=...) 重新凍結"
            )

    rules = m.get("rules")
    if not isinstance(rules, dict):
        problems.append("缺 rules")
    else:
        cfg = rules.get("config")
        if not isinstance(cfg, dict):
            problems.append("rules 缺 config 段(legacy 扁平格式?)")
        else:
            missing = [k for k in REQUIRED_CONFIG_KEYS if k not in cfg]
            if missing:
                problems.append(
                    f"rules.config 缺 {len(missing)} 個 load-bearing 參數"
                    f"(例:{missing[:6]})"
                )
        strat = rules.get("strategy")
        if not isinstance(strat, dict):
            problems.append(
                "rules 缺 strategy 段:策略的視窗/權重/持股數/再平衡天數/"
                "MA 出場/停損沒有被凍結"
            )
        else:
            try:
                spec = StrategySpec.from_dict(strat)
            except Exception as exc:      # 缺參數 / 未知策略 / 未知欄位
                problems.append(f"strategy 規格無效:{exc}")

        recomputed = rules_hash(rules)
        if m.get("rules_sha256_16") != recomputed:
            problems.append(
                f"rules_sha256_16={m.get('rules_sha256_16')!r} 與 rules 內容"
                f"重算的 {recomputed!r} 不符:manifest 應為 immutable,"
                "對不上代表被事後改過"
            )

    if m.get("git_commit", "unknown") == "unknown":
        warnings.append("git_commit=unknown:這份凍結無法對應到任何 commit")
    if m.get("git_dirty"):
        warnings.append(
            f"凍結時工作樹有 {m.get('git_dirty_file_count')} 個未提交改動:"
            "程式碼狀態無法完整重現"
        )

    ok = not problems
    # 警告有兩種來源(git 狀態、holdout 未解析),所以標籤只說「有警告」:
    # 沿用舊的 `reliable_but_git_state_unverifiable` 會在 holdout 未解析時
    # 指著錯的原因,而誤導性的標籤比籠統的標籤更糟。細節一律看 warnings。
    reliability = ("incomplete_or_legacy" if not ok
                   else ("reliable_with_warnings" if warnings else "reliable"))
    return ManifestStatus(ok, reliability, problems, warnings, spec, missing)


def apply_rules(m: Dict[str, Any]) -> StrategySpec:
    """把凍結規則寫回 config(in-process,不改檔),回傳凍結的策略規格。

    unknown key 一律 raise:舊版是 `if hasattr(config, k)` 靜默略過,等於 config
    改名之後那個凍結值就再也沒被套用,而 forward 仍會宣稱自己跑的是凍結規則。
    """
    status = validate_manifest(m)
    if not status.ok:
        raise ValueError(
            "拒絕套用不可靠的 manifest:" + status.describe()
        )
    cfg = m["rules"]["config"]
    unknown = sorted(k for k in cfg if not hasattr(config, k))
    if unknown:
        raise ValueError(
            f"[fail-closed] manifest 的 {unknown} 在現在的 config 不存在(改名/移除)"
            ":凍結值無處可套,forward 會偷偷跑另一套規則。請用當時的 commit 重跑,"
            "或開新 manifest 重新累積 forward 期。"
        )
    for k, v in cfg.items():
        setattr(config, k, json.loads(json.dumps(v)))   # deep copy
    assert status.spec is not None
    return status.spec


def run(label: str, *, strategy: str = DEFAULT_STRATEGY,
        calendar: Optional[Any] = None) -> Optional[Path]:
    """凍結並寫出 manifest。**這是 CLI 走的唯一路徑。**

    `calendar=None` 時自己去解交易日曆(`trading_calendar()`,離線只讀 TAIEX),
    因為 holdout 邊界是 manifest 的必要內容:少了它,`validate_manifest` 會判
    `ok=False`,forward 也拒用。原本 `calendar` 只是個選用關鍵字而 CLI 沒有
    對應選項,結果正式路徑產出的 manifest 一律 `resolved=False`。
    """
    if calendar is None:
        calendar = trading_calendar()
    m = build_manifest(label, strategy=strategy, calendar=calendar)
    path = manifest_path(m)
    if path.exists():
        print(f"⚠ {path.name} 已存在且不可覆寫(immutable)。"
              f"要改規則請換 --label 或等隔日再凍結。")
        return None
    status = validate_manifest(m)
    if not status.ok:
        # 自己剛組出來的 manifest 就不完整 = 程式碼有問題,不可寫出去騙後人。
        raise RuntimeError("build_manifest 產出的 manifest 未通過驗證:"
                           + status.describe())
    path.write_text(json.dumps(m, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8")
    print(f"已凍結規則 manifest(label={m['label']}, strategy={m['strategy_name']}, "
          f"hash={m['rules_sha256_16']})")
    print(f"  config 參數 {m['frozen_config_key_count']} 個 + 策略規格 "
          f"{len(m['rules']['strategy']['signal'])} 訊號 / "
          f"{len(m['rules']['strategy']['portfolio'])} 投組")
    print(f"  資料快照@凍結 = {m['data_snapshot_at_freeze']}｜"
          f"git = {m['git_commit'][:10]}(dirty={m['git_dirty']})")
    # 走到這裡 holdout 一定是 resolved 的:未解析的 manifest 在上面的
    # validate_manifest 就被擋掉了(不再有「只釘住規則」的產出)。
    h = m["holdout"]
    print(f"  holdout 邊界:IS {h['is_window'][0]}~{h['is_window'][1]}｜"
          f"embargo {h['embargo_trading_days']} 交易日｜"
          f"OS {h['os_window'][0]}~{h['os_window'][1]}｜"
          f"揭露紀錄 {holdout_ledger.LEDGER_NAME}")
    print(f"  → {path}")
    if status.warnings:
        print("  ⚠ " + "；".join(status.warnings))
    print("  之後:推進 SNAPSHOT_END_DATE 抓新資料,再跑 forward_test.py 做真 OOS。")
    return path


def main(argv: Optional[List[str]] = None) -> Optional[Path]:
    """CLI 入口(抽成函式,測試才能釘住「CLI 預設路徑產出的 manifest」)。"""
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="baseline")
    ap.add_argument("--strategy", default=DEFAULT_STRATEGY,
                    choices=sorted(KNOWN_STRATEGIES))
    args = ap.parse_args(argv)
    return run(args.label, strategy=args.strategy)


if __name__ == "__main__":
    main()
