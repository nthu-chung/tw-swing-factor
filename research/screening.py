# -*- coding: utf-8 -*-
"""把 Golden Path 的最後一份完整訊號快照整理成人可讀候選名單。

這裡**不算因子、不重排、不產生交易指令**。排名只來自已通過唯一 validator 的
SignalFrame；panel 只用來補股票名稱、產業與當日收盤價。這樣研究回測看到的排名與
人類打開 CSV 看到的排名不會分成兩套。
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Iterable, Optional

import numpy as np
import pandas as pd

from research.signal_validation import validate_signal_frame


DISPLAY_PANEL_COLUMNS = ("name", "industry", "close", "close_raw")
DISPLAY_SIGNAL_COLUMNS = (
    "date", "rank", "stock_id", "name", "industry",
    "close_as_traded", "close_adjusted", "price_space",
    "raw_score", "alpha_score", "rank_pct", "reason_codes",
    "strategy_id", "strategy_version", "snapshot_complete",
)

# 價格空間必須明講,不能只印一個數字(見 `_attach_price_columns`)。
PRICE_SPACE_AS_TRADED = "as_traded"
PRICE_SPACE_ADJUSTED_ONLY = "adjusted_only"


def _attach_price_columns(snapshot: pd.DataFrame) -> pd.DataFrame:
    """把價格拆成 `close_as_traded` / `close_adjusted` 兩欄,並標記空間。

    為什麼不能只印一個 `close`:panel 的 `close` 是**還原價**。它有兩個性質讓它
    不適合給人看 ——

      1. 它不是券商螢幕上的數字。實測 6515 在 2025-10-21 的還原價 2526.24,
         實際成交價 2450.00,差 3.1%。拿還原價去對單會對不起來。
      2. 用 `series_start` 錨定時,還原價的**絕對水準會隨抓取窗改變**(同一檔
         同一天,兩年窗給 2526.24、500 日窗給 2497.85)。報酬不變,所以回測是
         對的;但一個會隨抓取範圍變動的數字不該印給人當價格看。

    拿不到原始價時**不假裝**:`close_as_traded` 留空,`price_space` 標成
    `adjusted_only`,文字報表也會把「還原價,非成交價」印出來。少一個數字好過
    給一個看起來像成交價的數字。
    """
    has_raw = "close_raw" in snapshot.columns
    snapshot["close_as_traded"] = (
        pd.to_numeric(snapshot["close_raw"], errors="coerce") if has_raw
        else float("nan"))
    snapshot["close_adjusted"] = (
        pd.to_numeric(snapshot["close"], errors="coerce")
        if "close" in snapshot.columns else float("nan"))
    snapshot["price_space"] = np.where(
        snapshot["close_as_traded"].notna(),
        PRICE_SPACE_AS_TRADED, PRICE_SPACE_ADJUSTED_ONLY)
    return snapshot.drop(columns=[c for c in ("close", "close_raw")
                                  if c in snapshot.columns])


def build_candidate_screen(signal_frame: pd.DataFrame, *,
                           panel: Optional[pd.DataFrame] = None,
                           as_of=None, top_n: int = 10) -> pd.DataFrame:
    """回傳最後一個完整快照的前 N 名；它是候選清單，不是買賣指令。"""
    if int(top_n) < 1:
        raise ValueError("top_n 必須 >= 1")
    limit = pd.Timestamp(as_of) if as_of is not None else None
    validated = validate_signal_frame(
        signal_frame, who="candidate_screen", as_of_max=limit)
    signals = validated.frame
    if limit is not None:
        signals = signals[signals["date"] <= limit]
    if signals.empty:
        raise ValueError("[fail-closed] 指定 as-of 之前沒有訊號快照")

    snapshot_date = pd.Timestamp(signals["date"].max())
    snapshot = signals[signals["date"].eq(snapshot_date)].copy()
    if not bool(snapshot["snapshot_complete"].all()):
        raise ValueError(
            f"[fail-closed] {snapshot_date.date()} 的訊號快照不完整；"
            "缺列不能被誤讀為掉出排名")
    snapshot = snapshot.sort_values(["rank", "stock_id"], kind="mergesort")
    snapshot = snapshot[snapshot["rank"] <= int(top_n)].copy()

    if panel is not None and not panel.empty:
        required = {"date", "stock_id"}
        if not required.issubset(panel.columns):
            raise ValueError("panel 至少要有 date / stock_id 才能補顯示欄位")
        p = panel.copy()
        p["date"] = pd.to_datetime(p["date"], errors="coerce")
        p["stock_id"] = p["stock_id"].astype(str)
        p = p[p["date"].eq(snapshot_date)]
        if p.duplicated(["date", "stock_id"]).any():
            raise ValueError("[fail-closed] panel 的 (date, stock_id) 不唯一")
        enrich = [c for c in DISPLAY_PANEL_COLUMNS if c in p.columns]
        if enrich:
            snapshot = snapshot.merge(
                p[["date", "stock_id", *enrich]],
                on=["date", "stock_id"], how="left", validate="one_to_one")

    for col in ("name", "industry"):
        if col not in snapshot.columns:
            snapshot[col] = ""
    snapshot = _attach_price_columns(snapshot)
    snapshot["list_type"] = "research_candidate_not_order"
    snapshot["as_of"] = str(snapshot_date.date())

    ordered = [c for c in DISPLAY_SIGNAL_COLUMNS if c in snapshot.columns]
    extras = [c for c in snapshot.columns if c not in ordered]
    return snapshot[[*ordered, *extras]].reset_index(drop=True)


def format_candidate_screen(frame: pd.DataFrame) -> str:
    """產生可直接貼給人看的文字；不使用「買進／賣出」字樣。"""
    if frame is None or frame.empty:
        return "候選清單為空"
    first = frame.iloc[0]
    lines = [
        "=" * 72,
        f"量化候選清單（不是交易指令） | as-of {first.get('as_of', '')}",
        f"策略 {first.get('strategy_id', '')} / {first.get('strategy_version', '')}",
        "=" * 72,
    ]
    for _, row in frame.iterrows():
        name = str(row.get("name") or "").strip()
        label = f"{row['stock_id']} {name}".strip()
        traded, adjusted = row.get("close_as_traded"), row.get("close_adjusted")
        if traded is not None and not pd.isna(traded):
            close_text = f" 收盤 {float(traded):.2f}"
        elif adjusted is not None and not pd.isna(adjusted):
            # 只有還原價時要講清楚,否則人會拿它去對券商畫面而對不起來。
            close_text = f" 還原價 {float(adjusted):.2f}(非成交價)"
        else:
            close_text = ""
        industry = str(row.get("industry") or "").strip()
        industry_text = f" [{industry}]" if industry else ""
        lines.append(
            f"{int(row['rank']):>2}. {label:<18}{industry_text:<12} "
            f"score {float(row['raw_score']):.6f}{close_text}")
        reason = str(row.get("reason_codes") or "").strip()
        if reason:
            lines.append(f"    理由碼: {reason}")
    lines.extend([
        "-" * 72,
        "這是策略排名候選，不代表實際下單；部位與成交仍由 policy／事件引擎決定。",
        "價格欄為原始成交價（as-traded）；標「還原價」者代表拿不到原始價，"
        "不可直接拿去對券商畫面。",
    ])
    return "\n".join(lines)


def _read_run(run_dir: Path) -> pd.DataFrame:
    candidate = run_dir / "candidate_screen.csv"
    if candidate.exists():
        return pd.read_csv(candidate, dtype={"stock_id": str})
    signals = run_dir / "signals.csv"
    if not signals.exists():
        raise FileNotFoundError(f"run 目錄缺 signals.csv:{run_dir}")
    frame = pd.read_csv(signals, dtype={"stock_id": str})
    return build_candidate_screen(frame)


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="顯示 Golden Path 的人類可讀候選清單")
    parser.add_argument("--run-dir", required=True)
    args = parser.parse_args(list(argv) if argv is not None else None)
    print(format_candidate_screen(_read_run(Path(args.run_dir))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
