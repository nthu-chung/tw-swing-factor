# -*- coding: utf-8 -*-
"""panel 稠密度標籤與 `ts_`／rolling 的 fail-closed 閘門(管線不變式 3 的強制點)。

為什麼需要一個「標籤」而不是只靠註解
------------------------------------
因子層是 **long panel**(每列一個 `(date, stock_id)`),不是 wide 矩陣(日期是
index)。在 wide 格式下「20 期」必然是 20 個交易日;在 long 格式下 `rolling(20)`
算的是**20 列** —— 若 panel 只留下動態 universe 的成員日,一檔間歇進出 universe
的股票,那 20 列會橫跨 60+ 個日曆日,所有 `ts_`／rolling 因子全面失真
(AGENTS.md 陷阱 1)。對齊責任落在寫程式的人身上,而註解攔不住手滑。

所以建 panel 的地方(`backtest._prepare_panel` / `live_signal.build_light_panel`)
一律把稠密度戳進 `DataFrame.attrs`,再由這裡提供:

- `tag()`        建 panel 的人宣告稠密度(唯一寫入點)。
- `require_dense()` 要做 `ts_`／rolling 的人在算之前 fail-closed 檢查。
- `PanelOps` 的 `ts_*` 會自動呼叫這道檢查(見 `factor_engine/operators.py`)。

標籤是「best effort 的安全網」,不是可信憑證:`attrs` 會隨 copy／布林遮罩／取單欄
傳播,但 `merge` 之後會消失。所以規則刻意設計成**只在明確標為 members_only 時才
擋**(標籤消失 = 未知 = 放行),否則手寫測試 panel 與既有研究腳本會被誤殺,而
誤殺會逼人把閘門關掉 —— 那比沒有閘門更糟。真正的預設安全來自公開入口
`backtest.build_research_panel()`(預設稠密),這裡是第二道防線。
"""
from __future__ import annotations

from typing import Any, Optional

# attrs 的 key。名稱進 summary/provenance 時也用同一個字,避免兩套講法。
ATTR = "panel_density"

DENSE = "dense"                  # 每檔股票保留完整交易日序列(可安全做 ts_/rolling)
MEMBERS_ONLY = "members_only"    # 只留動態 universe 成員日 → 只能做當日橫斷面統計

_VALID = (DENSE, MEMBERS_ONLY)


def tag(obj: Any, density: str) -> Any:
    """在 panel(或任何有 `attrs` 的 pandas 物件)上標記稠密度,回傳同一個物件。"""
    if density not in _VALID:
        raise ValueError(f"未知的 panel 稠密度 {density!r};只接受 {_VALID}")
    attrs = getattr(obj, "attrs", None)
    if attrs is None:                      # 不是 pandas 物件就當作沒有標籤可放
        return obj
    obj.attrs[ATTR] = density
    return obj


def preserving_merge(left: Any, right: Any, **kwargs) -> Any:
    """`left.merge(right, ...)` 但把左邊的稠密度標籤接回去。

    為什麼需要:pandas 的 `attrs` 在 pickle/copy/concat/sort_values/reset_index/
    取單欄/assign 都會保留,**但 merge 一定丟失**。稠密度閘門對「無標籤」是
    fail-open(見模組 docstring 的理由),所以一個忘了補標的 merge 就等於閘門
    靜默消失 —— 而補標目前是慣例、不是強制。

    這個包裝把「記得補標」從人的紀律變成呼叫一個函式。已知的 merge 站點都應該
    改用它;`tests/test_dense_panel_factors.py` 會擋住 strategies/ 與
    rotation_research 直接呼叫 `DataFrame.merge`。
    """
    out = left.merge(right, **kwargs)
    density = density_of(left)
    return out if density is None else tag(out, density)


def density_of(obj: Any) -> Optional[str]:
    """讀出稠密度標籤;沒有標籤或標籤壞掉都回 None(= 未知)。"""
    attrs = getattr(obj, "attrs", None)
    if not isinstance(attrs, dict):
        return None
    value = attrs.get(ATTR)
    return value if value in _VALID else None


def is_members_only(obj: Any) -> bool:
    """是否**明確**標成只留成員日的稀疏 panel(未知一律回 False)。"""
    return density_of(obj) == MEMBERS_ONLY


def require_dense(obj: Any, *, who: str,
                  what: str = "ts_／rolling 因子") -> None:
    """稀疏 panel 上要算時序因子 → 直接 raise,不讓失真的因子值流出去。

    `who` 寫呼叫端(模組.函式),`what` 寫要算什麼,讓錯誤訊息可以直接動手修。
    """
    require_dense_density(density_of(obj), who=who, what=what)


def require_dense_density(density: Optional[str], *, who: str,
                          what: str = "ts_／rolling 因子") -> None:
    """同 `require_dense`,但直接吃稠密度字串。

    給已經把標籤讀進自己狀態的呼叫端用(例如 `PanelOps.__init__` 必須在
    `pd.to_datetime` 之前先讀,之後手上就只剩字串了)。
    """
    if density != MEMBERS_ONLY:
        return
    raise ValueError(
        f"[fail-closed] {who}:不可在只留動態 universe 成員日的稀疏 panel 上算 {what}。\n"
        "  為什麼:long panel 的 rolling(20) 算的是「20 列」;一檔間歇進出 universe 的\n"
        "  股票,那 20 列會橫跨 60+ 個日曆日(AGENTS.md 陷阱 1),因子值直接失真。\n"
        "  正確做法:用 backtest.build_research_panel(...)(預設稠密)算因子,\n"
        "  成員資格過濾留到選股階段才套 in_dynamic_universe。\n"
        "  只做當日橫斷面統計(IC/分位)才可以用 members_only=True 的 panel。"
    )
