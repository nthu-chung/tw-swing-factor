# -*- coding: utf-8 -*-
"""
一次性快取遷移：把「不含查詢範圍」的舊快取檔重命名成含範圍戳的新 key
==================================================================
背景：`data.py` 的 cache key 原為 `{dataset}__{stock_id}__{snapshot}.pkl`，不含
history_days。於是 `fetch_price('2330')` 與 `fetch_price('2330', history_days=2000)`
命中同一個檔案、回傳完全相同的 482 列且零警告 —— 想抓更長歷史（例如涵蓋空頭段）
變成靜默 no-op。2026-08-15 修法把正規化天數編進檔名
（`{dataset}__{stock_id}__{snapshot}__d{days}.pkl`，見 `data.CacheScope`）。

本腳本把現存舊檔就地 rename 成「該資料集當時實際使用的預設天數」：全庫 grep 確認
repo 內沒有任何呼叫端傳自訂 `history_days`（都吃 `config.HISTORY_DAYS` /
`MARKET_HISTORY_DAYS`），且抽查快取內容也符合（`price__2330__2026-06-22` 起於
2024-06-24 ≈ snapshot-730 日、`market__TAIEX__2026-06-22` 起於 2022-12-20
≈ snapshot-1280 日）。因此舊檔內容 = 預設範圍，改名不會造假：現行凍結數字仍能
bit-identical 重現、不必重抓。

刻意的限制：
- **預設 dry-run**。要真的動檔案必須明確加 `--apply`（破壞性動作不自動執行）。
- 只處理 `data.py` 管理的資料集白名單。`disposition__ALL__*`、`notice__ALL__*`、
  `divresult__*`、`pitsnap__*` 有各自的命名規則（不吃 history_days），誤改會讓那些
  層讀不到檔。
- `info`（全市場清單）沒有查詢範圍，維持三段式檔名，不遷移。
- 不推測、不重抓、不改內容，只 rename；每個檔案保留它自己的快照戳。

用法：.venv/bin/python migrate_cache_range.py            # dry-run，只列印
      .venv/bin/python migrate_cache_range.py --apply    # 真的改名
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import config
import data

# dataset -> 當時實際使用的預設天數設定名。
_DATASET_DEFAULT_ATTR = {
    "price": "HISTORY_DAYS",
    "price_adj": "HISTORY_DAYS",
    "price_limit": "HISTORY_DAYS",
    "inst": "HISTORY_DAYS",
    "margin": "HISTORY_DAYS",
    "lending": "HISTORY_DAYS",
    "fholding": "HISTORY_DAYS",
}
# `market` 命名空間下有兩條序列，預設範圍不同（TAIEX 要多抓 MA200 暖身）。
_MARKET_DEFAULT_ATTR = {"TAIEX": "MARKET_HISTORY_DAYS", "VIX": "HISTORY_DAYS"}


def _default_attr(dataset: str, stock_id: str) -> str | None:
    """回傳該 (dataset, stock_id) 的預設天數設定名；不在白名單則 None（不動）。"""
    if dataset == "market":
        return _MARKET_DEFAULT_ATTR.get(stock_id)
    return _DATASET_DEFAULT_ATTR.get(dataset)


def plan(cache_dir: Path | None = None) -> tuple[list[tuple[Path, Path]], int]:
    """列出 (舊路徑, 新路徑)，不動檔案。回傳 (待遷移清單, 略過數)。"""
    cache_dir = Path(cache_dir or config.CACHE_DIR)
    moves: list[tuple[Path, Path]] = []
    skipped = 0
    for path in sorted(cache_dir.glob("*.pkl")):
        parts = path.name[:-4].split("__")
        if len(parts) != 3:          # 已含範圍戳（4 段）或非本層命名（如 pitsnap）
            skipped += 1
            continue
        dataset, stock_id, snapshot = parts
        attr = _default_attr(dataset, stock_id)
        if attr is None:             # info / disposition / notice / divresult ...
            skipped += 1
            continue
        tag = data.range_tag(default_attr=attr)   # 範圍戳格式只有 data.py 一份
        new_path = cache_dir / f"{dataset}__{stock_id}__{snapshot}__{tag}.pkl"
        if new_path.exists():        # 已經有新檔，不覆蓋（保留兩者供比對）
            skipped += 1
            continue
        moves.append((path, new_path))
    return moves, skipped


def main(apply: bool = False, cache_dir: Path | None = None) -> int:
    moves, skipped = plan(cache_dir)
    for old, new in moves:
        print(f"  {old.name} -> {new.name}")
        if apply:
            os.rename(old, new)
    verb = "已遷移" if apply else "將遷移（dry-run，未動檔）"
    print(f"\n{verb} {len(moves)} 個快取檔加上範圍戳；略過 {skipped} 個"
          f"（已含範圍戳、無範圍維度或非資料層命名）。")
    if not apply and moves:
        print("確認清單無誤後加 --apply 才會真的改名。")
    return len(moves)


if __name__ == "__main__":
    main(apply="--apply" in sys.argv[1:])
