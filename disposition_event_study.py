# -*- coding: utf-8 -*-
"""
處置解除(出關)事件研究
========================
假說:過熱急漲股被處置(連續達注意→分盤+預收款券10日)後,量縮、主力難出貨,
出關(處置期滿)後是續強還是反轉?這是動能主線的自然延伸(動能常追到將被處置的股),
且資料免費可得(twse_disposition 由真實注意推導處置期間)。

設計(全因果、PIT):
  - 事件 = 處置期間結束(disp_end,出關)。進場 = 出關『次一交易日開盤』(T+1 open),
    退場 = 之後 h 個交易日收盤。算超額報酬 = 個股報酬 − 同期 TAIEX 報酬。
  - 對照另一角度:處置『開始日』(disp_start,通常=急漲後)往後看,測『追到即將處置的
    熱門股』之後的報酬(續強 vs 反轉)。
  - 剔除 disp_end 落在資料邊界(無 forward 資料)的段。IS/OS 時間切分。

⚠ 限制:(1) 處置期間是 proxy(真實注意+規則,偏寬);(2) 只用有快取價格的股(偏大型,
真正被處置的多為中小型冷門股,樣本偏誤);(3) 未還原價;(4) 出關當日分盤流動性成本未計。
結論僅為方向性線索,非可上線 edge。

用法:.venv/bin/python disposition_event_study.py
"""
from __future__ import annotations

import glob
import os

import numpy as np
import pandas as pd

import config

HORIZONS = (1, 5, 20)


def _load_prices():
    snap = getattr(config, "SNAPSHOT_END_DATE", "").strip() or "live"
    prices = {}
    for p in glob.glob(str(config.CACHE_DIR / f"price__*__{snap}.pkl")):
        sid = os.path.basename(p).split("__")[1]
        try:
            df = pd.read_pickle(p)
        except Exception:
            continue
        df = df[(df["close"] > 0) & (df["open"] > 0)].sort_values("date").reset_index(drop=True)
        if len(df) > 30:
            prices[sid] = df
    return prices, snap


def _taiex():
    df = pd.read_pickle(config.CACHE_DIR / f"market__TAIEX__{config.SNAPSHOT_END_DATE}.pkl")
    return df.sort_values("date").reset_index(drop=True)


def _fwd_from(df, anchor_date, h, use_open_entry=True):
    """從 anchor_date 的次一交易日開盤進場,持有 h 交易日到收盤;回報酬 + 進出日。"""
    idx = df.index[df["date"] == anchor_date]
    if len(idx) == 0:
        return None
    i = int(idx[0])
    e = i + 1                       # 次一交易日進場
    x = e + h                       # h 交易日後
    if x >= len(df) or e >= len(df):
        return None
    entry = float(df["open"].iloc[e]) if use_open_entry else float(df["close"].iloc[e])
    exit_c = float(df["close"].iloc[x])
    if entry <= 0:
        return None
    return {"ret": exit_c / entry - 1.0, "entry_date": df["date"].iloc[e], "exit_date": df["date"].iloc[x]}


def _taiex_ret(tx, d0, d1):
    a = tx.index[tx["date"] == d0]; b = tx.index[tx["date"] == d1]
    if len(a) == 0 or len(b) == 0:
        return np.nan
    c0 = tx["close"].iloc[int(a[0])]; c1 = tx["close"].iloc[int(b[0])]
    return c1 / c0 - 1.0 if c0 > 0 else np.nan


def run():
    prices, snap = _load_prices()
    disp = pd.read_pickle(config.CACHE_DIR / f"disposition__ALL__{snap}.pkl")
    tx = _taiex()
    data_end = max(df["date"].max() for df in prices.values())

    rows = []
    for _, r in disp.iterrows():
        sid = r["stock_id"]
        df = prices.get(sid)
        if df is None:
            continue
        # 出關事件:disp_end 出關,次日開盤進場;剔除邊界(disp_end 太近資料尾)
        if r["disp_end"] >= data_end - pd.Timedelta(days=35):
            continue
        rec = {"stock_id": sid, "disp_start": r["disp_start"], "disp_end": r["disp_end"]}
        ok = False
        for h in HORIZONS:
            f = _fwd_from(df, r["disp_end"], h)
            if f is None:
                rec[f"h{h}"] = np.nan; rec[f"h{h}_ex"] = np.nan; continue
            txr = _taiex_ret(tx, f["entry_date"], f["exit_date"])
            rec[f"h{h}"] = f["ret"]
            rec[f"h{h}_ex"] = f["ret"] - txr if pd.notna(txr) else np.nan
            ok = True
        if ok:
            rows.append(rec)

    ev = pd.DataFrame(rows)
    if ev.empty:
        print("[event] 無有效事件。"); return
    # IS/OS 切分(依 disp_end)
    cut = ev["disp_end"].quantile(config.IS_OS_SPLIT)
    _report(ev, cut, snap)
    return ev


def _stat(s):
    s = s.dropna()
    if len(s) < 5:
        return None
    from math import sqrt
    t = s.mean() / (s.std(ddof=1) / sqrt(len(s))) if s.std(ddof=1) > 0 else np.nan
    return {"n": len(s), "mean": s.mean(), "median": s.median(),
            "win": (s > 0).mean(), "t": t}


def _report(ev, cut, snap):
    print("=" * 92)
    print(f"  處置解除(出關)事件研究｜snapshot {snap}｜出關次日開盤進場 → T+h 收盤")
    print(f"  事件數 {len(ev)}｜{ev['stock_id'].nunique()} 檔｜⚠ 處置為 proxy、僅快取股(偏大型)、未還原價")
    print("=" * 92)
    print(f"  {'視窗':<6}{'n':>5}{'平均':>9}{'中位':>9}{'勝率':>8}{'超額均':>9}{'超額t':>8}  判讀")
    print("  " + "-" * 84)
    for h in HORIZONS:
        raw = _stat(ev[f"h{h}"]); ex = _stat(ev[f"h{h}_ex"])
        if not raw:
            continue
        exs = f"{ex['mean']:+.2%}" if ex else "n/a"
        ext = f"{ex['t']:+.2f}" if ex and pd.notna(ex['t']) else "n/a"
        v = ("★ 超額顯著" if ex and pd.notna(ex['t']) and abs(ex['t']) > 2
             else "—")
        print(f"  T+{h:<4}{raw['n']:>5}{raw['mean']:>+9.2%}{raw['median']:>+9.2%}"
              f"{raw['win']:>8.0%}{exs:>9}{ext:>8}  {v}")
    # IS/OS
    print("  " + "-" * 84)
    for seg, m in [("IS", ev["disp_end"] <= cut), ("OS", ev["disp_end"] > cut)]:
        sub = ev[m]
        ex20 = _stat(sub["h20_ex"])
        if ex20:
            print(f"  [{seg}] T+20 超額 均 {ex20['mean']:+.2%} 中位 {ex20['median']:+.2%} "
                  f"勝率 {ex20['win']:.0%} t {ex20['t']:+.2f} (n={ex20['n']})")
    print("=" * 92)
    ev.to_csv(config.OUTPUT_DIR / "disposition_event_study.csv", index=False, encoding="utf-8-sig")

    # markdown
    lines = [
        "# 處置解除(出關)事件研究",
        "",
        f"> snapshot `{snap}`｜出關(處置期滿)次日開盤進場 → 持有 T+h 收盤｜超額 = 個股 − TAIEX。",
        f"> 事件 {len(ev)} 筆 / {ev['stock_id'].nunique()} 檔。",
        "> ⚠ **限制**:處置期間為 proxy(真實注意+連續3日規則,偏寬);只用有快取價格的股"
        "(偏大型,真正被處置多為中小型冷門股→樣本偏誤);未還原價;未計出關當日分盤流動性成本。"
        "結論僅方向性線索,非可上線 edge,需擴樣+還原價+forward 重驗。",
        "",
        "| 視窗 | n | 平均 | 中位 | 勝率 | 超額均 | 超額t |",
        "|---|---|---|---|---|---|---|",
    ]
    for h in HORIZONS:
        raw = _stat(ev[f"h{h}"]); ex = _stat(ev[f"h{h}_ex"])
        if not raw:
            continue
        lines.append(f"| T+{h} | {raw['n']} | {raw['mean']:+.2%} | {raw['median']:+.2%} | "
                     f"{raw['win']:.0%} | {ex['mean']:+.2%} | {ex['t']:+.2f} |")
    lines += ["", "> |超額 t|>2 才算方向性顯著;IS/OS 見 console。若超額≈0 或反向,代表"
              "『出關續強』只是傳說,別做。"]
    (config.OUTPUT_DIR / "DISPOSITION_EVENT_STUDY.md").write_text("\n".join(lines), encoding="utf-8")
    print("[event] 已存 outputs/DISPOSITION_EVENT_STUDY.md + disposition_event_study.csv")


if __name__ == "__main__":
    run()
