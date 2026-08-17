# -*- coding: utf-8 -*-
"""run directory、atomic write 與輸出表格。

三條刻意的限制:

1. **run directory 唯一且不可覆寫。** 同一個 run_id 已存在就 raise,不覆蓋 ——
   覆蓋掉的那一份可能正是別人引用過的結果,而覆蓋不會留下任何痕跡。
2. **atomic write。** 先寫 `.tmp` 再 `os.replace`,中斷時不會留下半份 JSON
   讓下游誤讀成完整結果。
3. **產物不進版控。** 只寫進呼叫端指定的目錄(正式預設
   `outputs/research_runs/<run_id>/`),`preflight.py` 的 outputs 白名單也不放行
   這些 CSV/JSON。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

import pandas as pd

TABLES = ("signals", "phase_results", "decisions", "orders", "trades",
          "equity_curve", "candidate_screen")
DOCUMENTS = ("manifest", "summary", "audit")


_PATH_UNSAFE = ("/", "\\", "\x00")


def _reject_path_tricks(value: str, *, what: str) -> str:
    """拒絕(不是消毒)會跳出 output 目錄的片段。

    為什麼是拒絕而不是把 `/` 換成 `-`:消毒會讓 `--stamp ../../etc` 靜默變成
    另一個名字,呼叫端拿到的 run 目錄跟他要求的不同,而且不會有人發現。run id
    要進檔名也要進報告,名字被偷偷改掉等於結果對不回請求。
    """
    text = str(value)
    if not text.strip():
        raise ValueError(f"[fail-closed] {what} 不得為空白")
    if any(ch in text for ch in _PATH_UNSAFE) or ".." in text:
        raise ValueError(
            f"[fail-closed] {what}={value!r} 含路徑分隔或 '..';"
            "run 目錄名不接受會跳出輸出目錄的片段(這裡拒絕而不是消毒,"
            "消毒會讓你拿到一個名字被偷改過、對不回請求的目錄)")
    return text


def build_run_id(*, strategy_id: str, run_hash: str, stamp: str) -> str:
    """`<stamp>__<strategy>__<run_hash>`。stamp 由呼叫端提供(可重現)。"""
    _reject_path_tricks(stamp, what="stamp")
    _reject_path_tricks(strategy_id, what="strategy_id")
    _reject_path_tricks(run_hash, what="run_hash")
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in strategy_id)
    return f"{stamp}__{safe}__{run_hash}"


@dataclass(frozen=True)
class RunDirectory:
    path: Path
    run_id: str

    def file(self, name: str) -> Path:
        return self.path / name


def create_run_directory(output_dir, run_id: str) -> RunDirectory:
    # 這是公開入口,不保證呼叫端一定先經過 `build_run_id`。
    _reject_path_tricks(run_id, what="run_id")
    base = Path(output_dir)
    base.mkdir(parents=True, exist_ok=True)
    run_path = base / run_id
    if run_path.exists():
        raise FileExistsError(
            f"[fail-closed] run directory 已存在:{run_path}。"
            "不覆寫 —— 覆蓋掉的那一份可能已經被引用過,而覆蓋不留痕跡")
    run_path.mkdir(parents=False, exist_ok=False)
    return RunDirectory(path=run_path, run_id=run_id)


def _atomic_write(path: Path, payload: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, path)


def write_json(run: RunDirectory, name: str, payload: Mapping[str, Any]) -> Path:
    path = run.file(f"{name}.json")
    _atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=1,
                                   sort_keys=True, default=str))
    return path


def write_text(run: RunDirectory, name: str, payload: str) -> Path:
    path = run.file(f"{name}.txt")
    _atomic_write(path, str(payload))
    return path


def write_table(run: RunDirectory, name: str,
                frame: Optional[pd.DataFrame]) -> Path:
    """寫一張 CSV。空表也要寫(而且帶欄位),不留下「檔案不存在」的歧義。"""
    path = run.file(f"{name}.csv")
    df = frame if isinstance(frame, pd.DataFrame) else pd.DataFrame(frame or [])
    tmp = path.with_suffix(".csv.tmp")
    df.to_csv(tmp, index=False, encoding="utf-8-sig")
    os.replace(tmp, path)
    return path


def write_run(run: RunDirectory, *, manifest: Mapping[str, Any],
              summary: Mapping[str, Any], audit: Mapping[str, Any],
              tables: Mapping[str, Any]) -> Dict[str, str]:
    """一次寫完整份 run。回傳 {名稱: 路徑}。"""
    written: Dict[str, str] = {}
    for name, payload in (("manifest", manifest), ("summary", summary),
                          ("audit", audit)):
        written[name] = str(write_json(run, name, payload))
    for name in TABLES:
        written[name] = str(write_table(run, name, tables.get(name)))
    missing = [n for n in (*DOCUMENTS, *TABLES) if n not in written]
    if missing:
        raise RuntimeError(f"[fail-closed] run artifacts 不完整,缺 {missing}")
    return written
