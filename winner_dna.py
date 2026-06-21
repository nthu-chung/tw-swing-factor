# -*- coding: utf-8 -*-
"""
飆漲股 DNA 研究（winner_dna）
=============================
反過來做：先找出「事後飆漲」的股票，回推起漲點，看起漲當天有什麼共通特徵，
再用一段獨立時間驗證這些特徵是不是真的有預測力。

為什麼這樣設計（避免「只看贏家」的倖存者偏誤）：
  - 只盯飆漲股一定找得到共通點，但那可能是「所有股票都有」的特徵。
  - 所以一定要有【對照組】：同樣的日期，抽沒飆的股票算同一套特徵，比較兩群差異。
  - 只有贏家明顯高於對照組的特徵，才算訊號（lift / 命中率 vs 基準率）。
  - 再把規則【鎖死在前段時間】、到【後段時間】驗證，杜絕事後諸葛。

防未來函數：
  - 起漲點特徵一律取自 factors.compute_factors（point-in-time，只看當天以前）。
  - 飆漲判定 fwd_max 只用來「貼標籤」，絕不進特徵。
  - IS/OS 以時間切分，中間留 embargo 緩衝，避免一筆飆漲事件橫跨兩段洩漏。

用法：
  .venv/bin/python winner_dna.py                 # 預設 30% / 60日 / top300
  .venv/bin/python winner_dna.py --gain 0.5 --win 40 --pool 200
輸出：outputs/winner_dna_report.md + outputs/winner_dna_*.csv
"""
from __future__ import annotations

import sys
from typing import Optional

import numpy as np
import pandas as pd

import config
import data
import factors
import universe as uni


# ── 參數（可被命令列覆寫）────────────────────────────────────────────────
GAIN = 0.30          # 飆漲門檻：未來視窗內最大漲幅 >= 此值
WIN = 60             # 飆漲視窗（交易日）
POOL = 300           # 研究股票池（成交值前 N 大）
NEG_GAIN_MAX = 0.10  # 對照組（沒飆）：未來視窗最大漲幅 < 此值才算乾淨負樣本
IS_FRAC = 0.60       # 前 60% 時間找規則（in-sample）
EMBARGO = WIN        # IS/OS 之間緩衝（= 視窗，避免事件橫跨）

# 拿來比較的特徵欄位（全部來自 compute_factors，point-in-time 安全）
FEATURES = [
    "mom_ret",        # 60日報酬（動能）
    "near_high",      # 收盤 / 60日高（貼近新高程度）
    "bb_pos",         # 布林位階（離月線多遠）
    "bias_short",     # |close-MA20|/MA20
    "bias_mid",       # |close-MA60|/MA60
    "vol_ratio",      # 近5日量 / 前5日量（量能變化）
    "inst_6d",        # 法人6日淨買佔量比
    "inst_12d",       # 法人12日淨買佔量比
    "inst_dip_buy_days",  # 近5日跌時法人仍買天數
    "margin_short_ratio", # 資券比
    "avg_vol_lots",   # 近20日均量（張）
    "breakout_retest", # H3：突破回測不破型態（1/0）
]


def _load_panel(pool: int) -> pd.DataFrame:
    """逐檔抓資料、算因子，疊成一張大表（含未來最大漲幅標籤）。"""
    symbols = uni.get_universe(top_n=pool)
    rank_map = {sid: r for r, sid in enumerate(symbols, 1)}  # 成交值排名（市值代理，H4）
    print(f"[dna] 研究池 top{pool}：{len(symbols)} 檔，開始抓資料/算因子 …")
    frames = []
    for i, sid in enumerate(symbols, 1):
        bundle = data.fetch_bundle(sid)
        f = factors.compute_factors(bundle)
        if f.empty:
            continue
        f = f.copy()
        f["stock_id"] = sid
        # 未來視窗內最大漲幅（只用來貼標籤，不進特徵）
        c = f["close"].values
        n = len(c)
        fwd_max = np.full(n, np.nan)
        for t in range(n):
            if c[t] <= 0:           # 髒資料（停牌/缺值）跳過，避免除以零
                continue
            hi = c[t + 1: t + 1 + WIN]
            if len(hi) > 0:
                fwd_max[t] = hi.max() / c[t] - 1.0
        f["fwd_max"] = fwd_max

        # H3：突破回測型態（接 MNQ break-retest 發現）
        # 定義：近 20 日內曾創 60 日新高（突破），且當下已從該高點回檔 3~12%
        #       但仍站在「突破前 20 日高點」之上（回測不破）。1=符合, 0=否。
        roll_high_60 = f["roll_high"].values  # compute_factors 已算（60日高）
        cc = f["close"].values
        prior_high_20 = pd.Series(f["high"]).rolling(20).max().shift(1).values
        recent_new_high = pd.Series(roll_high_60).rolling(20).max().shift(1).values
        br = np.zeros(n, dtype=float)
        for t in range(n):
            if cc[t] <= 0 or np.isnan(recent_new_high[t]) or np.isnan(prior_high_20[t]):
                continue
            pulled_back = recent_new_high[t] > 0 and (cc[t] / recent_new_high[t] - 1.0) <= -0.03 \
                and (cc[t] / recent_new_high[t] - 1.0) >= -0.12
            holds = cc[t] >= prior_high_20[t]
            br[t] = 1.0 if (pulled_back and holds) else 0.0
        f["breakout_retest"] = br

        f["pool_rank"] = rank_map.get(sid, 9999)  # H4：成交值排名（市值代理）
        frames.append(f)
        if i % 25 == 0:
            print(f"  … {i}/{len(symbols)} 檔")
    if not frames:
        return pd.DataFrame()
    panel = pd.concat(frames, ignore_index=True)
    panel = panel.dropna(subset=["fwd_max"]).reset_index(drop=True)
    return panel


def _label_events(panel: pd.DataFrame) -> pd.DataFrame:
    """
    貼標籤：
      is_winner = 未來視窗最大漲幅 >= GAIN（飆漲起漲點候選）
      is_control = 未來視窗最大漲幅 < NEG_GAIN_MAX（乾淨的沒飆對照）
    對 winner 同一檔做去重：一段飆漲只取「最早合格日」當起漲點，之後 WIN 天內不重複計。
    """
    panel = panel.sort_values(["stock_id", "date"]).reset_index(drop=True)
    panel["is_winner_raw"] = panel["fwd_max"] >= GAIN
    panel["is_control"] = panel["fwd_max"] < NEG_GAIN_MAX

    # 起漲點去重：每檔內，winner 連續區間只留第一天，之後 WIN 天封鎖
    is_launch = np.zeros(len(panel), dtype=bool)
    for sid, idx in panel.groupby("stock_id").groups.items():
        idx = list(idx)
        blocked_until = -1
        for pos, gi in enumerate(idx):
            if not panel.at[gi, "is_winner_raw"]:
                continue
            if pos <= blocked_until:
                continue
            is_launch[gi] = True
            blocked_until = pos + WIN  # 封鎖之後 WIN 根 K
    panel["is_launch"] = is_launch
    return panel


def _split_is_os(panel: pd.DataFrame):
    """以時間切分 IS / OS，中間留 embargo。"""
    dates = np.sort(panel["date"].unique())
    cut = dates[int(len(dates) * IS_FRAC)]
    os_start = cut + pd.Timedelta(days=int(EMBARGO * 1.5))  # 日曆日緩衝（>交易日）
    is_df = panel[panel["date"] <= cut]
    os_df = panel[panel["date"] >= os_start]
    return is_df, os_df, cut, os_start


def _compare(launch: pd.DataFrame, control: pd.DataFrame) -> pd.DataFrame:
    """逐特徵比較起漲組 vs 對照組的中位數，算標準化差距（穩健版 effect size）。"""
    rows = []
    for col in FEATURES:
        a = pd.to_numeric(launch[col], errors="coerce").dropna()
        b = pd.to_numeric(control[col], errors="coerce").dropna()
        if len(a) < 10 or len(b) < 10:
            continue
        med_a, med_b = a.median(), b.median()
        # 用對照組的 MAD 當尺度，算「起漲組中位數比對照組高幾個尺度」
        mad_b = (b - b.median()).abs().median() or b.std(ddof=0) or 1.0
        lift = (med_a - med_b) / (1.4826 * mad_b)
        rows.append({
            "feature": col,
            "launch_median": round(float(med_a), 4),
            "control_median": round(float(med_b), 4),
            "robust_lift": round(float(lift), 3),
        })
    out = pd.DataFrame(rows).sort_values("robust_lift", key=lambda s: s.abs(),
                                         ascending=False).reset_index(drop=True)
    return out


def _build_rule(is_launch: pd.DataFrame, is_control: pd.DataFrame, cmp_df: pd.DataFrame):
    """
    用 IS 的比較結果，把「起漲組明顯高/低於對照」的前幾個特徵組成門檻規則。
    門檻取「起漲組該特徵的 25 百分位」（方向依 lift 正負），當作寬鬆共通條件。
    回傳 (rule_dict, 說明字串)。
    """
    top = cmp_df[cmp_df["robust_lift"].abs() >= 0.5].head(4)
    rule, desc = {}, []
    for _, r in top.iterrows():
        col = r["feature"]
        vals = pd.to_numeric(is_launch[col], errors="coerce").dropna()
        if r["robust_lift"] > 0:   # 起漲組偏高 → 取下緣門檻
            thr = float(vals.quantile(0.25))
            rule[col] = (">=", thr)
            desc.append(f"{col} >= {thr:.3f}")
        else:                       # 起漲組偏低 → 取上緣門檻
            thr = float(vals.quantile(0.75))
            rule[col] = ("<=", thr)
            desc.append(f"{col} <= {thr:.3f}")
    return rule, "、".join(desc)


def _apply_rule(df: pd.DataFrame, rule: dict) -> pd.Series:
    mask = pd.Series(True, index=df.index)
    for col, (op, thr) in rule.items():
        v = pd.to_numeric(df[col], errors="coerce")
        mask &= (v >= thr) if op == ">=" else (v <= thr)
    return mask.fillna(False)


def _hit_rate(df: pd.DataFrame, mask: pd.Series) -> dict:
    """規則命中的樣本裡，未來真的飆漲(>=GAIN)的比例 vs 全體基準率。"""
    base = float((df["fwd_max"] >= GAIN).mean())
    sel = df[mask]
    if len(sel) == 0:
        return {"n": 0, "hit": float("nan"), "base": base, "lift": float("nan")}
    hit = float((sel["fwd_max"] >= GAIN).mean())
    return {"n": int(len(sel)), "hit": hit, "base": base,
            "lift": (hit / base if base > 0 else float("nan"))}


def run(gain: float, win: int, pool: int):
    global GAIN, WIN, POOL, EMBARGO
    GAIN, WIN, POOL = gain, win, pool
    EMBARGO = win

    panel = _load_panel(pool)
    if panel.empty:
        print("[dna] 無資料，結束。")
        return
    panel = _label_events(panel)

    n_launch = int(panel["is_launch"].sum())
    n_ctrl = int(panel["is_control"].sum())
    print(f"\n[dna] 起漲事件：{n_launch} 筆；對照（沒飆）樣本：{n_ctrl} 筆")
    if n_launch < 20:
        print("[dna] ⚠️ 起漲事件太少，結論不可靠。建議放寬 --gain 或加大 --pool。")

    is_df, os_df, cut, os_start = _split_is_os(panel)
    is_launch = is_df[is_df["is_launch"]]
    is_control = is_df[is_df["is_control"]]
    os_all = os_df

    print(f"[dna] IS 截止 {pd.Timestamp(cut).date()}；OS 起 {pd.Timestamp(os_start).date()}")
    print(f"[dna] IS 起漲 {len(is_launch)} / 對照 {len(is_control)}；OS 全樣本 {len(os_all)}")

    cmp_df = _compare(is_launch, is_control)
    print("\n[dna] IS 起漲組 vs 對照組（穩健 lift，|值|大=區別大）：")
    print(cmp_df.to_string(index=False))

    rule, rule_desc = _build_rule(is_launch, is_control, cmp_df)
    print(f"\n[dna] 凝練規則（鎖死於 IS）：{rule_desc or '（無顯著特徵）'}")

    is_eval = _hit_rate(is_df, _apply_rule(is_df, rule))
    os_eval = _hit_rate(os_all, _apply_rule(os_all, rule))
    print("\n[dna] 規則命中率（飆漲=未來{}日漲>={:.0%}）：".format(win, gain))
    print(f"  IS：命中 {is_eval['hit']:.1%}  vs 基準 {is_eval['base']:.1%}  "
          f"(lift {is_eval['lift']:.2f}x, n={is_eval['n']})")
    print(f"  OS：命中 {os_eval['hit']:.1%}  vs 基準 {os_eval['base']:.1%}  "
          f"(lift {os_eval['lift']:.2f}x, n={os_eval['n']})  ← 樣本外才算數")

    # ── H4：大型(rank<=100) vs 中小型(rank>100) 分組，在 OS 上比規則 lift ──
    size_groups = {}
    if "pool_rank" in os_all.columns:
        for label, m in [("大型(top100)", os_all["pool_rank"] <= 100),
                         ("中小型(101+)", os_all["pool_rank"] > 100)]:
            sub = os_all[m]
            ev = _hit_rate(sub, _apply_rule(sub, rule)) if len(sub) else None
            size_groups[label] = ev
        print("\n[dna] H4 市值分組（OS 樣本外，同一條規則）：")
        for label, ev in size_groups.items():
            if ev and ev["n"] > 0:
                print(f"  {label}：命中 {ev['hit']:.1%} vs 基準 {ev['base']:.1%} "
                      f"(lift {ev['lift']:.2f}x, n={ev['n']})")
            else:
                print(f"  {label}：樣本不足")

    # ── H3：突破回測型態 單獨在 OS 上看 lift ──
    h3 = None
    if "breakout_retest" in os_all.columns:
        h3 = _hit_rate(os_all, os_all["breakout_retest"] >= 1.0)
        print(f"\n[dna] H3 突破回測型態（OS）：命中 {h3['hit']:.1%} vs 基準 {h3['base']:.1%} "
              f"(lift {h3['lift']:.2f}x, n={h3['n']})")

    _save(cmp_df, rule_desc, is_eval, os_eval, cut, os_start, n_launch, n_ctrl,
          size_groups, h3)


def _save(cmp_df, rule_desc, is_eval, os_eval, cut, os_start, n_launch, n_ctrl,
          size_groups=None, h3=None):
    cmp_df.to_csv(config.OUTPUT_DIR / "winner_dna_features.csv",
                  index=False, encoding="utf-8-sig")
    md = config.OUTPUT_DIR / "winner_dna_report.md"
    lines = [
        "# 飆漲股 DNA 研究報告",
        "",
        f"> 飆漲定義：未來 {WIN} 交易日內最大漲幅 ≥ {GAIN:.0%}｜研究池 top{POOL}",
        f"> 對照組：未來 {WIN} 日最大漲幅 < {NEG_GAIN_MAX:.0%}（乾淨沒飆）",
        f"> IS/OS 切分點 {pd.Timestamp(cut).date()}，OS 起 {pd.Timestamp(os_start).date()}（中間 embargo）",
        f"> 起漲事件總數 {n_launch}、對照樣本 {n_ctrl}",
        "",
        "## 一、起漲組 vs 對照組 特徵差異（IS）",
        "",
        "robust_lift 為正 = 起漲股該特徵明顯高於沒飆股；負 = 明顯低。|值| 越大區別越大。",
        "",
        cmp_df.to_markdown(index=False),
        "",
        "## 二、凝練規則（鎖死於 IS，不看 OS）",
        "",
        f"`{rule_desc or '（無顯著特徵，需放寬門檻或加大樣本）'}`",
        "",
        "## 三、樣本外驗證（關鍵）",
        "",
        "| 段 | 規則命中飆漲率 | 全體基準率 | lift | 樣本數 |",
        "|---|---|---|---|---|",
        f"| IS（找規則用） | {is_eval['hit']:.1%} | {is_eval['base']:.1%} | {is_eval['lift']:.2f}x | {is_eval['n']} |",
        f"| **OS（樣本外，才算數）** | **{os_eval['hit']:.1%}** | {os_eval['base']:.1%} | **{os_eval['lift']:.2f}x** | {os_eval['n']} |",
        "",
        "> 判讀：OS lift 明顯 >1（且樣本夠）才代表這套起漲特徵真的有預測力；",
        "> 若 OS lift ≈ 1 或 < IS 很多，就是 in-sample 過擬合，起漲共通點只是事後諸葛。",
        "",
    ]
    # H3：突破回測型態
    if h3 is not None:
        lines += [
            "## 三之二、H3 突破回測型態（接 MNQ break-retest）",
            "",
            "假說：創新高後拉回不破前高再進，勝率優於追高（MNQ 上此型態是少數有 edge 者）。",
            "",
            "| 型態 | 命中飆漲率 | 基準率 | lift | 樣本數 |",
            "|---|---|---|---|---|",
            f"| 突破回測不破（OS） | {h3['hit']:.1%} | {h3['base']:.1%} | {h3['lift']:.2f}x | {h3['n']} |",
            "",
            "> lift >1 = 台股動能市也支持 break-retest，與 MNQ 跨市場一致。",
            "",
        ]
    # H4：市值分組
    if size_groups:
        lines += [
            "## 三之三、H4 小型股動能溢價（大型 vs 中小型）",
            "",
            "假說：動能/起漲特徵在中小型股更強（流動性溢價、法人較難一次布局）。",
            "",
            "| 市值組 | 命中飆漲率 | 基準率 | lift | 樣本數 |",
            "|---|---|---|---|---|",
        ]
        for label, ev in size_groups.items():
            if ev and ev["n"] > 0:
                lines.append(f"| {label} | {ev['hit']:.1%} | {ev['base']:.1%} | "
                             f"{ev['lift']:.2f}x | {ev['n']} |")
            else:
                lines.append(f"| {label} | 樣本不足 | — | — | — |")
        lines += [
            "",
            "> 若中小型 lift 明顯 > 大型，代表選股池該往中小型移（目前 screen 用 top200 偏大型）。",
            "",
        ]
    lines += [
        "## 四、重要保留",
        "",
        f"- FinMind 免費版僅約 2 年，只涵蓋 2024-2026（AI 大多頭單一行情），",
        f"  「飆漲特徵」可能有時代紅利，換到空頭/盤整未必成立。",
        f"- 飆漲定義（{GAIN:.0%}/{WIN}日）會影響結論，可用 --gain/--win 做敏感度測試。",
        "- 本報告為研究用途，非投資建議。",
    ]
    md.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n[dna] 報告已存：{md}")


def _parse(argv):
    g, w, p = GAIN, WIN, POOL
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--gain" and i + 1 < len(argv):
            g = float(argv[i + 1]); i += 1
        elif a == "--win" and i + 1 < len(argv):
            w = int(argv[i + 1]); i += 1
        elif a == "--pool" and i + 1 < len(argv):
            p = int(argv[i + 1]); i += 1
        i += 1
    return g, w, p


if __name__ == "__main__":
    g, w, p = _parse(sys.argv[1:])
    run(g, w, p)
