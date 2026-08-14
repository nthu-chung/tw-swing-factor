# -*- coding: utf-8 -*-
"""
Forward-only 樣本外測試（唯一能產生真 clean OOS 的路徑）
======================================================
讀一份凍結的 FROZEN_MANIFEST（freeze_manifest.py 產出），把當時凍結的規則原封套用，
只對「凍結日之後才發生」的資料跑回測。因為規則在看到這些資料之前就固定了，這段
績效才是真正沒被擬合過的樣本外。

怎麼用（會累積,不是一次到位）:
  1. 某天跑 freeze_manifest.py 凍結規則（記下 freeze_date 與當時 data_snapshot）。
  2. 之後推進 SNAPSHOT_END_DATE（或用 live 模式）抓到 freeze_date 之後的新資料。
  3. 跑 forward_test.py → 它只算 (freeze_date, 最新資料] 這段的績效 + 買進持有基準。
  4. 隨時間累積,forward 期越長,OOS 檢定力越高。

注意:未還原價下仍受 backtest 的 fail-closed 閘門保護（會 raise，除非顯式逃生門）。

用法:.venv/bin/python forward_test.py                 # 用最新 manifest
      .venv/bin/python forward_test.py --manifest outputs/FROZEN_MANIFEST_2026-07-24.json
"""
from __future__ import annotations

import argparse
import copy
import glob
import json

import numpy as np
import pandas as pd

import config


def _latest_manifest() -> str | None:
    files = sorted(glob.glob(str(config.OUTPUT_DIR / "FROZEN_MANIFEST_*.json")))
    return files[-1] if files else None


def _apply_rules(rules: dict) -> None:
    """把凍結規則寫回 config（in-process，不改檔）。"""
    for k, v in rules.items():
        if hasattr(config, k):
            setattr(config, k, copy.deepcopy(v))


def run(manifest_path: str | None) -> None:
    manifest_path = manifest_path or _latest_manifest()
    if not manifest_path:
        print("找不到任何 FROZEN_MANIFEST（先跑 freeze_manifest.py）。")
        return
    m = json.loads(open(manifest_path, encoding="utf-8").read())
    freeze_date = m["freeze_date"]
    _apply_rules(m.get("rules", {}))

    # forward 窗 = (freeze_date, 目前資料最新日]
    snap = getattr(config, "SNAPSHOT_END_DATE", "").strip()
    latest = snap or "live(最新)"
    if snap and pd.to_datetime(snap) <= pd.to_datetime(freeze_date):
        print(f"⚠ 目前資料快照 {snap} ≤ 凍結日 {freeze_date}：尚無凍結後的新資料。")
        print("  → 推進 SNAPSHOT_END_DATE 抓 freeze_date 之後的資料，再跑本測試。"
              " 現在 forward 期為空,這是正常的（規則剛凍結）。")
        return

    # 延後 import,確保 _apply_rules 先生效
    import backtest
    from universes import historical_pit_universe

    # forward 是唯一能產生真 clean OOS 的路徑,候選池必須是 PIT 的。
    # 舊版用 universe 模組那支 legacy 靜態候選池函式(單一日期排名)當候選池:
    # 那等於用「凍結之後才知道誰熱門」決定 forward 期能選誰 —— 前視污染的正是
    # 這條路徑最不能污染的數字。改走月頻 PIT 入口。
    if not config.DYNAMIC_UNIVERSE_ENABLED:
        print("[forward] DYNAMIC_UNIVERSE_ENABLED=False:forward 不接受 legacy "
              "單日靜態池(非 PIT),拒絕產出假 clean OOS。")
        return
    pit = historical_pit_universe()
    start = (pd.to_datetime(freeze_date) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    print(f"[forward] manifest={manifest_path}")
    print(f"[forward] 凍結日 {freeze_date} → forward 窗 {start} ~ {latest}"
          f"｜規則 hash {m.get('rules_sha256_16')}")

    res = backtest.backtest_portfolio(
        **pit.backtest_kwargs(), start_date=start,
    )
    if "summary" not in res:
        print(f"[forward] 無法回測:{res.get('error')}")
        return
    s = res["summary"]
    print(f"[forward] 交易 {s['n_trades']} 筆｜勝率 {s['win_rate']:.1%}｜"
          f"累積 {s['cum_ret']:+.2%}｜年化 {s['ann_ret']:+.2%}｜Sharpe {s['sharpe']}｜"
          f"MaxDD {s['max_drawdown']:.2%}")
    if s.get("data", {}).get("integrity_bypassed"):
        print("[forward] ⚠ integrity_bypassed=True：未還原價、含公司行動污染，非乾淨數字。")
    if s["n_trades"] < 30:
        print("[forward] ⚠ forward 交易數過少,統計檢定力不足,持續累積再看。")

    out = config.OUTPUT_DIR / f"forward_test_{freeze_date}.json"
    out.write_text(json.dumps({
        "manifest": manifest_path, "freeze_date": freeze_date,
        "forward_start": start, "data_latest": latest, "summary": s,
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"[forward] 已存:{out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default=None)
    run(ap.parse_args().manifest)
