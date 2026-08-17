# -*- coding: utf-8 -*-
"""
逐檔預抓資料到本地快取（給回測用）。
FinMind 免費版有限流，所以一檔一檔抓、印進度、失敗略過。
"""
from __future__ import annotations

import sys
import time

from universes import build as build_universe
import data as data_mod


def run(top_n: int = 100):
    ids = build_universe.load(top_n)
    if not ids:
        print(f"找不到 top{top_n} 池，先跑：python3 build_universe.py {top_n}")
        return
    print(f"開始預抓 {len(ids)} 檔資料（price + inst + margin）...\n")
    ok = fail = 0
    for i, sid in enumerate(ids, 1):
        try:
            b = data_mod.fetch_bundle(sid)
            n_p = len(b["price"]); n_i = len(b["inst"]); n_m = len(b["margin"])
            status = "✓" if n_p > 0 else "✗(無價格)"
            if n_p > 0:
                ok += 1
            else:
                fail += 1
            print(f"  [{i:>3}/{len(ids)}] {sid}  price={n_p:>4} inst={n_i:>4} margin={n_m:>4}  {status}")
        except Exception as e:
            fail += 1
            print(f"  [{i:>3}/{len(ids)}] {sid}  失敗：{e}")
        sys.stdout.flush()
    print(f"\n完成：成功 {ok}、失敗/無資料 {fail}")


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    run(n)
