# -*- coding: utf-8 -*-
"""證券別白名單:「哪些證券可以進候選池」的**單一判定**。

原 bug(2026-08-15 修)
----------------------
`universe._is_normal_stock(stock_id, market_type)` 收了 `market_type` 參數,
函式體卻**完全沒用它** —— 實際只檢查「4 碼數字且不以 00 開頭」。於是:

  - TaiwanStockInfo 全表 541 檔 `type=emerging`(**興櫃**),其中 381 檔通過該過濾
    (以 repo 快取 `_cache/info__ALL__2026-08-06.pkl` 重現);
  - 同一份表另有 11 檔存託憑證(DR,代號 91xx 也是 4 碼)與 29 檔創新板通過。

修正後的實測差異(同一份快取,舊規則 vs 本模組):

  | 證券別來源 | 舊規則通過 | 本模組擋掉 | 明細 |
  |---|---|---|---|
  | info 2026-06-22(凍結快照) | 2509 | 408 | 興櫃 369 / 創新板 28 / DR 11 |
  | info 2026-08-06 | 2527 | 421 | 興櫃 381 / 創新板 29 / DR 11 |
  | PIT 逐日快照(<= 2026-06-22) | 1988 | 33 | 創新板 28 / DR 4 / 興櫃 1 |

  legacy 單日池 `outputs/universe_top100.json` 也真的含 1 檔創新板
  (7610 聯友金屬-創)—— 這不是理論上的洩漏。

為什麼這會系統性灌高 Sharpe:**興櫃沒有 ±10% 漲跌停**。2026-05 實測單日
|ret| > 10.5% 的比例,上市 0.034%、上櫃 0.042%、興櫃 **3.872%**(約 100 倍),
興櫃最大單日 +57.17%(6775 穎台科技 2026-05-12)、最小 -24.90%。而動能因子找的
正是「一天漲 40~57%」這種標的 —— 偏誤方向不是隨機的,是往「策略看起來更好」那邊。
流動性也擋不住:2026-05 有 339 檔興櫃真的有成交(合計日均成交值 136.8 億),
最大一檔 3595 山太士日均成交值 14.75 億、全市場 ADV 排名 **#188**,直接落在
`DYNAMIC_UNIVERSE_CANDIDATE_POOL=300` 之內。

判定規則(白名單,不是代號規則)
--------------------------------
1. 市場別必須是 `twse`(上市)或 `tpex`(上櫃);`emerging`(興櫃)明確排除。
2. 產業別不得落在非普通股清單(ETF / ETN / 指數 / 受益證券 / 存託憑證 /
   創新板)。這些在 TaiwanStockInfo 是用 `industry_category` 表達的,
   `type` 一律只有 twse/tpex/emerging 三種,所以**只看 type 不夠**。
3. 簡稱後綴:`-創`(創新板)與 `-DR`(存託憑證)。為什麼需要這條 ——
   `industry_category` 對創新板**不可靠**:實測 29 檔簡稱帶「-創」的股票只有 3 檔
   被標成 `創新板股票`,而且同一檔在不同快照會被改分類(4590 富田-創 從
   `創新板股票` 變成 `電機機械`)。只靠產業別會漏掉九成創新板。
4. 代號形狀 4 碼數字、非 00 開頭 —— 這條只用來擋特別股(2881A)、CB(五碼)、
   權證(六碼)這類 `industry_category` 仍寫著正常產業的證券,**不是**證券別判定
   本身(興櫃與 DR 的代號同樣是 4 碼數字,靠代號永遠分不出來)。
5. 產業別不在已知普通股清單裡 → 視為「不知道」而**不是**放行(見 fail-closed)。

限制:TaiwanStockInfo 的證券別是**當下狀態**,不是 point-in-time。一檔 2024 年
還在興櫃、2026 年轉上市的股票,現在查到的是 `twse` —— 這層過濾擋得住「今天仍是
興櫃」的股票,擋不住「當時是興櫃、現在已上市」的歷史列。PIT 池的實際保護來自
資料源本身(TWSE/TPEx 日行情端點不含興櫃);全市場 FinMind 路徑則靠這裡。

fail-closed
-----------
證券別資訊缺失(不在 TaiwanStockInfo、`type`/`industry_category` 空白、或出現
沒見過的產業別)時**不得預設放行** —— 「缺 market_type 就當可交易」正是原 bug
的另一種形態。呼叫端用 `on_unknown` 明確選擇:

  - `"raise"`(預設):丟 `SecurityTypeError`,把決定權交回人。
  - `"exclude"`:排除並記數(給不能中斷的 live 工具用)。

沒有 `"allow"`。

排除統計
--------
「這份績效用的是哪一種池」必須看得出來,而不是靠記得當時跑的是修正前還是修正後
的程式碼 —— 所以排除紀錄要進回測 summary 的 `universe.excluded_by_security_type`。

原 bug(2026-08-15 修,第二輪):統計原本只有一本 **module 級全域** 紀錄簿
`_EXCLUSIONS`,`reset_exclusion_log()` 的說明還寫著「一個 process = 一次研究執行,
不該清」。那個假設對兩種真實用法都是錯的:

  - **同一 process 連續跑兩次回測**:第二次的 summary 會把第一次擋掉的證券
    一起算進去(實測連續呼叫兩次 `backtest_portfolio`,第二次的
    `excluded_by_security_type.total` 仍含第一次的紀錄);
  - **平行 GA / 參數搜尋**:多個 candidate 共用同一本紀錄簿,誰的統計是誰的
    分不出來(= `CROSS_SECTIONAL_STRATEGY_RESEARCH_SPEC.md` §14 攻擊 16 的
    「平行 search 透過全域狀態互相污染」)。

修法與 §5.7「引擎應使用 immutable request,不得靠暫時改寫全域狀態傳遞參數」
同一條原則:排除統計改成**每次 backtest request 自己的 `ExclusionCollector`**
(隨 request 建立、隨結果回傳)。`exclusion_scope()` 把 collector 掛到
`contextvars`(每個 thread / asyncio task 各自一份,平行搜尋天然隔離),
`record_exclusion()` 只往「當下這個 request 的 collector」寫。

`_EXCLUSIONS` 全域紀錄簿保留,但降級成**純觀察用途**(想知道整個 process 到目前
為止擋過什麼時可以看),**不得**再當成任何 summary 數字的來源。
"""

from __future__ import annotations

import contextlib
import contextvars
from typing import (Any, Dict, Iterable, Iterator, List, Mapping, Optional,
                    Sequence, Tuple)

import pandas as pd


class SecurityTypeError(RuntimeError):
    """證券別無法判定時的 fail-closed 例外。"""


# ── 排除理由 ──────────────────────────────────────────────────────────────
REASON_EMERGING = "emerging_board"              # 興櫃:無漲跌停、流動性斷續
REASON_UNLISTED_MARKET = "not_listed_market"    # 既非上市也非上櫃
REASON_ETF = "etf"
REASON_ETN = "etn"
REASON_INDEX = "index_or_aggregate"             # 指數/大盤/所有證券等統計列
REASON_BENEFICIARY = "beneficiary_certificate"  # 受益證券
REASON_DR = "depositary_receipt"                # 存託憑證(TDR)
REASON_INNOVATION_BOARD = "innovation_board"    # 創新板
REASON_CODE_SHAPE = "non_common_code_shape"     # 特別股/CB/權證
REASON_NOT_IN_REGISTRY = "not_in_stock_info"    # 查不到證券別(可能已下市)
REASON_MISSING_FIELDS = "missing_security_type" # 有列但欄位空白
REASON_UNKNOWN_INDUSTRY = "unrecognized_industry"  # 沒見過的產業別

#: 這幾種理由代表「我們不知道」,由 `on_unknown` 決定 raise 還是排除。
UNKNOWN_REASONS = frozenset({
    REASON_NOT_IN_REGISTRY, REASON_MISSING_FIELDS, REASON_UNKNOWN_INDUSTRY,
})

#: TaiwanStockInfo 的 `type` 只有這三種值;前兩種才是可交易的集中/店頭市場。
LISTED_MARKET_TYPES = frozenset({"twse", "tpex"})
EXCLUDED_MARKET_TYPES: Dict[str, str] = {"emerging": REASON_EMERGING}

#: 非普通股的 `industry_category`(twse 與 tpex 的寫法不同,兩種都要列)。
NON_COMMON_INDUSTRIES: Dict[str, str] = {
    "ETF": REASON_ETF,
    "上櫃ETF": REASON_ETF,
    "上櫃指數股票型基金(ETF)": REASON_ETF,
    "ETN": REASON_ETN,
    "指數投資證券(ETN)": REASON_ETN,
    "Index": REASON_INDEX,
    "大盤": REASON_INDEX,
    "所有證券": REASON_INDEX,
    "受益證券": REASON_BENEFICIARY,
    "存託憑證": REASON_DR,
    "創新板股票": REASON_INNOVATION_BOARD,
    "創新版股票": REASON_INNOVATION_BOARD,   # FinMind 兩種寫法都出現過,不是筆誤
}

#: 已知的普通股產業別。刻意用白名單:FinMind 新增一種非普通股分類時,
#: 白名單會讓它落到 `REASON_UNKNOWN_INDUSTRY`(fail-closed),黑名單則會靜默放行。
#: 來源:`_cache/info__ALL__*.pkl` 與 TaiwanStockInfo 全表的 `industry_category` 聯集。
COMMON_STOCK_INDUSTRIES = frozenset({
    "光電業", "其他", "其他電子業", "其他電子類", "化學工業", "化學生技醫療",
    "半導體業", "塑膠工業", "居家生活", "居家生活類", "建材營造", "數位雲端",
    "數位雲端類", "文化創意業", "橡膠工業", "水泥工業", "汽車工業", "油電燃氣業",
    "玻璃陶瓷", "生技醫療業", "紡織纖維", "綠能環保", "綠能環保類", "航運業",
    "觀光事業", "觀光餐旅", "貿易百貨", "資訊服務業", "農業科技", "農業科技業",
    "通信網路業", "造紙工業", "運動休閒", "運動休閒類", "金融保險", "金融業",
    "鋼鐵工業", "電器電纜", "電子商務業", "電子工業", "電子通路業", "電子零組件業",
    "電機機械", "電腦及週邊設備業", "食品工業",
})

_ON_UNKNOWN_CHOICES = ("raise", "exclude")

#: registry:stock_id -> (market_type, industry, name)
Registry = Mapping[str, Tuple[str, str, str]]


def _text(value: Any) -> str:
    """把 None / NaN / 'nan' 統一成空字串(空字串 = 沒有資訊,不是「否」)。"""
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    s = str(value).strip()
    return "" if s.lower() in {"nan", "none", "<na>"} else s


def is_plausible_equity_code(code: Any) -> bool:
    """代號形狀是否像普通股(4 碼數字、非 00 開頭)。

    **這不是證券別判定**:興櫃與 DR 的代號同樣是 4 碼數字。只拿來擋特別股
    (2881A)、CB(五碼)、權證(六碼)這類產業別欄位仍是正常產業的證券,
    以及在只有代號、沒有證券別欄位的交易所快照上做形狀前篩。
    """
    c = _text(code)
    return len(c) == 4 and c.isdigit() and not c.startswith("00")


def is_non_common_industry(industry: Any) -> bool:
    """產業別是否屬於已知的非普通股分類(ETF/ETN/DR/受益證券/創新板/指數)。"""
    return _text(industry) in NON_COMMON_INDUSTRIES


def is_innovation_board_name(name: Any) -> bool:
    """簡稱是否帶創新板後綴(`-創` / `-KY創`)。

    為什麼要看名稱:TWSE 給創新板股票的簡稱固定加「-創」,但 FinMind 的
    `industry_category` **不可靠** —— 實測 29 檔帶「-創」的股票裡,只有 3 檔被標成
    `創新板股票`(7835 永悅健康-創 標的是「數位雲端」、2432 倚天酷碁-創 標的是
    「電腦及週邊設備業」),而且同一檔在不同快照會被改分類(4590 富田-創 從
    `創新板股票` 變成 `電機機械`)。只靠產業別會漏掉九成創新板。

    連字號是必要條件:群創/緯創/矽創/大統新創 這些簡稱本身以「創」結尾的普通股
    不能被誤殺。
    """
    n = _text(name)
    return n.endswith("創") and "-" in n


def is_depositary_receipt_name(name: Any) -> bool:
    """簡稱是否帶存託憑證後綴(`-DR`)。產業別若被改分類,這條仍擋得住。"""
    return _text(name).endswith("-DR")


def classify(stock_id: Any, market_type: Any, industry: Any, name: Any) -> str:
    """回傳排除理由;空字串代表**可以進池**。

    順序刻意是「市場別 → 產業別 → 簡稱後綴 → 代號形狀 → 產業白名單」:先報最
    具體的理由,才不會把 DR 記成「代號形狀不對」而讓統計看不出洩漏的是哪一類證券。
    """
    sid = _text(stock_id)
    mtype = _text(market_type).lower()
    ind = _text(industry)
    nm = _text(name)
    if not sid or not mtype or not ind or not nm:
        return REASON_MISSING_FIELDS
    if mtype in EXCLUDED_MARKET_TYPES:
        return EXCLUDED_MARKET_TYPES[mtype]
    if mtype not in LISTED_MARKET_TYPES:
        return REASON_UNLISTED_MARKET
    if ind in NON_COMMON_INDUSTRIES:
        return NON_COMMON_INDUSTRIES[ind]
    if is_innovation_board_name(nm):
        return REASON_INNOVATION_BOARD
    if is_depositary_receipt_name(nm):
        return REASON_DR
    if not is_plausible_equity_code(sid):
        return REASON_CODE_SHAPE
    if ind not in COMMON_STOCK_INDUSTRIES:
        return REASON_UNKNOWN_INDUSTRY
    return ""


# ── registry:證券別事實的來源(TaiwanStockInfo)────────────────────────────
_COLUMN_ALIASES = {
    "stock_id": ("stock_id",),
    "market_type": ("market_type", "type"),
    "industry": ("industry", "industry_category"),
    "name": ("name", "stock_name"),
}


def _pick_column(frame: pd.DataFrame, key: str) -> str:
    for name in _COLUMN_ALIASES[key]:
        if name in frame.columns:
            return name
    raise SecurityTypeError(
        f"[fail-closed] 證券別來源缺少 {key} 欄位(可接受的欄名:"
        f"{_COLUMN_ALIASES[key]});沒有證券別就不能決定誰可以進池"
    )


def build_registry(info: Optional[pd.DataFrame] = None
                   ) -> Dict[str, Tuple[str, str, str]]:
    """由 TaiwanStockInfo(或同構表)建 stock_id -> (market_type, industry, name)。

    `info=None` 時走 `data.fetch_stock_info()`(有快取;離線測試請自己傳 frame
    或用 `set_registry()`,絕不在測試裡打網路)。空表一律 raise —— 「查不到證券別」
    不可以退化成「全部放行」。
    """
    if info is None:
        import data as _data                     # 延後 import:避免與 data 互相 import
        info = _data.fetch_stock_info()
    if info is None or len(info) == 0:
        raise SecurityTypeError(
            "[fail-closed] 取不到 TaiwanStockInfo,無法判定證券別;"
            "拒絕在不知道證券別的情況下放行任何股票進池"
        )
    sid_col = _pick_column(info, "stock_id")
    mtype_col = _pick_column(info, "market_type")
    ind_col = _pick_column(info, "industry")
    name_col = _pick_column(info, "name")
    out: Dict[str, Tuple[str, str, str]] = {}
    for sid, mtype, ind, nm in zip(info[sid_col], info[mtype_col],
                                   info[ind_col], info[name_col]):
        key = _text(sid)
        if key:
            out[key] = (_text(mtype), _text(ind), _text(nm))
    return out


_REGISTRY_CACHE: Optional[Dict[str, Tuple[str, str, str]]] = None


def default_registry(refresh: bool = False) -> Dict[str, Tuple[str, str, str]]:
    """process 內共用的證券別 registry(一次執行 = 一份證券別快照)。"""
    global _REGISTRY_CACHE
    if refresh or _REGISTRY_CACHE is None:
        _REGISTRY_CACHE = build_registry()
    return _REGISTRY_CACHE


def set_registry(registry: Optional[Registry]) -> None:
    """覆寫(或以 None 清空)process 級 registry;離線測試用。"""
    global _REGISTRY_CACHE
    _REGISTRY_CACHE = None if registry is None else dict(registry)


def reset_registry() -> None:
    set_registry(None)


# ── 排除統計:結果要說得出「這份池是哪一種池」──────────────────────────────
_SAMPLE_CAP = 20
EXCLUSION_RULE_ID = "listed_common_stock_whitelist_v1"


def summarize_exclusions(rows: Sequence[Mapping[str, str]]) -> Dict[str, Any]:
    """把排除紀錄攤成 summary 用的統計。

    去重以 (stock_id, reason, source) 為單位:同一檔在同一個來源被排除幾次是
    實作細節(例如逐日快照會重複命中),要看的是「被擋掉的是哪些、哪一類」。
    """
    seen = set()
    by_reason: Dict[str, int] = {}
    by_source: Dict[str, Dict[str, int]] = {}
    samples: Dict[str, List[str]] = {}
    for e in rows:
        key = (e["stock_id"], e["reason"], e["source"])
        if key in seen:
            continue
        seen.add(key)
        by_reason[e["reason"]] = by_reason.get(e["reason"], 0) + 1
        by_source.setdefault(e["source"], {})
        by_source[e["source"]][e["reason"]] = (
            by_source[e["source"]].get(e["reason"], 0) + 1)
        bucket = samples.setdefault(e["reason"], [])
        if len(bucket) < _SAMPLE_CAP and e["stock_id"] not in bucket:
            bucket.append(e["stock_id"])
    return {
        "total": sum(by_reason.values()),
        "by_reason": dict(sorted(by_reason.items())),
        "by_source": {k: dict(sorted(v.items())) for k, v in sorted(by_source.items())},
        "sample_ids": {k: sorted(v) for k, v in sorted(samples.items())},
        "rule": EXCLUSION_RULE_ID,
    }


class ExclusionCollector:
    """一次 backtest request 自己的排除紀錄簿。

    為什麼要是物件而不是全域 list:全域紀錄簿在「同一 process 連續跑兩次回測」
    會讓第二次的 summary 含第一次的排除數(實測重現),在平行 GA 搜尋則讓每個
    candidate 的統計互相污染 —— 兩者都會讓「這份績效用的是哪一種池」這個問題
    得到錯誤答案,而錯誤方向不是隨機的(統計看起來永遠比實際更「有在擋」)。

    生命週期 = 一個 request:由 `backtest_portfolio` 建立、寫進 summary、
    隨結果回傳,不跨 request 累積。
    """

    __slots__ = ("_rows", "label")

    def __init__(self, label: str = "") -> None:
        self._rows: List[Dict[str, str]] = []
        self.label = str(label)

    def record(self, stock_id: Any, reason: str, source: str) -> None:
        self._rows.append({"stock_id": str(stock_id), "reason": str(reason),
                           "source": str(source)})

    def log(self) -> List[Dict[str, str]]:
        return [dict(e) for e in self._rows]

    def summary(self) -> Dict[str, Any]:
        return summarize_exclusions(self._rows)

    def __len__(self) -> int:
        return len(self._rows)

    def __repr__(self) -> str:                              # pragma: no cover
        return f"<ExclusionCollector label={self.label!r} n={len(self._rows)}>"


#: 「當下這個 request 的 collector」。用 contextvars 而不是 module 變數:
#: 每個 thread / asyncio task 自帶一份,平行搜尋不會互相看見對方的紀錄。
_ACTIVE_COLLECTOR: "contextvars.ContextVar[Optional[ExclusionCollector]]" = (
    contextvars.ContextVar("security_type_exclusion_collector", default=None))


def active_collector() -> Optional[ExclusionCollector]:
    """當下 request 的 collector;沒有開 scope 時回 None(不自己造一個)。"""
    return _ACTIVE_COLLECTOR.get()


@contextlib.contextmanager
def exclusion_scope(collector: Optional[ExclusionCollector] = None,
                    *, label: str = "") -> Iterator[ExclusionCollector]:
    """把一段執行(池建構 + 回測)的排除紀錄收進同一本 request 級紀錄簿。

    典型用法(池建構與回測要算同一個 request 才看得到完整統計):

        with security_type.exclusion_scope() as coll:
            symbols = uni.get_universe()          # 這裡擋掉的也算這個 request
            res = backtest.backtest_portfolio(symbols=symbols, ...)
        res["summary"]["universe"]["excluded_by_security_type"]  # 只含本次

    `backtest_portfolio` 沒有被 scope 包住時會自己開一個 —— 沒有 scope 的呼叫
    絕不會退回全域紀錄簿(那正是跨回測污染的來源)。
    """
    coll = ExclusionCollector(label=label) if collector is None else collector
    token = _ACTIVE_COLLECTOR.set(coll)
    try:
        yield coll
    finally:
        _ACTIVE_COLLECTOR.reset(token)


#: process 級觀察用紀錄簿。**只給人工觀察**(「這個 process 到目前為止擋過什麼」),
#: 不得成為任何 summary 數字的來源 —— 它會跨 request 累積,這正是原 bug。
_EXCLUSIONS: List[Dict[str, str]] = []


def reset_exclusion_log() -> None:
    """清空 process 級**觀察用**紀錄簿。

    注意:summary 的數字來自 request 級 `ExclusionCollector`,不是這裡,所以
    忘了清這本不會再污染任何結果。
    """
    _EXCLUSIONS.clear()


def record_exclusion(stock_id: Any, reason: str, source: str, *,
                     collector: Optional[ExclusionCollector] = None) -> None:
    """記一筆排除:寫進 request 級 collector(顯式 > 當下 scope),並留一份觀察用。"""
    target = collector if collector is not None else _ACTIVE_COLLECTOR.get()
    if target is not None:
        target.record(stock_id, reason, source)
    _EXCLUSIONS.append({"stock_id": str(stock_id), "reason": str(reason),
                        "source": str(source)})


def exclusion_log() -> List[Dict[str, str]]:
    """process 級觀察用紀錄簿的內容(非 summary 來源)。"""
    return [dict(e) for e in _EXCLUSIONS]


def exclusion_summary() -> Dict[str, Any]:
    """process 級觀察用統計。

    **不要**拿這個當回測 summary 的 `excluded_by_security_type` —— 它跨 request
    累積,連續跑兩次回測時第二次會含第一次的數字。summary 請用該次 request 的
    `ExclusionCollector.summary()`。
    """
    return summarize_exclusions(_EXCLUSIONS)


# ── 對外的過濾入口 ────────────────────────────────────────────────────────
def _check_on_unknown(on_unknown: str) -> str:
    if on_unknown not in _ON_UNKNOWN_CHOICES:
        raise ValueError(
            f"on_unknown 只接受 {_ON_UNKNOWN_CHOICES};刻意沒有 'allow' —— "
            f"「缺證券別就放行」正是這支模組要修掉的 bug"
        )
    return on_unknown


def _raise_unknown(unknown: List[Tuple[str, str]], source: str) -> None:
    sample = ", ".join(f"{sid}({reason})" for sid, reason in unknown[:10])
    raise SecurityTypeError(
        f"[fail-closed] {source}:{len(unknown)} 檔無法判定證券別(例:{sample})。\n"
        f"  可能原因:代號不在 TaiwanStockInfo(已下市?)、type/industry_category "
        f"空白,或出現尚未分類的產業別。\n"
        f"  解法:(a) 更新 TaiwanStockInfo 快取或補上該檔證券別;"
        f"(b) 確認後把新產業別加進 security_type.COMMON_STOCK_INDUSTRIES /"
        f" NON_COMMON_INDUSTRIES;(c) 呼叫端顯式帶 on_unknown='exclude'"
        f"(會連可能是普通股的下市股一起排掉 —— 對 PIT 池等於重新引入倖存者偏誤)。"
    )


def eligibility(stock_id: Any, registry: Optional[Registry] = None) -> str:
    """單檔判定:回傳排除理由,空字串 = 可進池。"""
    reg = default_registry() if registry is None else registry
    sid = _text(stock_id)
    facts = reg.get(sid)
    if facts is None:
        return REASON_NOT_IN_REGISTRY
    return classify(sid, facts[0], facts[1], facts[2])


def filter_ids(ids: Iterable[Any], *, registry: Optional[Registry] = None,
               source: str, on_unknown: str = "raise",
               collector: Optional[ExclusionCollector] = None) -> List[str]:
    """過濾一組代號,保留可進池的普通股(順序不變、去重)。

    這是四個進池點(`universe` / `pit_universe` / `current_watchlist` /
    回測引擎的外部 picks 閘門)共用的唯一實作 —— 「哪些證券可以進池」只能有
    一個答案。

    `collector`:把排除記進指定的 request 級紀錄簿;不給就記進當下
    `exclusion_scope()` 的那一本。
    """
    _check_on_unknown(on_unknown)
    reg = default_registry() if registry is None else registry
    kept: List[str] = []
    seen = set()
    unknown: List[Tuple[str, str]] = []
    for raw in ids:
        sid = _text(raw)
        if not sid or sid in seen:
            continue
        seen.add(sid)
        reason = eligibility(sid, reg)
        if not reason:
            kept.append(sid)
            continue
        if reason in UNKNOWN_REASONS:
            unknown.append((sid, reason))
        record_exclusion(sid, reason, source, collector=collector)
    if unknown and on_unknown == "raise":
        _raise_unknown(unknown, source)
    return kept


def filter_stock_info(info: pd.DataFrame, *, source: str,
                      on_unknown: str = "raise",
                      collector: Optional[ExclusionCollector] = None) -> List[str]:
    """直接對 TaiwanStockInfo 表過濾,回傳可進池的代號(排序、去重)。"""
    registry = build_registry(info)
    kept = filter_ids(registry.keys(), registry=registry, source=source,
                      on_unknown=on_unknown, collector=collector)
    return sorted(kept)


def eligible_mask(stock_ids: pd.Series, *, registry: Optional[Registry] = None,
                  source: str, on_unknown: str = "raise",
                  collector: Optional[ExclusionCollector] = None) -> pd.Series:
    """pandas 版:回傳與輸入同 index 的布林遮罩(給逐列表格用)。"""
    _check_on_unknown(on_unknown)
    reg = default_registry() if registry is None else registry
    ids = stock_ids.astype(str).str.strip()
    reasons = {sid: eligibility(sid, reg) for sid in dict.fromkeys(ids)}
    unknown = [(sid, r) for sid, r in reasons.items() if r in UNKNOWN_REASONS]
    for sid, reason in reasons.items():
        if reason:
            record_exclusion(sid, reason, source, collector=collector)
    if unknown and on_unknown == "raise":
        _raise_unknown(unknown, source)
    return ids.map(lambda s: not reasons.get(s, REASON_NOT_IN_REGISTRY))


def filter_frame(frame: pd.DataFrame, *, source: str,
                 id_column: str = "stock_id",
                 registry: Optional[Registry] = None,
                 on_unknown: str = "raise",
                 collector: Optional[ExclusionCollector] = None) -> pd.DataFrame:
    """過濾逐列表格(交易所快照 / 行情表),只留可進池的普通股。"""
    if frame is None or len(frame) == 0:
        return frame
    if id_column not in frame.columns:
        raise SecurityTypeError(
            f"[fail-closed] {source}:表格沒有 {id_column} 欄,無法判定證券別")
    mask = eligible_mask(frame[id_column], registry=registry, source=source,
                         on_unknown=on_unknown, collector=collector)
    return frame[mask.to_numpy()].reset_index(drop=True)
