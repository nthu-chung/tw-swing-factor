# -*- coding: utf-8 -*-
"""
凍結研究規則 manifest（clean-OOS 的第一步）
==========================================
本專案沒有真正乾淨的 OOS:所有權重/exit/門檻都反覆看過同一段 2024-2026 資料。
唯一能長出真 OOS 的方法 = 現在把「一整套規則」凍結成 immutable manifest，之後
只用 forward_test.py 對「凍結日之後才發生」的新資料 forward-only 驗證。

manifest 一旦寫出就**不可覆寫**（避免偷改規則再宣稱樣本外）。要改規則 = 開新 manifest、
重新累積 forward 期。

輸出:outputs/FROZEN_MANIFEST_<freeze_date>.json（immutable）。
用法:.venv/bin/python freeze_manifest.py
      .venv/bin/python freeze_manifest.py --label momentum_only_v1
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import datetime

import config


# 凍結的「規則」= 這些 config 欄位的當前值（load-bearing 參數全收）。
FROZEN_KEYS = [
    "FACTOR_WEIGHTS", "MIN_COMPOSITE", "TOP_N", "TREND_GUARD_ENABLED",
    "BT_EXIT_MODE", "BT_MA_EXIT", "BT_TREND_STOP_LOSS", "BT_MAX_HOLD_DAYS",
    "BT_HOLD_DAYS", "BT_TAKE_PROFIT", "BT_STOP_LOSS", "BT_MAX_POSITIONS",
    "BT_FEE", "BT_TAX", "BT_IC_HORIZON",
    "DYNAMIC_UNIVERSE_ENABLED", "DYNAMIC_UNIVERSE_TOP_N",
    "DYNAMIC_UNIVERSE_CANDIDATE_POOL", "DYNAMIC_UNIVERSE_LOOKBACK",
    "DYNAMIC_UNIVERSE_MIN_OBS", "DYNAMIC_UNIVERSE_MIN_AVG_VOLUME_LOTS",
    "DYNAMIC_UNIVERSE_MIN_AVG_TURNOVER",
    "MA_SHORT", "MA_LONG", "MOM_LOOKBACK", "MOM_RET_FULL", "MOM_NEAR_HIGH_FULL",
    "HIGH_LOOKBACK", "EXCLUDE_FINANCE", "EXCLUDE_ETF_PREFIX0",
    "MARKET_FILTER_ENABLED", "MARKET_FILTER_RULE", "MARKET_FILTER_RISKOFF_WEIGHT",
    "PRICE_DATASET",
]


def _git_hash() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(config.ROOT),
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except Exception:
        return "unknown"


def build_manifest(label: str) -> dict:
    freeze_date = datetime.now().strftime("%Y-%m-%d")
    rules = {}
    for k in FROZEN_KEYS:
        rules[k] = getattr(config, k, None)
    rules_json = json.dumps(rules, ensure_ascii=False, sort_keys=True, default=str)
    rules_hash = hashlib.sha256(rules_json.encode("utf-8")).hexdigest()[:16]
    return {
        "label": label,
        "freeze_date": freeze_date,
        "data_snapshot_at_freeze": getattr(config, "SNAPSHOT_END_DATE", ""),
        "git_commit": _git_hash(),
        "rules_sha256_16": rules_hash,
        "rules": rules,
        "note": (
            "IMMUTABLE。forward_test.py 只驗證 freeze_date 之後（且 SNAPSHOT_END_DATE > "
            "data_snapshot_at_freeze 時新抓到）的資料。規則要改請開新 manifest，勿覆寫。"
        ),
    }


def run(label: str) -> None:
    m = build_manifest(label)
    path = config.OUTPUT_DIR / f"FROZEN_MANIFEST_{m['freeze_date']}.json"
    if path.exists():
        print(f"⚠ {path.name} 已存在且不可覆寫（immutable）。"
              f"要改規則請換 --label 或等隔日再凍結。")
        return
    path.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已凍結規則 manifest（label={m['label']}, hash={m['rules_sha256_16']}）")
    print(f"  資料快照@凍結 = {m['data_snapshot_at_freeze']}｜git = {m['git_commit'][:10]}")
    print(f"  → {path}")
    print("  之後:推進 SNAPSHOT_END_DATE 抓新資料，再跑 forward_test.py 做真 OOS。")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="baseline")
    run(ap.parse_args().label)
