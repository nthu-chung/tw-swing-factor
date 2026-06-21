# -*- coding: utf-8 -*-
"""
tw-swing-factor 統一入口
========================
台股波段多因子選股系統。

用法：
  python3 main.py screen                  # 今日選股（小集合）
  python3 main.py screen --pool 200       # 今日選股（成交值前200大池，推薦）
  python3 main.py screen --full           # 今日選股（全市場，較慢）
  python3 main.py screen --date 2026-05-20  # 指定日選股（回看當時）
  python3 main.py backtest                 # 回測 + 因子IC（小集合）
  python3 main.py backtest --full --top 5  # 全市場回測，每次選前5
  python3 main.py ic                       # 只看因子IC分析

參數：
  --pool N        用「成交值前 N 大」池（需先 python3 build_universe.py N）
  --full          用全市場 universe（預設小集合 SAMPLE_UNIVERSE）
  --date YYYY-MM-DD   指定選股日（僅 screen）
  --top N         每次選前 N 檔（預設 3）
  --rebalance N   每 N 個交易日換股一次（預設 5）
"""

from __future__ import annotations

import sys

import config
import screener
import backtest


def _parse_args(argv):
    args = {
        "cmd": "screen",
        "full": False,
        "date": None,
        "pool": None,
        "top": 3,
        "rebalance": 5,
    }
    if argv:
        args["cmd"] = argv[0]
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--full":
            args["full"] = True
        elif a == "--pool" and i + 1 < len(argv):
            args["pool"] = int(argv[i + 1]); i += 1
        elif a == "--date" and i + 1 < len(argv):
            args["date"] = argv[i + 1]; i += 1
        elif a == "--top" and i + 1 < len(argv):
            args["top"] = int(argv[i + 1]); i += 1
        elif a == "--rebalance" and i + 1 < len(argv):
            args["rebalance"] = int(argv[i + 1]); i += 1
        i += 1
    return args


def main():
    args = _parse_args(sys.argv[1:])
    sample = not args["full"]

    if not config.FINMIND_TOKEN:
        print("⚠️  未偵測到 FINMIND_TOKEN。請設定環境變數，或確認 "
              "taiwan-industry-analyzer/backend/.env 內有 FINMIND_TOKEN。")
        return

    cmd = args["cmd"]
    if cmd == "screen":
        screener.screen(as_of=args["date"], sample=sample,
                        pool=args["pool"], verbose=True)
    elif cmd == "backtest":
        backtest.run_full(sample=sample, top_n=args["top"],
                          rebalance_every=args["rebalance"])
    elif cmd == "ic":
        ic_df = backtest.factor_ic(sample=sample)
        backtest._print_ic(ic_df)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
