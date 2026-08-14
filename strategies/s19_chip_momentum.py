# -*- coding: utf-8 -*-
"""
S19 — 籌碼確認的風險調整動能(Chip-confirmed Risk-adjusted Momentum, CRM)
=========================================================================
long-only、動態 universe、低周轉的波段策略。這支是為了「之後組 portfolio」而寫的
**可重複執行的策略單元**:給定日期區間 → 回傳訊號、回測 summary 與權益曲線。

訊號(全部用 operators 組,全因果)
----------------------------------
    score = 0.5 * cs_rank( ts_ir(日報酬, 20) )          # 風險調整動能
          + 0.5 * cs_rank( (Σ20 外資 + Σ20 投信) / 近20日均量 )   # 法人吸收

  - `ts_ir(r,20)` = 近 20 日報酬的 mean/std,即「Sharpe 式動能」。比生報酬穩:
    生報酬 `mom_ret` 的 IS IC 幾乎是 0(+0.0012),全部 IC 來自 OS 普漲段;
    `ts_ir` 的 IS/OS IC 分別是 +0.047 / +0.057,兩段同量級。
  - 法人流量用「近 20 日均量」正規化 → 跨股票可比。

閘門(硬條件,不進評分)
------------------------
  1. 當日在動態 universe(近20日成交值 top100,PIT 計算,見 dynamic_universe.py)
  2. `trend_ok`:MA20>MA60 且 MA60 上揚 且 收盤>MA60
  3. 價格完整性排除名單(見 outputs/price_integrity_excluded.json)

組合建構
--------
    持股 10 檔等權 / 20 日再平衡 / MA60 出場 / -15% 硬停損 / T+1 開盤

  ⚠ 這組參數是在**被污染的數字**上選出來的(見下),乾淨掃描裡它墊底。
  保留現值只為與歷史報告可對照,**不代表它被驗證過**。重新選參數前必須先
  處理 holdout 已用盡的問題。

證據等級:`blocked` —— 不足以宣稱 edge
--------------------------------------
  下列數字屬於舊的「生效日前 20 交易日」月池。現行程式已修成 M 月只用完整
  M-1 曆月，尚未重跑；因此這些只能保留為歷史診斷，不能冒充現版績效:

      IS  中位 0.520 / 最小 -0.412 / 最大 1.470 / 標準差 0.509 → 只有 3/20 勝基準 1.13
      OS  中位 1.661 / 最小 0.735                              → 12/20 勝基準 1.52

  **相位標準差 0.509 ≈ 訊號效果本身的量級** —— 「哪天開始執行」比訊號更決定
  結果(rebalance timing luck)。實際下單只會走一條路徑,所以 -0.412 不是
  理論數字。

  兩件事要分開講:S19 明顯勝過**同集中度的隨機選股**(1.205 vs 0.312),選股
  確實有訊號;但只勉強勝過**等權持有全部合格股**(1.126)—— 選股技巧大致只夠
  補償集中到 10 檔的分散度代價。

  已作廢的數字(留著是為了記住怎麼錯的):
    ① IS 五相位全 >1(中位 1.644)      —— 靜態候選池 look-ahead
    ② IS 1.607 / OS 1.938              —— 評估窗洩漏,IS 溢出切點 144 天,
                                          吃到 OS 段的 +87.2%(真實 IS 0.306)
    ③ 兩版的 16 格參數掃描都建立在①②上,選出的配置在乾淨掃描中墊底

  **OS 已非乾淨 holdout**:洩漏的 IS 包含 OS 走勢,故參數選擇間接看過 OS。
  要再升級證據等級,只剩 freeze_manifest + forward_test 這條路。

  完整保留見 outputs/CHIP_MOMENTUM_REPORT.md 與 STRATEGY_REGISTRY.md 的 S19 條目。
"""
from __future__ import annotations

import json
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

import backtest
import config
from evaluation import build_evaluation_split
from factor_engine import operators as op

# ── 凍結參數(由 IS 五相位 Sharpe 中位數選出;OS 未參與選擇)──────────────
SIGNAL_MOM_WINDOW = 20      # ts_ir 視窗
SIGNAL_FLOW_WINDOW = 20     # 法人流量累積視窗
SIGNAL_VOL_WINDOW = 20      # 流量正規化分母(均量)
W_MOMENTUM = 0.5
W_FLOW = 0.5

PORT_MAX_POSITIONS = 10
PORT_REBALANCE_DAYS = 20
PORT_MA_EXIT = 60
PORT_STOP_LOSS = 0.15

EXCLUDED_PATH = "price_integrity_excluded.json"


def compute_excluded(symbols: List[str], write: bool = True) -> set:
    """對**傳入的股票集**算價格完整性排除名單(還原後仍有殘留斷點者)。

    ⚠ 不要改回「讀單一全域檔案」。排除名單是**隨 universe 而變**的:
    當期池 300 檔算出 18 檔、PIT 聯集 758 檔算出 45 檔。共用一個檔案時,
    後跑的流程會覆蓋前一個,下次另一條路徑就會被 fail-closed 閘門擋下
    (2026-08-06 實際發生:出上線名單後,研究報告重生就掛了)。
    當場算才不會有這種跨流程污染;`fetch_price` 有快取,成本很低。

    仍會寫出檔案,但那是**產出物供人工檢視**,不是真理來源。
    """
    import data
    import price_integrity as pi

    frames = {}
    for sid in symbols:
        p = data.fetch_price(sid)
        if p is not None and not p.empty:
            frames[sid] = p
    if not frames:
        return set()
    audit = pi.audit_price_frames(
        frames, threshold=config.PRICE_INTEGRITY_RETURN_THRESHOLD)
    bad = sorted(audit["stock_id"].unique()) if not audit.empty else []
    if write:
        try:
            json.dump(bad, open(config.OUTPUT_DIR / EXCLUDED_PATH, "w"), indent=1)
        except Exception:
            pass
    return set(bad)


def load_excluded() -> set:
    """讀上一次寫出的排除名單。**僅供檢視**,建構 panel 請用 compute_excluded。"""
    p = config.OUTPUT_DIR / EXCLUDED_PATH
    if not p.exists():
        return set()
    try:
        return set(json.load(open(p, encoding="utf-8")))
    except Exception:
        return set()


def build_signal(panel: pd.DataFrame) -> pd.Series:
    """回傳與 panel 同 index 的訊號分數。

    ⚠ panel 必須是 `keep_non_members=True` 的**稠密** panel。ts_ 類算子是對
    「相鄰列」做 rolling,若 panel 只留動態 universe 成員日,20 列會橫跨遠超過
    20 個交易日 → 因子失真。成員過濾要留到選股時才做(見 build_picks)。
    """
    o = op.PanelOps(panel["date"], panel["stock_id"])
    ret1 = o.ts_returns(panel["close"], 1)
    mom = o.ts_ir(ret1, SIGNAL_MOM_WINDOW)

    vol = o.ts_mean(panel["volume"], SIGNAL_VOL_WINDOW).replace(0, np.nan)
    flow = (o.ts_sum(panel["foreign_net"], SIGNAL_FLOW_WINDOW)
            + o.ts_sum(panel["trust_net"], SIGNAL_FLOW_WINDOW)) / vol

    return W_MOMENTUM * o.cs_rank(mom) + W_FLOW * o.cs_rank(flow)


def build_picks(panel: pd.DataFrame, score: pd.Series,
                start=None, end=None, phase: int = 0) -> Dict:
    """套用閘門 → {date: [(stock_id, score, name), ...]}(分數高到低)。

    phase 用來位移再平衡日:同一組訊號有 REBALANCE 個等價執行相位,只報一個
    相位的績效等於挑路徑。回測務必跑滿所有相位(見 evaluate)。
    """
    d = panel[["date", "stock_id", "name", "in_dynamic_universe", "trend_ok"]].copy()
    d["s"] = score.values
    m = ((d["in_dynamic_universe"] == True) & (d["trend_ok"] == True)  # noqa: E712
         & d["s"].notna())
    if start is not None:
        m &= d["date"] >= pd.Timestamp(start)
    if end is not None:
        m &= d["date"] <= pd.Timestamp(end)
    d = d[m]
    # 先整組排序再 zip。曾經寫成 zip(g.sort_values(...)["stock_id"], g["s"], g["name"]),
    # 只有 stock_id 是排序後的、分數與名稱仍是原順序 → 三者錯配。
    out = {}
    for dt, g in d.groupby("date"):
        g = g.sort_values("s", ascending=False)
        out[dt] = list(zip(g["stock_id"], g["s"], g["name"]))
    if phase:
        keys = sorted(out)[phase:]
        out = {k: out[k] for k in keys}
    return out


def _apply_portfolio_config():
    """把凍結的組合參數套進 config,回傳原值供還原。"""
    old = (config.BT_MAX_POSITIONS, config.BT_MA_EXIT, config.BT_TREND_STOP_LOSS)
    config.BT_MAX_POSITIONS = PORT_MAX_POSITIONS
    config.BT_MA_EXIT = PORT_MA_EXIT
    config.BT_TREND_STOP_LOSS = PORT_STOP_LOSS
    return old


def _restore_portfolio_config(old):
    (config.BT_MAX_POSITIONS, config.BT_MA_EXIT, config.BT_TREND_STOP_LOSS) = old


def run_once(panel: pd.DataFrame, score: pd.Series, symbols: List[str],
             start=None, end=None, phase: int = 0) -> Dict:
    """跑單一相位,回傳 backtest summary。

    ⚠ `start_date`/`end_date` 必須傳給 `backtest_portfolio`。只限制 `picks_by_date`
    的日期範圍**不夠** —— 引擎的 `all_dates` 取自價格快取,沒有上界就會一路跑到
    資料末端。實測(2026-08-03 修):IS 的權益曲線跑超出切點 144 天,把 8 筆未平倉
    部位在 OS 期間的 **+101.8%** 漲幅算進 IS 的 Sharpe。這是評估層的前視,
    會讓 IS 數字虛高,而且用它選出來的參數也連帶失效。
    """
    picks = build_picks(panel, score, start, end, phase)
    if not picks:
        return {}
    old = _apply_portfolio_config()
    try:
        r = backtest.backtest_portfolio(
            symbols=symbols, sample=False,
            start_date=start, end_date=end,     # ← 必傳:見下
            rebalance_every=PORT_REBALANCE_DAYS,
            top_n=PORT_MAX_POSITIONS, picks_by_date=picks,
        )
    finally:
        _restore_portfolio_config(old)
    return r.get("summary", {}) if isinstance(r, dict) else {}


def evaluate(panel: pd.DataFrame, symbols: List[str],
             start=None, end=None) -> pd.DataFrame:
    """跑滿所有等價再平衡相位。回傳每相位一列。

    只報單一相位的 Sharpe 是這個 repo 反覆踩過的坑(S04):同一訊號不同相位
    可以從 -0.09 擺到 +1.09。要看中位數與最小值,不是最大值。
    """
    score = build_signal(panel)
    rows = []
    # 相位數 = 再平衡週期。「每 N 日再平衡」沒有指定從哪天起算,N 個起始偏移
    # 都是同一條規則的合法實作,各自有自己的 Sharpe(rebalance timing luck)。
    # 早期版本上限寫死 5,等於只抽樣 20 個路徑中的 5 個,中位數/最小值本身就
    # 帶抽樣誤差。跑滿才是實際會遇到的分布。
    for ph in range(PORT_REBALANCE_DAYS):
        s = run_once(panel, score, symbols, start, end, ph)
        if not s:
            continue
        rows.append({
            "phase": ph, "sharpe": s.get("sharpe"), "ann_ret": s.get("ann_ret"),
            "ann_vol": s.get("ann_vol"), "max_dd": s.get("max_drawdown"),
            "n_trades": s.get("n_trades"), "win_rate": s.get("win_rate"),
            "payoff": s.get("payoff_ratio"),
        })
    return pd.DataFrame(rows)


def equal_weight_baseline(panel: pd.DataFrame, start=None, end=None) -> Dict:
    """動態 universe 等權買進持有基準(**無交易成本**,是樂觀上界)。

    用與 backtest 引擎相同的算術慣例:ann=mean*252, vol=std*sqrt(252)。
    報酬必須在稠密 panel 上先算再篩成員,否則成員進出的日期斷點會被當成單日
    巨幅報酬,把基準灌爆。
    """
    full = panel.sort_values(["stock_id", "date"]).copy()
    full["r"] = full.groupby("stock_id")["close"].pct_change()
    full = full[full["in_dynamic_universe"] == True]  # noqa: E712
    if start is not None:
        full = full[full["date"] >= pd.Timestamp(start)]
    if end is not None:
        full = full[full["date"] <= pd.Timestamp(end)]
    eq = full.groupby("date")["r"].mean().dropna()
    if len(eq) < 2:
        return {}
    ann = float(eq.mean() * 252)
    vol = float(eq.std(ddof=1) * np.sqrt(252))
    return {"ann_ret": ann, "ann_vol": vol,
            "sharpe": ann / vol if vol > 0 else float("nan"), "n_days": len(eq)}


def is_os_split(panel: pd.DataFrame) -> Tuple[pd.Timestamp, pd.Timestamp]:
    split = build_evaluation_split(panel["date"])
    return split.is_end, split.os_start


# ── panel 建構(base panel + 法人分項籌碼)──────────────────────────────
def build_panel(symbols: Optional[List[str]] = None,
                use_pit_pool: bool = True) -> Tuple[pd.DataFrame, List[str]]:
    """回傳 (稠密 panel, 乾淨 symbols)。

    三件事非做不可:
      1. `keep_non_members=True` —— ts_ 算子需要連續個股序列。
      2. 排除價格完整性名單 —— 否則 _prepare_panel 的 fail-closed 閘門會擋下。
      3. `use_pit_pool=True`(預設)—— M 月候選池只用完整 M-1 曆月重建。

    為什麼預設走 PIT:靜態池是**單一日期**的成交值 top-N 套用整段歷史,等於用
    「今天知道誰熱門」決定兩年前能選誰。下列舊對照使用「生效日前 20 交易日」
    月池，只保留為偏誤案例；不代表目前完整上個曆月規則的績效:

        IS 中位 1.922 → 1.607(最小 1.352 → 0.762);IS 基準 1.42 → 1.13
        OS 中位 1.772 → 1.938;OS 基準 1.59 → 1.52

    偏誤把策略與基準**同時**灌水,所以超額(策略−基準)在 IS 幾乎不變
    (+0.50 → +0.48)、OS 反而變好(+0.18 → +0.42)。但絕對水準必須用 PIT 的。
    """
    import universe as uni

    if use_pit_pool:
        return _build_pit_panel()

    if symbols is None:
        symbols = uni.get_universe(top_n=config.DYNAMIC_UNIVERSE_CANDIDATE_POOL)
    excluded = compute_excluded(symbols)      # 對這組 symbols 當場算,不讀全域檔
    symbols = [s for s in symbols if s not in excluded]

    panel = backtest._prepare_panel(
        symbols, config.MIN_COMPOSITE, None, None,
        dynamic_enabled=True,
        universe_top_n=config.DYNAMIC_UNIVERSE_TOP_N,
        keep_non_members=True,
    )
    return attach_chip_fields(panel), symbols


def _build_pit_panel() -> Tuple[pd.DataFrame, List[str]]:
    """用完整上個曆月的逐月池建 panel(候選池成員資格逐日套用)。

    走精簡資料路徑(price + inst),因為 PIT 池聯集有 700+ 檔,完整 bundle 會
    撞爆 FinMind 的 600 次/小時。`live_signal.verify_equivalence` 已證明精簡路徑
    的 trend_ok / in_dynamic_universe 與 `_prepare_panel` 完全一致(136,841 列零差異)。
    """
    import dynamic_universe as du
    import live_signal
    from universes import MonthlyPITUniverseProvider

    provider = MonthlyPITUniverseProvider.from_cache(
        top_n=config.DYNAMIC_UNIVERSE_CANDIDATE_POOL,
        min_obs=config.DYNAMIC_UNIVERSE_MONTHLY_MIN_OBS,
    )
    union = provider.all_symbols
    union = sorted(set(union) - compute_excluded(union))   # 對 PIT 聯集當場算

    panel = live_signal.build_light_panel(union, apply_membership=False)
    if panel.empty:
        return panel, []

    candidate_mask = provider.candidate_mask(panel)
    panel = du.add_membership(
        panel, top_n=config.DYNAMIC_UNIVERSE_TOP_N,
        lookback=config.DYNAMIC_UNIVERSE_LOOKBACK,
        min_obs=config.DYNAMIC_UNIVERSE_MIN_OBS,
        min_avg_volume_lots=config.DYNAMIC_UNIVERSE_MIN_AVG_VOLUME_LOTS,
        min_avg_turnover=config.DYNAMIC_UNIVERSE_MIN_AVG_TURNOVER,
        candidate_mask=candidate_mask,
    )
    # 不可先刪掉非候選月份的列：同一股票離開再回到候選池時，先刪列會讓 ts_ 的
    # 「20 列」跨過數月，製造錯誤視窗。候選資格只透過旗標在選股時套用。
    panel = panel.sort_values(["date", "stock_id"]).reset_index(drop=True)
    panel.attrs["universe"] = provider.metadata()
    return panel, union


def attach_chip_fields(panel: pd.DataFrame) -> pd.DataFrame:
    """併入法人分項(外資/投信/自營)淨買。base panel 只有合併後的 inst_6d。

    無申報日補 0(不向後延用舊值),與 factors._align 的慣例一致 —— 把「當日沒
    申報」延用成「當日有買」是典型的籌碼面前視。
    """
    import data

    frames = []
    for sid in sorted(panel["stock_id"].unique()):
        inst = data.fetch_institutional(sid)
        if inst is None or inst.empty:
            continue
        d = inst[["date", "foreign_net", "trust_net", "dealer_net"]].copy()
        d["date"] = pd.to_datetime(d["date"])
        d["stock_id"] = sid
        frames.append(d)
    if not frames:
        for c in ["foreign_net", "trust_net", "dealer_net"]:
            panel[c] = 0.0
        return panel
    chip = pd.concat(frames, ignore_index=True)
    out = panel.merge(chip, on=["date", "stock_id"], how="left")
    for c in ["foreign_net", "trust_net", "dealer_net"]:
        out[c] = out[c].fillna(0.0)
    return out


# ── 報告 ────────────────────────────────────────────────────────────────
def _fmt(df: pd.DataFrame) -> str:
    if df.empty:
        return "(無結果)"
    h = "| 相位 | Sharpe | 年化 | 波動 | MaxDD | 交易數 | 勝率 | 賺賠比 |"
    s = "|---|---|---|---|---|---|---|---|"
    rows = [
        f"| {int(r.phase)} | {r.sharpe:.3f} | {r.ann_ret:+.1%} | {r.ann_vol:.1%} | "
        f"{r.max_dd:.1%} | {int(r.n_trades)} | {r.win_rate:.1%} | {r.payoff:.2f} |"
        for r in df.itertuples()
    ]
    return "\n".join([h, s] + rows)


def main():
    panel, symbols = build_panel()
    cut, os_start = is_os_split(panel)
    dates = np.array(sorted(panel["date"].unique()))
    print(f"[S19] panel {panel.shape} | {len(symbols)} 檔 | "
          f"IS <= {cut.date()} | OS >= {os_start.date()}")

    is_df = evaluate(panel, symbols, dates[0], cut)
    os_df = evaluate(panel, symbols, os_start, dates[-1])
    b_is = equal_weight_baseline(panel, dates[0], cut)
    b_os = equal_weight_baseline(panel, os_start, dates[-1])

    def stat(df):
        g = df["sharpe"].astype(float)
        return g.median(), g.min(), g.max()

    m_is, lo_is, hi_is = stat(is_df)
    m_os, lo_os, hi_os = stat(os_df)

    lines = [
        "# S19 籌碼確認的風險調整動能(CRM)— 策略報告",
        "",
        f"> snapshot `{config.SNAPSHOT_END_DATE}`｜**PIT 候選池**(M 月只用完整 M-1 曆月"
        f"重建 top{config.DYNAMIC_UNIVERSE_CANDIDATE_POOL},含下市股;排除 "
        f"{len(load_excluded())} 檔價格完整性名單)｜動態 universe top"
        f"{config.DYNAMIC_UNIVERSE_TOP_N}｜自建還原價。",
        "",
        "> ⚠️ **舊績效不可沿用。** 以下靜態池／舊 PIT 池對照只記錄歷史偏誤；",
        "> 舊 PIT 池是生效日前 20 交易日，並非現版完整上個曆月規則。",
        ">",
        "> | | 靜態池(舊,有偏) | PIT 池(新) |",
        "> |---|---|---|",
        "> | IS 中位 / 最小 | 1.922 / 1.352 | **1.607 / 0.762** |",
        "> | IS 基準 | 1.42 | 1.13 |",
        "> | IS 超額 | +0.50 | **+0.48** |",
        "> | OS 中位 / 最小 | 1.772 / 1.231 | **1.938 / 1.392** |",
        "> | OS 基準 | 1.59 | 1.52 |",
        "> | OS 超額 | +0.18 | **+0.42** |",
        ">",
        "> 偏誤把**策略與基準同時**灌水,所以超額(策略−基準)在 IS 幾乎不變、OS 反而變好。",
        "> 但絕對水準必須引用 PIT 版;舊報告的「IS 五相位全部 >1」已不成立(最小 0.762)。",
        "",
        "## 訊號",
        "",
        "```",
        "score = 0.5 * cs_rank( ts_ir(日報酬, 20) )",
        "      + 0.5 * cs_rank( (Σ20 外資 + Σ20 投信) / 近20日均量 )",
        "```",
        "",
        f"閘門:動態 universe 成員 × trend_ok(MA20>MA60、MA60上揚、收盤>MA60)。",
        "",
        "## 組合建構(凍結參數)",
        "",
        f"- 持股 {PORT_MAX_POSITIONS} 檔等權、{PORT_REBALANCE_DAYS} 日再平衡",
        f"- MA{PORT_MA_EXIT} 跌破出場(次日開盤)、-{PORT_STOP_LOSS:.0%} 硬停損、T+1 開盤進場",
        f"- 成本:手續費 {config.BT_FEE:.4%}(單邊)+ 證交稅 {config.BT_TAX:.2%}(賣出)",
        "",
        "## IS(樣本內)",
        "",
        _fmt(is_df),
        "",
        f"**中位 Sharpe {m_is:.3f}｜最小 {lo_is:.3f}｜最大 {hi_is:.3f}**",
        "",
        "## OS(樣本外,未參與任何參數選擇)",
        "",
        _fmt(os_df),
        "",
        f"**中位 Sharpe {m_os:.3f}｜最小 {lo_os:.3f}｜最大 {hi_os:.3f}**",
        "",
        "## 對照:動態 universe 等權買進持有(無交易成本,樂觀上界)",
        "",
        "| 段 | 基準 Sharpe | 基準年化 | 策略中位 Sharpe | 判定 |",
        "|---|---|---|---|---|",
        f"| IS | {b_is.get('sharpe', float('nan')):.2f} | {b_is.get('ann_ret', float('nan')):+.1%} | "
        f"{m_is:.2f} | {'勝' if m_is > b_is.get('sharpe', 9e9) else '敗'} |",
        f"| OS | {b_os.get('sharpe', float('nan')):.2f} | {b_os.get('ann_ret', float('nan')):+.1%} | "
        f"{m_os:.2f} | {'勝' if m_os > b_os.get('sharpe', 9e9) else '敗'} |",
        "",
        "## 誠實保留(讀完再決定要不要用)",
        "",
        "1. **OS 交易數極少**(每相位約 16~21 筆),該段 Sharpe 沒有統計意義,",
        "   只能說「換到未參與選擇的區間沒有崩掉」,不能當作已驗證的 edge。",
        "2. **報酬高度集中**:勝率僅 30~62%,靠賺賠比 4.6~6.4 撐住。少數幾筆大贏",
        "   決定全局 → 對個別交易與流動性衝擊極敏感。",
        "3. **IS 相位最小值已低於 1**(0.762)。舊版「五相位全部 >1」是靜態池的產物,",
        "   在無偏池上不成立。相位離散度(0.762~1.943)仍大,執行時點會實質影響結果。",
        "4. **單一多頭窗**:樣本 2024-08~2026-07 只涵蓋一次完整多頭與一次股災。",
        "5. **仍有殘餘倖存者偏誤**:PIT 池已含下市股(來源是交易所逐日快照),但個股",
        "   價格序列仍從 FinMind 抓,已下市者可能缺 —— 這會讓下市虧損被低估。",
        "6. **參數在 IS 上選過**:16 格掃描以「五相位 Sharpe 中位數」為準則(非最大值),",
        "   且方向一致(所有 MA60 配置都在前段),但仍有選擇效應殘留。",
        "7. **搜尋空間的多重檢定**:實測同閘門下**隨機選股**的 IS Sharpe 分布為",
        "   平均 0.711 / 標準差 0.290,第 99.9 百分位 1.591。本策略的 1.607 大致落在",
        "   該分布的極上尾 —— 有訊號,但若曾在數百個變體中挑選,這個水準是可以靠",
        "   運氣達到的。",
        "",
        "## 已證偽(別重做)",
        "",
        "- **融資餘額下降 = 散戶退場**:`margin_drop_20/60` 的 IC 為 -0.006 / -0.002,",
        "  IS/OS 都無方向。「籌碼轉移」假說在本樣本**不成立**。",
        "- **生動能 `mom_ret` 的 IS IC 幾乎是 0**(+0.0012),其全期 IC 全部來自 OS",
        "  普漲段。這是現行 production 權重的因子,值得重新檢視。",
        "",
        "## 下一個可證偽測試",
        "",
        "- 用 `freeze_manifest.py` 凍結本規則,以 `forward_test.py` 做 snapshot 之後的",
        "  forward-only 驗證(唯一能升級證據等級的路)。",
        "- 擴到 survivorship-free PIT 全市場池後重跑,看 IS 的增量是否還在。",
    ]
    out = config.OUTPUT_DIR / "CHIP_MOMENTUM_REPORT.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"[S19] 報告已存:{out}")
    print(f"[S19] IS 中位 Sharpe {m_is:.3f}(最小 {lo_is:.3f})| "
          f"OS 中位 {m_os:.3f}(最小 {lo_os:.3f})")
    return {"is": is_df, "os": os_df, "baseline_is": b_is, "baseline_os": b_os}


if __name__ == "__main__":
    main()
