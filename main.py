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
  python3 main.py backtest --full --pool 300 --universe-top 100 --top 5
                                            # long-only 動態 top100（候選 top300）
  python3 main.py ic                       # 只看因子IC分析

參數：
  --pool N        動態模式的候選池大小（需先 python3 build_universe.py N）
  --universe-top N 每個訊號日依20日平均成交值取前 N（預設100）
  --static-universe 關閉動態 universe，僅供 legacy 對照
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
import universe as uni


def _parse_args(argv):
    args = {
        "cmd": "screen",
        "full": False,
        "date": None,
        "pool": None,
        "top": 3,
        "rebalance": 5,
        "universe_top": config.DYNAMIC_UNIVERSE_TOP_N,
        "static_universe": False,
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
        elif a == "--universe-top" and i + 1 < len(argv):
            args["universe_top"] = int(argv[i + 1]); i += 1
        elif a == "--static-universe":
            args["static_universe"] = True
        i += 1
    return args


def main():
    args = _parse_args(sys.argv[1:])
    # 指定 --pool 即代表研究候選池，不再走 14 檔 sample smoke test。
    sample = (not args["full"]) and args["pool"] is None

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
                          rebalance_every=args["rebalance"],
                          pool=args["pool"],
                          dynamic_enabled=not args["static_universe"],
                          universe_top_n=args["universe_top"])
    elif cmd == "ic":
        symbols = None
        if not sample:
            if args["static_universe"]:
                symbols = uni.get_universe(
                    top_n=args["pool"] or args["universe_top"]
                )
            else:
                symbols = uni.get_research_candidates(
                    universe_top_n=args["universe_top"],
                    candidate_pool_n=args["pool"],
                )
        ic_df = backtest.factor_ic(
            symbols=symbols,
            sample=sample,
            dynamic_enabled=(not args["static_universe"]) and not sample,
            universe_top_n=args["universe_top"],
        )
        backtest._print_ic(ic_df)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
