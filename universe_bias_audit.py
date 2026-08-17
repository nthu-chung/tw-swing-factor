# -*- coding: utf-8 -*-
"""
Universe 倖存者/前視偏誤量化（上界）
====================================
回答:目前回測用的候選池是「今天的成交值 top300」（build_universe 打當日 openapi），
這帶入兩層偏誤,分別量化其「可計算的上界」:

  (1) 候選池倖存者偏誤（candidate-pool survivorship）
      - 池內全是「到 snapshot 仍存活」的股票 → 缺歷史下市/轉板股。
      - 可計算:池中有多少檔在回測窗頭其實資料不足/根本不夠格,卻因窗尾存活被選入。
      - 不可計算:下市股對「當時真實 top300」的替換率 —— 需外部下市清單,故本報告
        明確標為「無法從現有快取量化」,並說明其方向（使報告績效偏樂觀）。

  (2) 候選集「窗尾排名」前視（selection look-ahead）
      - 池是用「窗尾成交值」排名,等於用未來人氣挑池 → 窗頭就把「後來才爆量」的股
        放進池。可計算:窗頭 vs 窗尾 的池內成交值排名遷移（rank migration）。

⚠ **research-only（原始快取稽核）**：本腳本直讀 `_cache` 的原始價格，繞過
`backtest` 的還原價與完整性 fail-closed 閘門，也不經過事件驅動引擎。它產出的是
**偏誤上界的診斷**，不是策略績效，不得升格成正式策略入口。

本腳本純離線讀 _cache 的價格快取（路徑一律由 data.cache_scope() 給，含快照戳與
範圍戳），不打 API、不改資料。
輸出 outputs/UNIVERSE_BIAS_REPORT.md + outputs/universe_bias_audit.csv。

用法:.venv/bin/python universe_bias_audit.py
"""
from __future__ import annotations

import glob
import os

import numpy as np
import pandas as pd

import config
import data
from universes import legacy_static as uni


WIN = 20  # 排名用的滾動視窗（交易日），對齊 DYNAMIC_UNIVERSE_LOOKBACK


def _load_survivor_prices() -> dict[str, pd.DataFrame]:
    """讀目前候選池 stock_id 的快取價格（含快照戳）。"""
    pool = uni.get_research_candidates()  # 現行候選池
    frames = {}
    for sid in pool:
        # 2026-08-15:檔名多了範圍維度,改問資料層要路徑(不再自己拼字串)。
        p = data.cache_scope("price", sid).path
        if not p.exists():
            continue
        try:
            df = pd.read_pickle(p)
        except Exception:
            continue
        if df is None or df.empty or "turnover" not in df.columns:
            continue
        df = df[(df["close"] > 0) & (df["turnover"] > 0)].sort_values("date")
        if not df.empty:
            frames[sid] = df.reset_index(drop=True)
    return frames


def run() -> None:
    frames = _load_survivor_prices()
    if not frames:
        print("[bias] 無快取價格（先跑 build_universe.py 與資料抓取），結束。")
        return

    all_dates = pd.to_datetime(
        np.unique(np.concatenate([f["date"].values for f in frames.values()]))
    )
    win_start, win_end = all_dates.min(), all_dates.max()

    rows = []
    for sid, f in frames.items():
        d = pd.to_datetime(f["date"])
        early = f[d <= win_start + pd.Timedelta(days=40)]  # 窗頭約前 ~1.5 個月
        late = f[d >= win_end - pd.Timedelta(days=40)]      # 窗尾約後 ~1.5 個月
        rows.append({
            "stock_id": sid,
            "n_days": len(f),
            "first_date": str(d.min())[:10],
            "last_date": str(d.max())[:10],
            "spans_full_window": bool(d.min() <= win_start + pd.Timedelta(days=10)
                                      and d.max() >= win_end - pd.Timedelta(days=10)),
            "early_avg_turnover": float(early["turnover"].mean()) if len(early) else np.nan,
            "late_avg_turnover": float(late["turnover"].mean()) if len(late) else np.nan,
        })
    a = pd.DataFrame(rows)

    # 池內排名遷移：用窗頭/窗尾平均成交值各自排名（1=最大）
    a["rank_early"] = a["early_avg_turnover"].rank(ascending=False, method="first")
    a["rank_late"] = a["late_avg_turnover"].rank(ascending=False, method="first")
    a["rank_climb"] = a["rank_early"] - a["rank_late"]  # 正 = 窗尾排名進步（後來爆量）

    n = len(a)
    n_full = int(a["spans_full_window"].sum())
    n_delisted = 0  # 快取內全是存活者 → 0（正是問題所在）
    # 前視代理:窗頭資料不足（<半個窗）卻被選入池的檔數
    n_late_entrants = int((a["n_days"] < 0.5 * a["n_days"].max()).sum())
    # 排名大幅遷移:窗尾比窗頭進步 >= 池規模 25% 的檔（「後來才紅」被提前放進池）
    big_climb = int((a["rank_climb"] >= 0.25 * n).sum())
    top_climbers = a.sort_values("rank_climb", ascending=False).head(10)

    a.to_csv(config.OUTPUT_DIR / "universe_bias_audit.csv", index=False, encoding="utf-8-sig")

    md = [
        "# Universe 倖存者 / 前視偏誤量化報告（上界）",
        "",
        f"> 候選池 = 現行 `universe_top{config.DYNAMIC_UNIVERSE_CANDIDATE_POOL}.json`"
        f"（build_universe 打當日 openapi、窗尾成交值排名）｜快照 "
        f"{getattr(config, 'SNAPSHOT_END_DATE', '') or 'live'}",
        f"> 分析窗 {str(win_start)[:10]} ~ {str(win_end)[:10]}｜可讀取快取 {n} 檔",
        "> 純離線審計,不打 API、不改資料。**此報告量化的是偏誤上界,不是修好偏誤。**",
        "",
        "## 一、候選池倖存者偏誤",
        "",
        f"- 池內橫跨完整窗的存活股:**{n_full}/{n}**",
        f"- 快取中的下市/轉板股:**{n_delisted}**（== 0，正說明池是純存活者集合）",
        f"- 窗頭資料明顯不足卻被選入池:**{n_late_entrants}** 檔",
        "",
        "> ⚠ **不可計算部分（方向性）**:真正的候選池倖存者偏誤來自「當時在榜、後來下市/",
        "> 轉板」的股票被完全排除。這些股票不在任何快取裡,無法從現有資料量化其替換率。",
        "> 其效果是**系統性拉高**報告績效（輸家提早消失）→ 目前所有絕對績效應視為**樂觀上界**,",
        "> 真實 edge ≤ 報告值。根治需外部歷史下市清單（見 RESEARCH_OPERATING_PROTOCOL）。",
        "",
        "## 二、候選集「窗尾排名」前視（selection look-ahead）",
        "",
        f"- 窗尾成交值排名比窗頭大幅進步（≥ 池規模 25%）的股:**{big_climb}/{n}**",
        "  代表這些股是「後來才爆量/變紅」,卻因為用**窗尾**人氣挑池而從**窗頭第一天**就在池內。",
        "  這是選池層的前視:回測在窗頭就能選到「後來才紅」的股。",
        "",
        "### 窗尾排名進步最多的 10 檔（後來才紅、被提前放進池）",
        "",
        "| stock_id | 窗頭排名 | 窗尾排名 | 進步 |",
        "|---|---|---|---|",
    ]
    for _, r in top_climbers.iterrows():
        md.append(f"| {r['stock_id']} | {int(r['rank_early'])} | {int(r['rank_late'])} "
                  f"| +{int(r['rank_climb'])} |")
    md += [
        "",
        "## 三、與已量化的 selection bias 對照",
        "",
        "- `DYNAMIC_UNIVERSE_REPORT.md` 已量化「每日 PIT 排名」對「窗尾靜態 top100」的差:",
        "  static current-top100 Sharpe ≈ 2.27 → 每日 PIT 排名後崩到 ≈ 0.11。",
        "  → 排名層前視已由 dynamic universe 消除;**本報告的第二層（候選集仍用窗尾人氣）",
        "  與第一層（下市缺失）尚未消除,只被量化為上界。**",
        "",
        "## 四、結論",
        "",
        "1. 候選池是純存活者 + 窗尾人氣挑池 → 兩層偏誤都讓績效偏樂觀。",
        "2. 排名層已由每日 PIT 動態 universe 消除;候選集層與下市層只被量化、未消除。",
        "3. **在切換到 survivorship-free PIT 全市場池之前,所有絕對績效一律標『樂觀上界、待重驗』。**",
    ]
    (config.OUTPUT_DIR / "UNIVERSE_BIAS_REPORT.md").write_text("\n".join(md), encoding="utf-8")
    print(f"[bias] 池 {n} 檔｜完整存活 {n_full}｜窗尾大幅進步 {big_climb}｜下市股 {n_delisted}")
    print(f"[bias] 報告已存:outputs/UNIVERSE_BIAS_REPORT.md")


if __name__ == "__main__":
    run()
