"""Current TWSE momentum/flow screen using official daily public endpoints.

This is a research-prioritization helper, not an order generator.  It keeps the
repo's frozen backtest snapshot untouched and writes a separate current screen.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests

import security_type


TWSE_PRICE_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
TWSE_FLOW_URL = "https://www.twse.com.tw/rwd/zh/fund/T86"
OUTPUT = Path(__file__).resolve().parent / "outputs" / "current_twse_screen.csv"


def _number(value) -> float:
    text = str(value).replace(",", "").strip()
    if text in {"", "--", "---", "nan"}:
        return np.nan
    return pd.to_numeric(text, errors="coerce")


def _regular_equity_mask(sid: pd.Series) -> pd.Series:
    """上市／上櫃**普通股**遮罩(證券別白名單,與回測用的是同一份判定)。

    原 bug(2026-08-15 修):這裡只檢查「4 碼數字、非 00 開頭」,而 DR(9105 泰金寶-DR
    這類 91xx)與創新板股票的代號同樣是 4 碼數字 —— 光看代號永遠分不出來,一定要
    查 TaiwanStockInfo 的 type / industry_category。判定共用 `security_type`,
    `universe` / `pit_universe` / 這裡只能有一個答案。

    `on_unknown="exclude"`:這支是 live 的研究排序工具,不是證據來源;當天剛掛牌
    還沒進 stock_info 的代號排掉並記數(`security_type.exclusion_summary()` 看得到),
    比中斷整份 screen 合理。方向仍是保守的 —— 不知道就不放行。
    """
    return security_type.eligible_mask(
        sid, source="current_watchlist._regular_equity_mask",
        on_unknown="exclude")


def fetch_price_day(session: requests.Session, day: date) -> pd.DataFrame:
    response = session.get(
        TWSE_PRICE_URL,
        params={
            "date": day.strftime("%Y%m%d"),
            "type": "ALLBUT0999",
            "response": "json",
        },
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("stat") != "OK":
        return pd.DataFrame()
    tables = [
        table
        for table in payload.get("tables", [])
        if "每日收盤行情" in str(table.get("title", ""))
    ]
    if not tables:
        return pd.DataFrame()
    table = tables[0]
    frame = pd.DataFrame(table["data"], columns=table["fields"])
    frame = frame.rename(
        columns={
            "證券代號": "stock_id",
            "證券名稱": "name",
            "成交股數": "volume",
            "成交金額": "turnover",
            "開盤價": "open",
            "最高價": "high",
            "最低價": "low",
            "收盤價": "close",
        }
    )
    keep = ["stock_id", "name", "volume", "turnover", "open", "high", "low", "close"]
    frame = frame[keep].copy()
    frame["stock_id"] = frame["stock_id"].astype(str).str.strip()
    frame["name"] = frame["name"].astype(str).str.strip()
    for column in ["volume", "turnover", "open", "high", "low", "close"]:
        frame[column] = frame[column].map(_number)
    frame["date"] = pd.Timestamp(day)
    normal = _regular_equity_mask(frame["stock_id"])
    valid = (frame[["volume", "turnover", "open", "high", "low", "close"]] > 0).all(axis=1)
    return frame[normal & valid].reset_index(drop=True)


def fetch_flow_day(session: requests.Session, day: date) -> pd.DataFrame:
    response = session.get(
        TWSE_FLOW_URL,
        params={
            "date": day.strftime("%Y%m%d"),
            "selectType": "ALLBUT0999",
            "response": "json",
        },
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("stat") != "OK" or not payload.get("data"):
        return pd.DataFrame()
    frame = pd.DataFrame(payload["data"], columns=payload["fields"])
    frame = frame.rename(
        columns={
            "證券代號": "stock_id",
            "外陸資買賣超股數(不含外資自營商)": "foreign_net",
            "投信買賣超股數": "trust_net",
            "三大法人買賣超股數": "institution_net",
        }
    )
    keep = ["stock_id", "foreign_net", "trust_net", "institution_net"]
    frame = frame[keep].copy()
    frame["stock_id"] = frame["stock_id"].astype(str).str.strip()
    for column in ["foreign_net", "trust_net", "institution_net"]:
        frame[column] = frame[column].map(_number).fillna(0.0)
    frame["date"] = pd.Timestamp(day)
    return frame[_regular_equity_mask(frame["stock_id"])].reset_index(drop=True)


def build_screen(as_of: date, calendar_days: int = 70) -> tuple[pd.DataFrame, pd.Timestamp]:
    session = requests.Session()
    prices, flows = [], []
    cursor = as_of
    while cursor >= as_of - timedelta(days=calendar_days):
        price = fetch_price_day(session, cursor)
        if not price.empty:
            prices.append(price)
            if len(flows) < 10:
                flow = fetch_flow_day(session, cursor)
                if not flow.empty:
                    flows.append(flow)
        cursor -= timedelta(days=1)

    if not prices:
        raise RuntimeError("No TWSE daily price data was returned.")
    price_panel = pd.concat(prices, ignore_index=True).sort_values(["stock_id", "date"])
    flow_panel = (
        pd.concat(flows, ignore_index=True)
        if flows
        else pd.DataFrame(columns=["stock_id", "date", "institution_net"])
    )
    latest = price_panel["date"].max()

    rows = []
    for stock_id, group in price_panel.groupby("stock_id", sort=False):
        group = group.sort_values("date").tail(45).copy()
        if len(group) < 21 or group["date"].iloc[-1] != latest:
            continue
        current = group.iloc[-1]
        prior20 = group.iloc[-21:-1]
        stock_flow = flow_panel[flow_panel["stock_id"] == stock_id].sort_values("date")
        inst5 = stock_flow.tail(5)["institution_net"].sum() if not stock_flow.empty else 0.0
        rows.append(
            {
                "as_of": latest,
                "stock_id": stock_id,
                "name": current["name"],
                "close": current["close"],
                "ret_5d": current["close"] / group["close"].iloc[-6] - 1
                if len(group) >= 6
                else np.nan,
                "ret_20d": current["close"] / group["close"].iloc[-21] - 1,
                "near_20d_high": current["close"] / prior20["high"].max() - 1,
                "above_ma20": current["close"] / prior20["close"].mean() - 1,
                "vol_ratio": current["volume"] / prior20["volume"].mean(),
                "avg_turnover_20d": prior20["turnover"].mean(),
                "inst_net_5d": inst5,
                "inst_to_volume_5d": inst5 / max(prior20["volume"].mean() * 5, 1),
            }
        )

    screen = pd.DataFrame(rows)
    screen = screen[
        (screen["avg_turnover_20d"] >= 20_000_000)
        & (screen["close"] >= 10)
        & (screen["above_ma20"] > 0)
    ].copy()
    score_fields = {
        "ret_5d": 0.15,
        "ret_20d": 0.25,
        "near_20d_high": 0.20,
        "above_ma20": 0.10,
        "vol_ratio": 0.10,
        "inst_to_volume_5d": 0.20,
    }
    screen["screen_score"] = 0.0
    for field, weight in score_fields.items():
        screen["screen_score"] += screen[field].rank(pct=True).fillna(0.0) * weight
    screen = screen.sort_values(
        ["screen_score", "avg_turnover_20d"], ascending=[False, False]
    ).reset_index(drop=True)
    screen["technical_rank"] = np.arange(1, len(screen) + 1)
    return screen, latest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--as-of", default=date.today().isoformat())
    args = parser.parse_args()
    requested = datetime.strptime(args.as_of, "%Y-%m-%d").date()
    screen, latest = build_screen(requested)
    OUTPUT.parent.mkdir(exist_ok=True)
    screen.to_csv(OUTPUT, index=False, encoding="utf-8-sig")
    print(f"TWSE screen as of {latest.date()} ({len(screen)} eligible names)")
    print(screen.head(40).to_string(index=False))
    print(f"Saved: {OUTPUT}")


if __name__ == "__main__":
    main()
