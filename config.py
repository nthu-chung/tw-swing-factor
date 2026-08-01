# -*- coding: utf-8 -*-
"""
全域設定：因子權重、選股門檻、回測參數、資料來源。

設計原則
--------
- 所有「可調的數字」集中在這裡，方便日後做參數掃描 / 上嚴格驗證。
- 因子權重用 dict 表示，加總不必為 1（評分時會自動正規化）。
- FinMind token 優先讀環境變數 FINMIND_TOKEN；
  若無，fallback 去讀同工作區 taiwan-industry-analyzer/backend/.env（複用既有 token）。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# ── 路徑 ────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent
CACHE_DIR = ROOT / "_cache"          # 原始資料快取（pickle）
OUTPUT_DIR = ROOT / "outputs"        # 選股清單 / 回測結果
CACHE_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)


# ── FinMind Token ──────────────────────────────────────────────────────
def _load_finmind_token() -> str:
    """優先環境變數，否則複用 taiwan-industry-analyzer 的 .env。"""
    tok = os.getenv("FINMIND_TOKEN", "").strip()
    if tok:
        return tok
    # fallback：同層工作區的既有專案
    candidate = ROOT.parent / "taiwan-industry-analyzer" / "backend" / ".env"
    if candidate.exists():
        txt = candidate.read_text(encoding="utf-8", errors="ignore")
        m = re.search(r"(?im)^\s*FINMIND_TOKEN\s*=\s*(.+)\s*$", txt)
        if m:
            return m.group(1).strip().strip('"').strip("'")
    return ""


FINMIND_TOKEN = _load_finmind_token()
FINMIND_BASE = "https://api.finmindtrade.com/api/v4/data"

# FinMind 免費版有流量限制，抓取之間 sleep（秒）
FINMIND_SLEEP = 0.35


# ── 資料抓取範圍 ────────────────────────────────────────────────────────
# 抓多久的歷史供回測（含暖身期，因子需要 MA60 等）
HISTORY_DAYS = 730  # 約 2 年

# 大盤指數(TAIEX)抓更長：市場濾網要算 MA200，需在回測起點(≈snapshot-730日)之前
# 就有 200 個交易日暖身，否則 IS 前段 MA200 是 NaN、濾網等於沒作用。TAIEX 只有一
# 條序列，多抓幾年成本極低。往前多抓 ~1.5 年當 MA200 暖身。
# 註：只往前延伸歷史、snapshot 截止日不變，既有日期的 TAIEX 值不變 → 不影響 PR#1
# 的 RS/抗跌因子（那些用 merge_asof 對齊個股日、只取重疊區間）。
MARKET_HISTORY_DAYS = 730 + 550  # ≈ 3.5 年

# ── 資料快照（防漂移）──────────────────────────────────────────────────
# 2026-06-22 加：原本資料抓取用 datetime.now() 算結束日，每天都會往後滑動，
# 加上 12h 快取過期會回 FinMind 重抓 → IS/OS 切點和籌碼資料每天微微不同。
# 在 IS 16 個月、~80 筆交易的小樣本上，這種邊界漂移會讓 Sharpe 改變到讓
# 不同權重排名翻轉（實證：2026-06-20 mom_quality IS Sharpe=0.41，
# 2026-06-22 同一程式碼變 1.33，純粹來自資料邊界 + FinMind 籌碼補修）。
#
# 解法：鎖一個資料快照日，所有回測都以這天為資料截止。要更新快照才主動推進。
# 環境變數 SWING_SNAPSHOT_END 可覆寫（給 ad-hoc 實驗用）。
# 設成空字串 "" 則退回 datetime.now()（debug / 探索用，正式回測請鎖日）。
SNAPSHOT_END_DATE = os.getenv("SWING_SNAPSHOT_END", "2026-06-22").strip()

# 快取策略：當 SNAPSHOT_END_DATE 鎖住時，快取永久有效（以 mtime > snapshot 視為新）
# 否則維持 12h 過期重抓。
CACHE_TTL_HOURS_DEFAULT = 12


# ── Universe（選股池）─────────────────────────────────────────────────
# 快速原型用的小集合（涵蓋不同產業 / 大中小型），跑通後再換全市場。
SAMPLE_UNIVERSE = [
    "2330",  # 台積電 半導體
    "2317",  # 鴻海 電子代工
    "2454",  # 聯發科 IC設計
    "2308",  # 台達電 電源
    "2382",  # 廣達 伺服器
    "3231",  # 緯創 伺服器
    "2412",  # 中華電 電信（低beta對照）
    "2603",  # 長榮 航運
    "1301",  # 台塑 傳產
    "2002",  # 中鋼 鋼鐵
    "3008",  # 大立光 光學
    "3017",  # 奇鋐 散熱
    "6505",  # 台塑化
    "2891",  # 中信金 金融（會被pre-filter濾掉，測試用）
    "2603",  # （重複示意，load 時會去重）
]

# pre-filter：排除條件
EXCLUDE_FINANCE = True          # 排除金融保險（產業別含「金融」）
EXCLUDE_ETF_PREFIX0 = True      # 排除 00 開頭 ETF
MIN_AVG_VOLUME_LOTS = 500       # 近20日均量門檻（張），低於視為流動性不足

# ── 動態 universe（long-only；只決定「當日可選哪些股票」）──────────────
# 重要：動態排名能消除「拿期末 top100 回套整段歷史」的直接 look-ahead，
# 但若候選池本身仍是期末 top300，仍殘留 candidate-pool survivorship bias。
# 論文級驗證應把候選池換成「歷史上所有曾上市櫃股票（含下市）」。
DYNAMIC_UNIVERSE_ENABLED = True
DYNAMIC_UNIVERSE_TOP_N = 100          # 每個訊號日成交值排名前 N 才可被選
DYNAMIC_UNIVERSE_CANDIDATE_POOL = 300 # 現階段用既有 top300 當 bootstrap 候選池
DYNAMIC_UNIVERSE_LOOKBACK = 20        # 用截至訊號日的近 N 個交易日平均成交值/量
DYNAMIC_UNIVERSE_MIN_OBS = 20         # 暖身不足不納入
DYNAMIC_UNIVERSE_MIN_AVG_VOLUME_LOTS = MIN_AVG_VOLUME_LOTS
DYNAMIC_UNIVERSE_MIN_AVG_TURNOVER = 0.0  # 新台幣；0 表示只靠 top-N + 成交量

# 候選池 PIT 閘門(2026-07-24 加):候選池建構日(as_of)晚於資料快照 = 未來池
# look-ahead(用未來成交值排名/存活性回套過去)。universe.get_universe 會 fail-closed
# 擋下。SWING_ALLOW_FUTURE_POOL=1 顯式放行(研究/debug,結果不可當已驗證)。
ALLOW_FUTURE_POOL = os.getenv("SWING_ALLOW_FUTURE_POOL", "").strip() == "1"

# 價格來源。FinMind 的 TaiwanStockPrice 是未還原價；論文級研究建議改用
# TaiwanStockPriceAdj（backer/sponsor）並用 SWING_PRICE_DATASET 覆寫。
PRICE_DATASET = os.getenv("SWING_PRICE_DATASET", "TaiwanStockPrice").strip()

# ── 未還原價 fail-closed 閘門（2026-07-24 加）──────────────────────────────
# 未還原價會被公司行動（除權息/分割/減資）污染 → 假停損/假 MA 出場、選股排名被
# 機械性壓低。backtest._prepare_panel 會在未還原價且偵測到斷點時直接 raise，拒絕
# 產出假績效。要跑污染 smoke test 才顯式打開逃生門（結果 summary 會戳
# integrity_bypassed=True，不可當已驗證數字）。
ALLOW_UNADJUSTED_BACKTEST = os.getenv("SWING_ALLOW_UNADJUSTED", "").strip() == "1"
# 斷點偵測門檻：台股單日漲跌幅 ±10%，任何隔夜/收盤跳空 > 11% 幾乎必為公司行動或
# 壞列，故用 0.11（比 price_integrity 預設 0.20 嚴），才攔得到 3~10% 的除權息缺口
# 以外、10~20% 的分割/減資（0.20 會漏接約 76/82 筆真斷點）。
PRICE_INTEGRITY_RETURN_THRESHOLD = float(
    os.getenv("SWING_PRICE_INTEGRITY_THRESHOLD", "0.11").strip() or "0.11"
)


# ── 因子參數 ────────────────────────────────────────────────────────────
# 技術面
MA_SHORT = 20
MA_LONG = 60
BBANDS_WIN = 20
BBANDS_K = 2.0
BIAS_SHORT_MAX = 0.024   # 均線糾結：短期 BIAS 門檻
BIAS_MID_MAX = 0.030
HIGH_LOOKBACK = 60       # N 日新高判定（波段尺度，配合動能因子）
VOL_DRYUP_RATIO = 0.5    # 窒息量：近5日均量 / 前5日均量 <= 此值

# 動能因子（找「下一波成長股」的核心：強勢續強）
MOM_LOOKBACK = 60        # 動能回看天數（約一季）
MOM_RET_FULL = 0.30      # 60日報酬達 +30% 給滿分（對齊 Qullamaggie 門檻）
MOM_NEAR_HIGH_FULL = 0.90  # 收盤 / 60日高點 >= 0.90 視為貼近高點（強勢）

# 相對強勢 / 抗跌因子（弱市防禦研究，2026-07-20 加；基準 = TAIEX 加權指數）
# 目的：在大盤走弱時找「抗跌 + 逆勢相對強勢」的股，看能否對純動能帶來增量 edge。
# 分數映射一律用「對稱」區間（以中性=0.5 為中心），因為 top100 的橫斷面分布
# 顯示：相對報酬 rs 中位≈0、下跌日相對報酬中位≈0、下行 beta 中位≈1.2（top100
# 多為高 beta 成長股）。若用單邊 [0,+X] 映射會把整個「低於中位」的半邊壓成 0、
# 失去橫斷面鑑別力（IC/分層會失真）。對稱映射只夾極端尾端，保留中段排序。
RS_LOOKBACK = 60             # 相對強勢回看天數（對齊 MOM_LOOKBACK 便於與動能比較）
RS_EXCESS_FULL = 0.20        # 60日相對大盤超額報酬 ±20% 對映 0~1（0=打平大盤→0.5）
DOWNSIDE_WINDOW = 60         # 下行 beta / 抗跌度回看視窗（交易日）
DOWNSIDE_MIN_DOWN_DAYS = 8   # 視窗內至少 N 個大盤下跌日才算有效（否則 NaN）
DOWNSIDE_BETA_DEFENSIVE = 0.4   # 下行 beta <= 此值視為完全抗跌（滿分 1.0）
DOWNSIDE_BETA_AGGRESSIVE = 1.8  # 下行 beta >= 此值視為完全跟跌（0 分；跨越中位 1.2）
DOWNDAY_RS_FULL = 0.006      # 大盤下跌日平均相對報酬 ±0.6%/日 對映 0~1（0→0.5）

# 籌碼面（法人正規化用「近 N 日均量(股)」當分母，跨股票可比、資料一定有）
INST_NORM_WINDOW = 20    # 正規化分母：近20日均量
INST_WIN_SHORT = 1       # 法人淨買累積窗（日）
INST_WIN_MID = 6
INST_WIN_LONG = 12
INST_RATIO_PASS = 0.0    # 「主力未撤」門檻：中長窗淨買佔量比需 > 此值
MARGIN_OPTIMAL_LOW = 2.0   # 資券比最佳區間
MARGIN_OPTIMAL_HIGH = 8.0


# ── 趨勢保護硬門檻（任一不過直接淘汰）────────────────────────────────────
TREND_GUARD_ENABLED = True   # MA20>MA60 且 MA60上揚 且 收盤>MA60


# ── 多因子權重 ──────────────────────────────────────────────────────────
# 每個因子輸出 0~1 標準化分數，乘以權重後加總、再正規化成 0~100。
# composite_score 會自動以實際 key 的權重和正規化。
#
# 演化史（嚴格依鐵則：永遠分 IS/OS 看，不要只看全期 → 否則被普漲 OS 騙）：
#
# (1) 原始 9 因子（FACTOR_WEIGHTS_LEGACY_9）：35% 權重押在「買弱」群
#     （ma_squeeze 反向 / vol_dryup / bb_pullback / inst_long 翻號），
#     IS Sharpe 0.54、IS 年化 +15.1%（資料快照 2026-06-22, top100 trend 退場）。
# (2) mom_quality 3 因子（LEGACY_MOMQ）：用全期 IC 挑出 momentum + ma_alignment
#     + margin_health。全期 Sharpe 1.53 看起來漂亮，但 IS 段只有 0.41~1.33
#     （資料漂移範圍很大，第二天再跑就翻 3 倍），OS +267% 純粹是普漲 beta
#     （top100 等權買進持有 +170%、98% 個股漲）。被全期數字騙了一輪。
# (3) momentum_only（2026-06-22 static-universe 基線）：IS Sharpe 1.50 / 年化 +40.5% /
#     MaxDD -19.4% / 80 筆。在所有候選裡 IS 第二高（與 mom80_instmid20 的 1.54
#     差距在誤差內），但因子數最少、最不依賴噪音邊際因子，依「複雜度↔穩定性
#     反向」鐵則選定。詳見 outputs/WEIGHT_FIX_REPORT.md（候選對比 + 決策推導）。
#
# (4) 2026-07-23 動態 universe 修正：單一五日再平衡相位為 -4.4%，但其餘
#     四個等價相位皆為正；動態 top-5 對同日 universe 的20日超額約 +2.84pp。
#     所以 -4.4% 只能證明執行相位不穩，不能否定動能。momentum-only 仍只作
#     最簡單 baseline；較接近實際操作的族群/法人/突破研究見 rotation_research.py。
#
# ⚠️ 已知保留：
#  - IS 也只有 16 個月、80 筆，純動能的 1.50 仍可能含運氣。要等更長資料才確定。
#  - 動能策略在反轉期會集體失靈，需搭配市場濾網（VIX / 大盤 MA200）當總開關。
#  - 回測視窗 = config.SNAPSHOT_END_DATE 鎖住的那天，避免邊界漂移。
FACTOR_WEIGHTS = {
    "momentum": 1.0,  # 研究 baseline；不可把單一IC或單一再平衡相位當最終結論
}

# 上一版上線權重（mom_quality）。被證明：(a) 全期 +1.53 純粹被 OS 普漲拉高，
# (b) IS 段 1.33 < momentum_only 1.50（資料快照 2026-06-22）。保留備查。
FACTOR_WEIGHTS_LEGACY_MOMQ = {
    "momentum": 0.50, "ma_alignment": 0.20, "margin_health": 0.30,
}

# 體檢前的原始 9 因子權重（保留備查，勿刪——切回可比較）
FACTOR_WEIGHTS_LEGACY_9 = {
    "momentum": 0.20, "inst_mid": 0.15, "inst_long": 0.15, "inst_dip_buy": 0.05,
    "margin_health": 0.05, "ma_alignment": 0.10, "bb_pullback": 0.10,
    "ma_squeeze": 0.10, "vol_dryup": 0.05,
}


# ── 選股輸出 ────────────────────────────────────────────────────────────
TOP_N = 20               # 每日選股輸出前幾名
MIN_COMPOSITE = 50.0     # 綜合分數門檻（0~100）


# ── 回測參數（波段：抱數週～數月，讓獲利奔跑）────────────────────────────
# 退場模式：
#   "trend"  = 趨勢出場（推薦，真波段）：跌破 MA_EXIT 或硬停損才出，不設固定停利，
#              讓贏家一路抱到趨勢轉折，符合「找下一波成長股、不每天湯沖」的目標。
#   "fixed"  = 固定持有 N 天 + 停利/停損（短波段，比較基準用）。
BT_EXIT_MODE = "trend"

# trend 模式參數
BT_MA_EXIT = 20          # 收盤跌破此均線（MA20）即出場（搭配 MA60 為更慢的版本）
BT_TREND_STOP_LOSS = 0.08  # 硬停損 -8%（趨勢沒走出來時的保命線）
BT_MAX_HOLD_DAYS = 120   # 最長持有（約半年上限，避免殭屍部位）
# 缺 bar(下市/長停牌)超過此交易日數 → 視為下市,以最後已知收盤強制平倉。
# 沒有這道,缺 bar 部位會逃過所有出場判定、永遠凍結佔槽,survivorship-free 重跑時
# 會忽略下市虧損(偏樂觀)。10 日 ≈ 兩週無交易。
BT_STALE_EXIT_DAYS = 10

# fixed 模式參數（僅 BT_EXIT_MODE="fixed" 時生效）
BT_HOLD_DAYS = 20        # 固定持有天數
BT_TAKE_PROFIT = 0.25    # 停利 +25%（拉高，不要 10% 就跑）
BT_STOP_LOSS = 0.08      # 停損 -8%

# 組合 / 執行
BT_MAX_POSITIONS = 5     # 同時最多持有檔數（等權重）
BT_ENTRY_NEXT_OPEN = True  # 隔日開盤進場（避免用當日收盤訊號當日成交的未來函數）
BT_FEE = 0.001425        # 手續費（單邊）
BT_TAX = 0.003           # 證交稅（賣出）

# ── 漲跌停可成交性(2026-08-01 加)────────────────────────────────────────
# 台股漲跌幅 ±10%(2015/6/1 起)。一字鎖漲停(open==high==low 且跳空≈+10%)時委買
# 遠大於委賣、實務上『買不到』;一字鎖跌停時『賣不掉』。回測若無條件用開盤成交會
# 系統性高估(追動能最想買的封板股恰恰最難成交)。開此模型:進場遇一字漲停→跳過
# (買不到);出場遇一字跌停→順延到能成交日(賣不掉、被迫續抱)。不需額外資料,
# 直接由 OHLC 推(high==low 無盤中區間=鎖死)。
BT_MODEL_LIMIT_LOCK = os.getenv("SWING_MODEL_LIMIT_LOCK", "1").strip() != "0"
BT_LIMIT_PCT = 0.095     # 判定鎖漲跌停的跳空門檻(略低於 10% 容 tick 圓整)

# ── 市場濾網 / 擇時 overlay（下檔保護；方向A：不做空、不做 regime 切換模型）──
# 2026-07-21 加。在 momentum_only 多頭策略上疊加「大盤走弱→降曝險」的總開關。
# 設計原則（守鐵則、避免 n=1 過擬合）：用教科書級、少參數、不需 grid-search 的
# 強先驗規則，訊號建在大盤 TAIEX。**預設關閉**，開了也不動 FACTOR_WEIGHTS。
#
# 誠實限制：資料只有 1 次熊市（2025 關稅股災），任何濾網都不能宣稱「已驗證」，
# 只能說「這個強先驗規則在我們僅有的一次股災上幫多少、牛市段代價多少」。
MARKET_FILTER_ENABLED = False       # 預設關（不影響現有回測/上線）
# 規則（少參數、教科書級）：
#   "ma200" 收盤<MA200 → risk-off（最慢、最少假訊號，經典長線多空分界）
#   "ma60"  收盤<MA60  → risk-off（中速）
#   "ma20"  收盤<MA20  → risk-off（快、假訊號多，當「太敏感」對照組）
#   "vol"   近20日年化實現波動 > 門檻 → risk-off（波動飆高）
MARKET_FILTER_RULE = "ma200"
MARKET_FILTER_RISKOFF_WEIGHT = 0.0  # risk-off 時目標曝險比例（0=全空手, 0.5=減半）
MARKET_FILTER_MA = {"ma200": 200, "ma60": 60, "ma20": 20}  # 規則→均線天數
MARKET_FILTER_VOL_WINDOW = 20       # vol 規則：實現波動視窗
MARKET_FILTER_VOL_THRESHOLD = 0.30  # vol 規則唯一參數：年化波動門檻（圓整值，未最佳化）


# ── 因子 IC / 驗證 ──────────────────────────────────────────────────────
BT_IC_HORIZON = 20       # IC 用的未來報酬視窗（交易日，約一個月＝波段尺度）
# IS/OS 切分（階段三嚴格驗證用；先放著，backtest 已預留接口）
IS_OS_SPLIT = 0.70       # 前 70% 時間 in-sample，後 30% out-of-sample
EMBARGO_DAYS = 20        # IS/OS 之間的緩衝（= IC_HORIZON，避免重疊洩漏）
