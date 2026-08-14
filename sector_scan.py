# -*- coding: utf-8 -*-
"""
族群輪動掃描（sector_scan）
===========================
DNA 研究證明「個股單點因子快照」無樣本外預測力（OS lift≈1）。改從【族群】下手：
資金往哪個族群流，是結構性、跨股票的訊號，可能才是能「提早」的東西。

本腳本回答三個問題（按嚴謹度遞增）：
  Q1【描述】現在哪些族群最強？（族群動能/廣度/法人流向排行）
  Q2【驗證】族群強弱會「延續」嗎？—— 族群動能的樣本外 IC。
            這是關鍵：會延續 → 抓強勢族群才成立；不會 → 族群輪動也是隨機。
  Q3【實戰】強勢族群裡，哪些是「補漲股」？（族群已動、個股還沒跟上 = 提早點）

族群定義：先用 FinMind 官方產業別（粗分類）跑通框架。
  ⚠️ 抓不到「功率元件」這種主題概念股（橫跨半導體+電子零組件），
     主題式族群需另給手工成分股清單（見 --theme_file，未提供則用官方產業別）。

防未來函數：族群聚合只用當天以前資料；forward return 只用來驗證、不進特徵。
IS/OS 時間切分 + embargo，與 winner_dna 同紀律。

用法：
  .venv/bin/python sector_scan.py                  # top300、官方產業別
  .venv/bin/python sector_scan.py --pool 200 --fwd 20
輸出：outputs/sector_scan_report.md + outputs/sector_*.csv
"""
from __future__ import annotations

import sys
from collections import defaultdict

import numpy as np
import pandas as pd

import config
import data
import dynamic_universe
import evaluation_split
import factors
import universe as uni

POOL = 300
FWD = 20            # 族群未來報酬視窗（交易日）— 驗證延續性用
MOM_WIN = 20        # 族群動能回看窗
MIN_SECTOR_N = 5    # 少於此檔數的產業不算族群


def _load_stock_panels(pool: int):
    """逐檔抓資料算因子，回傳 {sid: df}（含 close/ma/mom/inst 等）與 industry map。"""
    target = min(pool, config.DYNAMIC_UNIVERSE_TOP_N)
    symbols = uni.get_research_candidates(universe_top_n=target,
                                          candidate_pool_n=pool)
    imap = uni.get_industry_map()
    nmap = uni.get_name_map()
    print(f"[sector] 研究池 top{pool}：{len(symbols)} 檔，載入中 …")
    panels = {}
    for i, sid in enumerate(symbols, 1):
        f = factors.compute_factors(data.fetch_bundle(sid))
        if f.empty:
            continue
        panels[sid] = f[["date", "close", "volume", "turnover",
                         "ma_short", "ma_long", "mom_ret", "inst_6d",
                         "trend_ok"]].copy()
        if i % 50 == 0:
            print(f"  … {i}/{len(symbols)}")
    return panels, imap, nmap


def _build_sector_daily(panels: dict, imap: dict):
    """
    聚合成「每日 × 每族群」面板：
      ret20      族群中位 20日報酬（族群動能）
      breadth    族群內 close>MA20 的比例（廣度/共振度）
      inst6d     族群中位法人6日佔量比（資金流向）
      fwd_ret    族群中位「未來 FWD 日報酬」（驗證用，不進特徵）
    """
    # 先把每檔的每日指標攤平成長表
    rows = []
    for sid, df in panels.items():
        ind = imap.get(sid, "(未知)")
        d = df.sort_values("date").reset_index(drop=True)
        c = d["close"].values
        n = len(c)
        # 未來 FWD 日報酬（個股），之後聚合成族群中位
        fwd = np.full(n, np.nan)
        for t in range(n):
            if c[t] > 0 and t + FWD < n:
                fwd[t] = c[t + FWD] / c[t] - 1.0
        d["fwd_ret"] = fwd
        d["above_ma20"] = (d["close"] > d["ma_short"]).astype(float)
        d["stock_id"] = sid
        d["industry"] = ind
        rows.append(d[["stock_id", "date", "industry", "close", "volume",
                       "turnover", "mom_ret", "above_ma20", "inst_6d",
                       "fwd_ret"]])
    long = pd.concat(rows, ignore_index=True)

    if config.DYNAMIC_UNIVERSE_ENABLED:
        ranked = dynamic_universe.add_membership(
            long,
            top_n=min(POOL, config.DYNAMIC_UNIVERSE_TOP_N),
            lookback=config.DYNAMIC_UNIVERSE_LOOKBACK,
            min_obs=config.DYNAMIC_UNIVERSE_MIN_OBS,
            min_avg_volume_lots=config.DYNAMIC_UNIVERSE_MIN_AVG_VOLUME_LOTS,
            min_avg_turnover=config.DYNAMIC_UNIVERSE_MIN_AVG_TURNOVER,
        )
        long = ranked[ranked["in_dynamic_universe"]].copy()

    agg = long.groupby(["date", "industry"]).agg(
        ret20=("mom_ret", "median"),
        breadth=("above_ma20", "mean"),
        inst6d=("inst_6d", "median"),
        fwd_ret=("fwd_ret", "median"),
        n=("mom_ret", "count"),
    ).reset_index()
    agg = agg[agg["n"] >= MIN_SECTOR_N].reset_index(drop=True)
    return agg


def _sector_momentum_ic(agg: pd.DataFrame):
    """
    Q2 核心驗證：族群動能延續性。
    每個交易日，跨族群把「特徵(ret20/breadth/inst6d)」與「族群未來報酬」算 Spearman 相關（IC）。
    IC>0 且樣本外站得住 = 強勢族群會續強 = 輪動可被抓。
    回傳 IS/OS 各特徵的平均 IC。
    """
    split = evaluation_split.build_evaluation_split(
        agg["date"], minimum_embargo_days=FWD
    )
    cut, os_start = split.is_end, split.os_start
    feats = ["ret20", "breadth", "inst6d"]

    def _avg_ic(sub):
        out = {}
        for fcol in feats:
            ics = []
            for d, g in sub.groupby("date"):
                g2 = g.dropna(subset=[fcol, "fwd_ret"])
                if len(g2) >= 4:  # 至少4個族群才算跨族群相關
                    ic = g2[fcol].corr(g2["fwd_ret"], method="spearman")
                    if pd.notna(ic):
                        ics.append(ic)
            out[fcol] = (np.mean(ics) if ics else np.nan, len(ics))
        return out

    is_ic = _avg_ic(agg[(agg["date"] >= split.is_start) & (agg["date"] <= cut)])
    os_ic = _avg_ic(agg[(agg["date"] >= os_start) & (agg["date"] <= split.os_end)])
    return is_ic, os_ic, cut, os_start


def _current_ranking(agg: pd.DataFrame):
    """Q1：最後一個交易日的族群排行（綜合動能+廣度+法人）。"""
    last_date = agg["date"].max()
    cur = agg[agg["date"] == last_date].copy()
    # 三個指標各自 rank 後等權合成（0~1）
    for col in ["ret20", "breadth", "inst6d"]:
        cur[f"r_{col}"] = cur[col].rank(pct=True)
    cur["sector_score"] = cur[["r_ret20", "r_breadth", "r_inst6d"]].mean(axis=1)
    cur = cur.sort_values("sector_score", ascending=False).reset_index(drop=True)
    return cur, last_date


def _laggards_in_hot(panels, imap, nmap, hot_sectors, last_date):
    """
    Q3：強勢族群裡的「補漲股」。
    族群已動（在 hot 名單），但個股 mom_ret 還在族群後段（低於族群中位）、
    且趨勢仍 OK（沒壞），視為補漲候選。
    """
    # 先算每族群最後一日的 mom 中位
    sec_mom_med = {}
    for sid, df in panels.items():
        ind = imap.get(sid, "")
        if ind not in hot_sectors:
            continue
        row = df[df["date"] <= last_date]
        if row.empty:
            continue
        sec_mom_med.setdefault(ind, []).append(row.iloc[-1]["mom_ret"])
    sec_mom_med = {k: np.nanmedian(v) for k, v in sec_mom_med.items()}

    out = []
    for sid, df in panels.items():
        ind = imap.get(sid, "")
        if ind not in hot_sectors:
            continue
        row = df[df["date"] <= last_date]
        if row.empty:
            continue
        r = row.iloc[-1]
        med = sec_mom_med.get(ind, np.nan)
        if pd.isna(r["mom_ret"]) or pd.isna(med):
            continue
        # 補漲：個股動能 < 族群中位（落後），但趨勢沒壞
        if r["mom_ret"] < med and bool(r.get("trend_ok", False)):
            out.append({
                "stock_id": sid, "name": nmap.get(sid, ""), "industry": ind,
                "mom_ret": round(float(r["mom_ret"]), 3),
                "sector_mom_median": round(float(med), 3),
                "gap": round(float(med - r["mom_ret"]), 3),
                "inst_6d": round(float(r.get("inst_6d", 0) or 0), 3),
            })
    return pd.DataFrame(out).sort_values("gap", ascending=False).reset_index(drop=True)


def run(pool: int, fwd: int):
    global POOL, FWD
    POOL, FWD = pool, fwd

    panels, imap, nmap = _load_stock_panels(pool)
    if not panels:
        print("[sector] 無資料，結束。")
        return
    agg = _build_sector_daily(panels, imap)
    sectors = sorted(agg["industry"].unique())
    print(f"\n[sector] 有效族群（>={MIN_SECTOR_N}檔）：{len(sectors)} 個 -> {sectors}")

    # Q2：延續性驗證
    is_ic, os_ic, cut, os_start = _sector_momentum_ic(agg)
    print(f"\n[sector] Q2 族群動能延續性（IS截止{pd.Timestamp(cut).date()} / "
          f"OS起{pd.Timestamp(os_start).date()}，IC=跨族群特徵vs未來{fwd}日報酬）：")
    print(f"  {'特徵':<10}{'IS_IC':>10}{'OS_IC':>10}   判讀")
    for f in ["ret20", "breadth", "inst6d"]:
        isv, isn = is_ic[f]
        osv, osn = os_ic[f]
        verdict = "✓ 延續(OS>0.03)" if (pd.notna(osv) and osv > 0.03) else "✗ 樣本外無延續"
        print(f"  {f:<10}{isv:>10.3f}{osv:>10.3f}   {verdict}")

    # Q1：現在族群排行
    cur, last_date = _current_ranking(agg)
    print(f"\n[sector] Q1 族群排行（{pd.Timestamp(last_date).date()}）前8：")
    print(cur[["industry", "ret20", "breadth", "inst6d", "sector_score", "n"]]
          .head(8).to_string(index=False))

    # Q3：強勢族群補漲股（取族群分數前3的族群）
    hot = cur.head(3)["industry"].tolist()
    lag = _laggards_in_hot(panels, imap, nmap, hot, last_date)
    print(f"\n[sector] Q3 強勢族群 {hot} 的補漲候選（落後族群、趨勢未壞）前10：")
    if lag.empty:
        print("  （無）")
    else:
        print(lag.head(10).to_string(index=False))

    _save(agg, cur, last_date, lag, is_ic, os_ic, cut, os_start, hot)


def _save(agg, cur, last_date, lag, is_ic, os_ic, cut, os_start, hot):
    agg.to_csv(config.OUTPUT_DIR / "sector_daily.csv", index=False, encoding="utf-8-sig")
    cur.to_csv(config.OUTPUT_DIR / "sector_ranking.csv", index=False, encoding="utf-8-sig")
    lag.to_csv(config.OUTPUT_DIR / "sector_laggards.csv", index=False, encoding="utf-8-sig")

    def _ic_row(name):
        return (f"| {name} | {is_ic[name][0]:.3f} | {os_ic[name][0]:.3f} | "
                f"{'✓ 延續' if (pd.notna(os_ic[name][0]) and os_ic[name][0] > 0.03) else '✗ 無延續'} |")

    lines = [
        "# 族群輪動掃描報告",
        "",
        f"> 候選池 current top{POOL}；每日動態 top{min(POOL, config.DYNAMIC_UNIVERSE_TOP_N)}"
        f"｜族群=FinMind官方產業別（≥{MIN_SECTOR_N}檔）｜未來報酬窗 {FWD} 日",
        f"> IS/OS 切分 {pd.Timestamp(cut).date()} / OS起 {pd.Timestamp(os_start).date()}",
        "",
        "## Q2 族群動能延續性（關鍵：能不能『提早抓族群』的前提）",
        "",
        "跨族群把特徵與未來報酬算 IC。OS_IC>0.03 才代表強勢族群會續強、輪動可被抓。",
        "",
        "| 族群特徵 | IS_IC | OS_IC | 判讀 |",
        "|---|---|---|---|",
        _ic_row("ret20") + "  ← 族群20日動能",
        _ic_row("breadth") + "  ← 族群廣度(站上MA20比例)",
        _ic_row("inst6d") + "  ← 族群法人流向",
        "",
        "> OS_IC ≈ 0 或負 = 族群輪動也是隨機，跟個股 DNA 一樣無樣本外 edge。",
        "> OS_IC 明顯為正 = 找到可抓的結構性訊號，這條路成立。",
        "",
        f"## Q1 當前族群排行（{pd.Timestamp(last_date).date()}）",
        "",
        cur[["industry", "ret20", "breadth", "inst6d", "sector_score", "n"]].head(10).to_markdown(index=False),
        "",
        f"## Q3 強勢族群 {hot} 補漲候選（族群已動、個股落後、趨勢未壞）",
        "",
        "「提早」的實戰落點：族群動起來但這些股還沒跟上。gap 越大越落後。",
        "",
        (lag.head(15).to_markdown(index=False) if not lag.empty else "（無補漲候選）"),
        "",
        "## 重要保留",
        "",
        "- 族群用官方粗分類，抓不到「功率元件」等主題概念股（橫跨多產業）。主題式需手工成分清單。",
        "- 僅 2024-2026 單一多頭行情，族群延續性可能有時代紅利。",
        "- 本報告為研究用途，非投資建議。",
    ]
    md = config.OUTPUT_DIR / "sector_scan_report.md"
    md.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n[sector] 報告已存：{md}")


def _parse(argv):
    p, fwd = POOL, FWD
    i = 0
    while i < len(argv):
        if argv[i] == "--pool" and i + 1 < len(argv):
            p = int(argv[i + 1]); i += 1
        elif argv[i] == "--fwd" and i + 1 < len(argv):
            fwd = int(argv[i + 1]); i += 1
        i += 1
    return p, fwd


if __name__ == "__main__":
    p, fwd = _parse(sys.argv[1:])
    run(p, fwd)
