# -*- coding: utf-8 -*-
"""
Forward-only 樣本外測試(唯一能產生真 clean OOS 的路徑)
======================================================
讀一份凍結的 FROZEN_MANIFEST(freeze_manifest.py 產出),把當時凍結的規則
**原封**套用,只對「凍結日之後才發生」的資料跑回測。因為規則在看到這些資料之前
就固定了,這段績效才是真正沒被擬合過的樣本外。

怎麼用(會累積,不是一次到位):
  1. 某天跑 freeze_manifest.py 凍結規則(記下 freeze_date 與當時 data_snapshot)。
  2. 之後推進 SNAPSHOT_END_DATE(或用 live 模式)抓到 freeze_date 之後的新資料。
  3. 跑 forward_test.py → 它只算 (freeze_date, 最新資料] 這段的績效 + 基準。
  4. 隨時間累積,forward 期越長,OOS 檢定力越高。

2026-08-15 修掉的 bug(這支原本產出的數字沒有一項可信)
------------------------------------------------------
1. **沒有基準**,docstring 卻寫「+ 買進持有基準」—— 而 AGENTS.md 的鐵則是
   「和基準比,不是和零比」(動態 universe 等權持有在 IS 就有 Sharpe 1.17)。
2. **只跑單一相位**:同一訊號換再平衡相位,Sharpe 實測從 -0.09 擺到 +1.09。
   單相位的 forward 數字等於挑路徑。
3. **吃引擎簽章預設** `rebalance_every=5 / top_n=3`,而不是凍結的
   20 日 / 10 檔 —— 驗證的規則跟凍結的規則是兩套。
4. **傳 symbols 卻繞過策略**:直接呼叫 `backtest_portfolio`,用的是 config 的
   FACTOR_WEIGHTS 路徑,不是被凍結的那個策略單元。
5. **同名輸出每次重跑就覆寫** `forward_test_{freeze_date}.json`:forward 紀錄
   本該是 append-only,覆寫等於可以重跑到好看再留下來。
6. **零測試**。

現在的形狀:manifest 先過 `validate_manifest`(legacy/不完整一律拒用)→
`apply_rules` 把 config 與策略規格原封套回去 → 走策略單元的**全相位**掃描
(`strategies/s19_chip_momentum.evaluate_sweep`,底層是 `evaluation/phases.py`
的共用 sweep,和正式 IS/OS 同一份實作;PIT 候選池由策略的 `build_panel` 從
`universes.historical_pit_universe()` 取)→ 附基準 → 寫**不可覆寫**的輸出 +
append-only ledger。

2026-08-15(P1-3)另外補上:**holdout 使用紀錄**。forward 窗也是 holdout,而且
是最不能被重複宣稱的那一種 —— 第二次跑同一段 forward 只是重現,不是新的樣本外。
每次成功的 forward 會 append 一列進 `outputs/holdout_ledger.jsonl`
(`evaluation/holdout.py`,帶雜湊鏈防靜默改寫),重疊到已看過的區間就標
`holdout_previously_seen=True` 並在 payload 裡把 `fresh_oos` 設成 False。
兩份 ledger 語意分工:`forward_test_runs.jsonl` 記「跑出什麼」(帶 Sharpe),
`holdout_ledger.jsonl` 記「看過哪一段」(刻意不放績效數字)。

2026-08-15(P1-1)另外修掉:相位聚合原本是這支自己寫的第三份實作,
`single_phase_debug` 用 `len(df) == 1` 反推 —— 那是拿**結果**當**意圖**:
20 相位掃描只有一相位有結果時會被誤標成 debug,而再平衡天數真的是 1 的正式
全相位掃描也會被誤標。現在旗標由掃描端宣告,forward 收到 debug 掃描直接 raise。

注意:未還原價下仍受 backtest 的 fail-closed 閘門保護(會 raise,除非顯式逃生門)。

用法:.venv/bin/python forward_test.py                 # 用最新 manifest
      .venv/bin/python forward_test.py --manifest outputs/FROZEN_MANIFEST_2026-07-24_x.json
"""
from __future__ import annotations

import argparse
import glob
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

import config
import freeze_manifest
from evaluation import holdout as holdout_ledger
from evaluation.phases import PhaseSweep

LEDGER_NAME = "forward_test_runs.jsonl"


def _latest_manifest() -> Optional[str]:
    files = sorted(glob.glob(str(config.OUTPUT_DIR / "FROZEN_MANIFEST_*.json")))
    return files[-1] if files else None


def _assert_forward_integrity(sweep: PhaseSweep) -> None:
    """forward 是唯一能升級證據等級的路徑,所以它的偏誤閘門要最嚴。

    三件事在**結果層面**驗證(不是相信自己傳對了參數):
      1. 掃描必須是**全相位**的。單相位只能 debug:同一訊號換相位 Sharpe 實測
         從 -0.09 擺到 +1.09,拿一條路徑宣稱 clean OOS 等於挑路徑。
      2. 每個相位的候選池都必須是 PIT(`candidate_pool_pit=True`)。
         舊版用 legacy 單日靜態池當候選池 = 用「凍結之後才知道誰熱門」決定
         forward 期能選誰,前視污染的正是這條路徑最不能污染的數字。
      3. 評估窗不得溢出最後一個訊號日(`days_beyond_last_pick=0`),
         否則 forward 會借用訊號用完後那段的走勢。
    """
    if sweep.single_phase_debug:
        raise RuntimeError(
            "[fail-closed] forward 收到 single_phase_debug 掃描:"
            "單相位只能 debug,不得作為 forward OOS 證據"
        )
    df = sweep.rows
    if df.empty:
        return
    bad_pit = df[df["candidate_pool_pit"] != True]      # noqa: E712
    if not bad_pit.empty:
        raise RuntimeError(
            "[fail-closed] forward 有相位的候選池不是 PIT"
            f"(相位 {bad_pit['phase'].tolist()[:5]}):拒絕產出假 clean OOS"
        )
    beyond = pd.to_numeric(df["days_beyond_last_pick"], errors="coerce").fillna(-1)
    if (beyond != 0).any():
        raise RuntimeError(
            "[fail-closed] forward 有相位的評估窗溢出最後訊號日"
            f"(days_beyond_last_pick={sorted(set(beyond.tolist()))}):"
            "訊號用完後仍在計績效"
        )


def _output_path(freeze_date: str, label: str, rules_hash: str,
                 run_stamp: str) -> Path:
    return (config.OUTPUT_DIR
            / f"forward_test_{freeze_date}_{label}_{rules_hash}_{run_stamp}.json")


def run(manifest_path: Optional[str] = None, *,
        now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    manifest_path = manifest_path or _latest_manifest()
    if not manifest_path:
        print("找不到任何 FROZEN_MANIFEST(先跑 freeze_manifest.py)。")
        return None
    m = json.loads(Path(manifest_path).read_text(encoding="utf-8"))

    # legacy / 不完整的 manifest 不得冒充可靠凍結版本 → raise,不是印警告照跑。
    status = freeze_manifest.validate_manifest(m)
    if not status.ok:
        raise ValueError(
            f"[fail-closed] {manifest_path} 不是可靠的凍結版本,拒絕用它宣稱 "
            f"forward OOS:{status.describe()}\n"
            "  → 請用 freeze_manifest.py 重新凍結(schema "
            f"{freeze_manifest.MANIFEST_SCHEMA})並從那天重新累積 forward 期。"
        )
    spec = freeze_manifest.apply_rules(m)     # config + 策略規格都原封套回去
    freeze_date = m["freeze_date"]
    label = m["label"]

    # forward 窗 = (freeze_date, 目前資料最新日]
    snap = getattr(config, "SNAPSHOT_END_DATE", "").strip()
    latest = snap or "live(最新)"
    if snap and pd.to_datetime(snap) <= pd.to_datetime(freeze_date):
        print(f"⚠ 目前資料快照 {snap} ≤ 凍結日 {freeze_date}:尚無凍結後的新資料。")
        print("  → 推進 SNAPSHOT_END_DATE 抓 freeze_date 之後的資料,再跑本測試。"
              " 現在 forward 期為空,這是正常的(規則剛凍結)。")
        return None

    mod = spec.module()          # 只接受註冊過的策略(見 strategies/spec.py)
    start = (pd.to_datetime(freeze_date) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    end = snap or None
    print(f"[forward] manifest={manifest_path}(reliability={status.reliability})")
    print(f"[forward] 策略 {spec.name}｜凍結日 {freeze_date} → forward 窗 "
          f"{start} ~ {latest}｜規則 hash {m.get('rules_sha256_16')}")
    for w in status.warnings:
        print(f"[forward] ⚠ {w}")

    # 候選池:策略的 build_panel 走 universes.historical_pit_universe()(月頻 PIT)。
    # 這裡不自己組 symbols —— 自己組就會變成第二條候選池路徑,而它遲早會退化成
    # 單日靜態池(這正是舊版做的事)。
    panel, symbols = mod.build_panel()
    if panel is None or len(panel) == 0:
        print("[forward] panel 為空,無法回測。")
        return None
    provider = panel.attrs.get("universe_provider")
    if provider is None:
        raise RuntimeError(
            "[fail-closed] 策略 panel 沒有帶 PIT universe_provider:"
            "forward 不接受無法證明是 PIT 的候選池"
        )

    # 跑滿所有等價相位(相位數 = 凍結的再平衡天數)。掃描與聚合都走
    # `evaluation.phases`,和正式 IS/OS 是同一份實作 —— 這裡不再自己寫迴圈,
    # 也不再自己算中位/最小/最差 MaxDD(舊版那份用 `len(df)==1` 反推
    # `single_phase_debug`,是拿結果當意圖)。
    sweep = mod.evaluate_sweep(panel, symbols, start, end,
                               universe_provider=provider, spec=spec)
    phases = sweep.rows
    if sweep.empty:
        print("[forward] forward 窗內沒有任何相位產出結果(可能訊號尚未出現)。")
        return None
    _assert_forward_integrity(sweep)
    stats = sweep.stats()

    # 基準:動態 universe 等權買進持有(無成本,樂觀上界)。沒有基準的 forward
    # 數字無法解讀 —— 普漲段任何 long-only 策略都會是正的。
    benchmark = mod.equal_weight_baseline(panel, start, end)
    if not benchmark:
        raise RuntimeError(
            "[fail-closed] 算不出基準:forward 結果不得在沒有基準的情況下報出"
        )

    excess = None
    if benchmark.get("sharpe") is not None and not np.isnan(benchmark["sharpe"]):
        excess = stats["sharpe_median"] - float(benchmark["sharpe"])

    print(f"[forward] 相位 {stats['n_phases']} 個｜Sharpe 中位 "
          f"{stats['sharpe_median']:.3f}(最小 {stats['sharpe_min']:.3f})｜"
          f"最差 MaxDD {stats['worst_max_drawdown']:.2%}｜"
          f"交易數中位 {stats['n_trades_median']:.0f}")
    print(f"[forward] 基準(等權買進持有)Sharpe {benchmark.get('sharpe')}"
          f"｜策略−基準(中位)= {excess}")
    if stats["n_trades_median"] < 30:
        print("[forward] ⚠ forward 交易數過少,統計檢定力不足,持續累積再看。")

    run_at = now or datetime.now()

    # 同名輸出不可覆寫:forward 紀錄是 append-only(否則可以重跑到好看再留)。
    # 這一步刻意排在寫 holdout 台帳**之前**:被擋下來的重跑什麼都沒揭露,
    # 不該在台帳留下一筆「看過」。
    out = _output_path(freeze_date, label, str(m.get("rules_sha256_16")),
                       run_at.strftime("%Y%m%dT%H%M%S"))
    if out.exists():
        raise FileExistsError(
            f"[fail-closed] {out.name} 已存在,拒絕覆寫 forward 紀錄"
            "(forward 是 append-only:重跑請保留每一次的結果)"
        )

    # forward 窗也是 holdout —— 而且是最不能被重複宣稱的那一種:第二次跑同一段
    # forward 只是重現,不是新的樣本外。實際評估到的右界取「資料最後一天」,
    # 不用 `end=None` 蒙混過去(台帳上一段沒有右界的區間等於沒有紀錄)。
    revealed_end = pd.Timestamp(panel["date"].max())
    if end:
        revealed_end = min(revealed_end, pd.Timestamp(end))
    holdout = holdout_ledger.record_reveal(
        strategy_hash=str(m.get("rules_sha256_16")),
        strategy_name=spec.name,
        os_start=start, os_end=revealed_end,
        source="forward_test.run", segment="forward",
        label=label, manifest=str(manifest_path),
        split_mode="forward_after_freeze_date",
        context={"freeze_date": freeze_date, "output": out.name,
                 "n_phases": stats.get("n_phases")},
        now=run_at,
    )
    if holdout["holdout_previously_seen"]:
        print("[forward] ⚠ 這段 forward 窗與已被看過的 holdout 重疊"
              f"({holdout['holdout_status']}, 重疊 "
              f"{holdout['previously_seen_days']} 天):這次**不是** fresh OOS,"
              f"真正沒看過的起點是 {holdout['fresh_os_start']}。")

    payload = {
        "manifest": str(manifest_path),
        "manifest_schema": m.get("manifest_schema"),
        "manifest_reliability": status.reliability,
        "manifest_warnings": status.warnings,
        "label": label,
        "strategy": spec.rules(),
        "rules_sha256_16": m.get("rules_sha256_16"),
        "freeze_git_commit": m.get("git_commit"),
        "run_git_commit": freeze_manifest._git_state(),
        "freeze_date": freeze_date,
        "forward_start": start,
        "forward_end": end,
        "data_latest": latest,
        "data_snapshot_at_freeze": m.get("data_snapshot_at_freeze"),
        "run_at": run_at.isoformat(timespec="seconds"),
        "phase_stats": stats,
        "phases": phases.to_dict(orient="records"),
        "benchmark_equal_weight_hold": benchmark,
        "excess_sharpe_vs_benchmark_median": excess,
        # holdout 使用紀錄跟著結果走:讀這份檔案的人不必去翻台帳,就看得到
        # 這段 forward 窗是不是第一次被這套規則揭露。
        "holdout": holdout,
        "manifest_holdout_boundaries": m.get("holdout"),
        "fresh_oos": bool(holdout["fresh_oos_claim_allowed"]),
        "evidence_note": (
            "forward-only 期間的結果。相位中位數/最小值與基準都在同一份檔案裡;"
            "交易數不足時不構成 edge 證據,也不自動升級任何策略的證據等級。"
            + ("" if holdout["fresh_oos_claim_allowed"] else
               f"｜此窗與已消耗的 holdout 重疊({holdout['holdout_status']}),"
               "只能當重現,不得宣稱 fresh OOS。")
        ),
    }

    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                   encoding="utf-8")
    ledger = config.OUTPUT_DIR / LEDGER_NAME
    with open(ledger, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "run_at": payload["run_at"], "manifest": payload["manifest"],
            "rules_sha256_16": payload["rules_sha256_16"],
            "freeze_date": freeze_date, "label": label,
            "forward_start": start, "forward_end": end,
            "sharpe_median": stats["sharpe_median"],
            "sharpe_min": stats["sharpe_min"],
            "benchmark_sharpe": benchmark.get("sharpe"),
            "output": out.name,
            # 指向 holdout 台帳的那一列。兩份 ledger 語意不重疊:這份記
            # 「跑出什麼」,holdout 台帳記「看過哪一段」。
            "holdout_ledger_seq": holdout["seq"],
            "holdout_previously_seen": holdout["holdout_previously_seen"],
        }, ensure_ascii=False, default=str) + "\n")
    print(f"[forward] 已存:{out}")
    print(f"[forward] 已追加紀錄:{ledger}")
    print(f"[forward] holdout 台帳 #{holdout['seq']}:"
          f"{holdout['holdout_status']}｜"
          f"{holdout_ledger.ledger_path()}")
    return payload


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default=None)
    run(ap.parse_args().manifest)
