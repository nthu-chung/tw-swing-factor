# -*- coding: utf-8 -*-
"""
一次性快取遷移：把「未加快照戳」的舊快取檔重命名成含快照戳的新 key
================================================================
背景：data.py 的 cache key 原為 `{dataset}__{stock_id}.pkl`，不含 SNAPSHOT_END_DATE。
改 cutoff 時檔名仍命中舊檔 → 靜默用到與快照不符（甚至含未來）的資料。2026-07-24
修法把 snapshot 編進檔名（`{dataset}__{stock_id}__{snapshot}.pkl`）。

本腳本把現存舊檔就地 rename 成「當前凍結快照」的新名字，讓現行凍結數字仍能
bit-identical 重現（不重抓、不改資料）。之後改 snapshot 就會 miss → 真重抓。

用法：.venv/bin/python migrate_cache_stamp.py            # 依 config.SNAPSHOT_END_DATE
      .venv/bin/python migrate_cache_stamp.py --dry-run  # 只列印不動檔
"""
from __future__ import annotations

import glob
import os
import sys

import config


def main(dry_run: bool = False) -> None:
    snap = getattr(config, "SNAPSHOT_END_DATE", "").strip()
    if not snap:
        print("SNAPSHOT_END_DATE 為空（live 模式），不遷移。")
        return

    renamed = skipped = 0
    for path in glob.glob(str(config.CACHE_DIR / "*.pkl")):
        name = os.path.basename(path)[:-4]          # 去 .pkl
        parts = name.split("__")
        if len(parts) != 2:                          # 只處理未加戳的 dataset__id
            skipped += 1
            continue
        dataset, sid = parts
        new_path = config.CACHE_DIR / f"{dataset}__{sid}__{snap}.pkl"
        if new_path.exists():
            skipped += 1
            continue
        print(f"  {name}.pkl -> {dataset}__{sid}__{snap}.pkl")
        if not dry_run:
            os.rename(path, new_path)
        renamed += 1

    verb = "將遷移" if dry_run else "已遷移"
    print(f"\n{verb} {renamed} 個快取檔加上 __{snap} 戳記；略過 {skipped} 個"
          f"（非 dataset__id 格式或已存在）。")


if __name__ == "__main__":
    main(dry_run="--dry-run" in sys.argv[1:])
