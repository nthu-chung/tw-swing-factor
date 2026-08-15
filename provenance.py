# -*- coding: utf-8 -*-
"""執行環境的 provenance(目前只有 git 狀態)。

為什麼要獨立成一個模組
----------------------
「這個 Sharpe 是哪一份程式碼算出來的」跟「這個 Sharpe 是多少」一樣重要:改了
出場規則、改了成本模型、改了候選池,結果都會變,而報告裡只留數字的話,事後
沒有任何辦法把數字對回程式碼。過去只有 `freeze_manifest.py` 記 git 狀態,
回測 `summary` 完全沒有 —— 於是 `outputs/` 底下的每一份績效都對不到 commit。

兩邊必須共用同一份實作,否則「dirty 怎麼算」會慢慢分岔(例如一邊算 untracked
一邊不算),到時候兩份 provenance 講的是不同的事。

`git_state()` 預設在**同一個 process 內**快取:`run_full` 一次會呼叫幾十次
`backtest_portfolio`,每次三個 subprocess 是白花的成本;而一次研究執行中途
commit 不該讓前後兩段結果戳上不同 commit(那會讓 provenance 更難解讀,不是
更準)。要強制重讀請傳 `use_cache=False`(`freeze_manifest` 就是這樣用:凍結
是一次性動作,值得付重讀的成本)。
"""

from __future__ import annotations

import subprocess
from typing import Any, Dict, Optional

import config

_CACHED_STATE: Optional[Dict[str, Any]] = None

# git 不可用(沒裝 git、不是 repo、subprocess 被擋)時的誠實值。不可以留白或
# 猜一個 commit —— 「不知道」必須看得出來是「不知道」。
UNKNOWN = "unknown"


def _git(*args: str) -> Optional[str]:
    try:
        return subprocess.check_output(
            ["git", *args], cwd=str(config.ROOT), stderr=subprocess.DEVNULL,
        ).decode().strip()
    except Exception:
        return None


def git_state(*, use_cache: bool = True) -> Dict[str, Any]:
    """回傳 `{git_commit, git_branch, git_dirty, git_dirty_file_count}`。

    dirty 的工作樹代表這份結果對不到任何 commit —— **無法重現**,必須看得見,
    而不是靜默當成乾淨。
    """
    global _CACHED_STATE
    if use_cache and _CACHED_STATE is not None:
        return dict(_CACHED_STATE)

    commit = _git("rev-parse", "HEAD")
    status = _git("status", "--porcelain")
    dirty_files = [ln[3:] for ln in (status or "").splitlines() if ln.strip()]
    state = {
        "git_commit": commit or UNKNOWN,
        "git_branch": _git("rev-parse", "--abbrev-ref", "HEAD") or UNKNOWN,
        "git_dirty": bool(dirty_files),
        "git_dirty_file_count": len(dirty_files),
    }
    if use_cache:
        _CACHED_STATE = dict(state)
    return state


def reset_cache() -> None:
    """清掉 process 內的快取(測試用)。"""
    global _CACHED_STATE
    _CACHED_STATE = None


__all__ = ["git_state", "reset_cache", "UNKNOWN"]
