# -*- coding: utf-8 -*-
"""
選股池（universe）載入與 pre-filter。

- get_universe(sample=True)：快速原型用小集合（config.SAMPLE_UNIVERSE）。
- get_universe(sample=False)：全市場上市櫃普通股（去除 ETF / 金融，視 config）。
- apply_prefilter()：套用流動性 / 產業 / ETF 排除。
"""

from __future__ import annotations

from typing import List, Dict

import pandas as pd

import config
import data
import security_type


# ── 未來池逃生門的紀錄簿 ────────────────────────────────────────────────
# 2026-08-15:`SWING_ALLOW_FUTURE_POOL=1` 以前**只 print 一行**就放行。價格逃生門
# (`ALLOW_UNADJUSTED_BACKTEST`)至少會在 summary 戳 `integrity_bypassed=True`,
# 未來池逃生門卻不留任何痕跡 —— 於是「用晚於快照的排名池回套歷史」產生的績效,
# 存進 outputs/ 之後就跟乾淨結果長得一模一樣,只有當初盯著 stdout 的人知道。
# 這裡把放行事件記進 process 級紀錄簿,回測 summary 會把它寫進 `universe` 欄位。
_FUTURE_POOL_BYPASSES: List[Dict] = []


def future_pool_bypass_log() -> List[Dict]:
    """本 process 內被 `SWING_ALLOW_FUTURE_POOL=1` 放行過的未來池。"""
    return [dict(e) for e in _FUTURE_POOL_BYPASSES]


def reset_future_pool_bypass_log() -> None:
    """清空紀錄簿(測試用;正式流程一個 process = 一次研究執行,不該清)。"""
    _FUTURE_POOL_BYPASSES.clear()


def _record_future_pool_bypass(pool_asof: str, snap: str, top_n: int) -> None:
    _FUTURE_POOL_BYPASSES.append({
        "pool_top_n": int(top_n),
        "pool_asof": str(pool_asof),
        "snapshot_end": str(snap),
        "detected_by": "universe.get_universe",
    })


def _assert_universe_pit(pool_asof: str, top_n: int) -> None:
    """候選池 PIT 檢查:池的建構日不得晚於資料快照,否則 = 未來池 look-ahead。

    build_universe 打 openapi 只能取『當日』全市場,故池永遠是建構日當下的存活+熱門股。
    若池建於晚於 SNAPSHOT_END_DATE(例如推進快照做研究、卻沒重建池,或反之用了較新的池),
    等於用未來的成交值排名/存活性回套過去 → 選股 look-ahead。這裡 fail-closed 擋下。
    SWING_ALLOW_FUTURE_POOL=1 可顯式放行(研究/debug 用,結果不可當已驗證)。
    無 provenance(舊池無 as_of)時只警告,無法驗證 PIT。
    """
    snap = getattr(config, "SNAPSHOT_END_DATE", "").strip()
    if not snap:
        return                                   # live 模式不檢查
    if not pool_asof:
        print(f"[universe] ⚠ top{top_n} 候選池無建構日(as_of)provenance,無法驗證 PIT;"
              f"請以新版 build_universe.py 重建以取得 as_of 戳。")
        return
    if pool_asof > snap:
        if getattr(config, "ALLOW_FUTURE_POOL", False):
            # 只 print 不夠:結果落到 outputs/ 之後那行警告就消失了。記進紀錄簿,
            # 讓 backtest summary 的 `future_pool_bypassed` 帶著它一起留存。
            _record_future_pool_bypass(pool_asof, snap, top_n)
            print(f"[universe] ⚠ 候選池建於 {pool_asof} 晚於快照 {snap}(未來池 look-ahead),"
                  f"SWING_ALLOW_FUTURE_POOL=1 已放行——結果含選股前視,不可當已驗證。")
            return
        raise RuntimeError(
            f"[fail-closed] top{top_n} 候選池建於 {pool_asof},晚於資料快照 {snap} → "
            f"用未來的成交值排名/存活性回套過去 = 選股 look-ahead。\n"
            f"  解法:(a) 用 SWING_SNAPSHOT_END >= {pool_asof} 的快照;或 (b) 以 <= {snap} 的日期"
            f"重建候選池;或 (c) 顯式 SWING_ALLOW_FUTURE_POOL=1(結果不可當已驗證)。"
        )


def _is_normal_stock(stock_id: str, market_type: str, industry: str,
                     name: str) -> bool:
    """上市／上櫃**普通股**才放行(證券別白名單,不是代號規則)。

    原 bug(2026-08-15 修):這個函式收了 `market_type` 參數卻**完全沒用它**,
    實際只檢查「4 碼數字且不以 00 開頭」。結果 TaiwanStockInfo 的 541 檔
    `type=emerging`(興櫃)有 381 檔通過,連同 11 檔存託憑證(DR,代號 91xx
    同樣是 4 碼)與創新板一起混進全市場 universe。興櫃沒有 ±10% 漲跌停
    (2026-05 實測單日 |ret|>10.5% 佔比:上市 0.034% / 上櫃 0.042% / 興櫃 3.872%,
    最大單日 +57.17%),而動能因子找的正是那種標的 —— 偏誤方向是系統性灌高 Sharpe。

    判定本身放在 `security_type`(三個池建構點共用同一份),這裡只是薄轉發。
    """
    return not security_type.classify(stock_id, market_type, industry, name)


def get_universe(sample: bool = True, top_n: int = None) -> List[str]:
    """
    回傳股票代號清單（已去重）。
    - sample=True：小集合（config.SAMPLE_UNIVERSE）。
    - top_n 指定（如 100）：讀 build_universe.py 產生的「成交值前 N 大」池。
    - 否則：全市場上市櫃普通股。
    """
    if top_n:
        # 這條路只是**讀回**已建好的池檔;證券別過濾在建構端
        # (`build_universe.build`)就做掉了,不在讀取端重做 —— 讀取端再篩一次會讓
        # 「池檔內容」與「實際用的池」不一致,as_of provenance 也就對不上。
        import build_universe
        try:
            ids = build_universe.load(top_n)
            asof = build_universe.load_asof(top_n)
        except Exception as e:
            print(f"[universe] 載入 top{top_n} 失敗：{e}")
            ids, asof = [], None
        if ids:
            _assert_universe_pit(asof, top_n)   # PIT 違規(未來池)直接 raise,不吞
            seen, out = set(), []
            for s in ids:
                if s not in seen:
                    seen.add(s); out.append(s)
            return out
        raise FileNotFoundError(
            f"找不到或無法載入 top{top_n} 候選池；請先跑 "
            f"`.venv/bin/python build_universe.py {top_n}`。拒絕降級成 sample universe。"
        )

    if sample:
        seen, out = set(), []
        for s in config.SAMPLE_UNIVERSE:
            if s not in seen:
                seen.add(s)
                out.append(s)
        return out

    info = data.fetch_stock_info()
    if info.empty:
        raise RuntimeError("無法取得全市場股票清單；拒絕降級成 sample universe")

    # 證券別過濾是 fail-closed 的:興櫃/DR/創新板/ETF 一律擋掉,判不出證券別的
    # 直接 raise(缺 market_type 就當可交易 = 原 bug 的另一種形態)。
    eligible = set(security_type.filter_stock_info(
        info, source="universe.get_universe"))
    industry_by_id = {
        str(r.get("stock_id", "")).strip(): str(r.get("industry", "")).strip()
        for _, r in info.iterrows()
    }
    out = []
    for sid in eligible:
        industry = industry_by_id.get(sid, "")
        if config.EXCLUDE_FINANCE and ("金融" in industry or "金control" in industry):
            continue
        out.append(sid)
    return sorted(set(out))


def get_research_candidates(universe_top_n: int = None,
                            candidate_pool_n: int = None) -> List[str]:
    """legacy 單一日期候選池,**只能當顯式對照組**。

    ⚠ 這不是 PIT 候選池,也不是 PIT 的替代品。它讀的是 `outputs/universe_top*.json`
    ——「某一天」的成交值排名。把它回套整段歷史 = 用今天知道誰熱門去決定兩年前能
    選誰(AGENTS.md 陷阱 4),而且該檔存活的股票本身就有存活者偏誤。

    正式歷史策略請改用:

        from universes import historical_pit_universe
        pit = historical_pit_universe()          # 月頻 PIT:M 月只用完整 M-1 曆月

    仍要用這個靜態池做對照時,呼叫引擎時必須顯式帶 `static_universe_comparator=True`,
    結果才會被標成 `formal_evidence_eligible=False`(不可作正式證據)。

    ``universe_top_n`` 是每日目標檔數;dynamic 模式下候選池必須比它寬。
    """
    target = universe_top_n or config.DYNAMIC_UNIVERSE_TOP_N
    if not config.DYNAMIC_UNIVERSE_ENABLED:
        return get_universe(top_n=target)

    pool = candidate_pool_n or config.DYNAMIC_UNIVERSE_CANDIDATE_POOL
    if pool < target:
        raise ValueError(
            f"動態 universe 候選池({pool})不可小於每日目標({target})"
        )
    ids = get_universe(top_n=pool)
    if len(ids) < target:
        raise ValueError(
            f"候選池只有 {len(ids)} 檔，少於動態 universe 目標 {target}；"
            f"請先跑 `.venv/bin/python build_universe.py {pool}`"
        )
    return ids


def get_industry_map() -> Dict[str, str]:
    """stock_id -> 產業別。"""
    info = data.fetch_stock_info()
    if info.empty:
        return {}
    return {str(r["stock_id"]).strip(): str(r.get("industry", "")).strip()
            for _, r in info.iterrows()}


def get_name_map() -> Dict[str, str]:
    """stock_id -> 名稱。"""
    info = data.fetch_stock_info()
    if info.empty:
        return {}
    return {str(r["stock_id"]).strip(): str(r.get("name", "")).strip()
            for _, r in info.iterrows()}


def passes_liquidity(price_df: pd.DataFrame) -> bool:
    """近 20 日均量（張）是否達門檻。volume 單位為股，/1000 = 張。"""
    if price_df is None or price_df.empty or "volume" not in price_df.columns:
        return False
    recent = price_df.tail(20)
    avg_lots = recent["volume"].mean() / 1000.0
    return avg_lots >= config.MIN_AVG_VOLUME_LOTS


if __name__ == "__main__":
    u = get_universe(sample=True)
    print(f"sample universe: {len(u)} 檔 -> {u}")
