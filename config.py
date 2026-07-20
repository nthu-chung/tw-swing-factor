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
# (3) momentum_only（目前上線，2026-06-22）：IS Sharpe 1.50 / 年化 +40.5% /
#     MaxDD -19.4% / 80 筆。在所有候選裡 IS 第二高（與 mom80_instmid20 的 1.54
#     差距在誤差內），但因子數最少、最不依賴噪音邊際因子，依「複雜度↔穩定性
#     反向」鐵則選定。詳見 outputs/WEIGHT_FIX_REPORT.md（候選對比 + 決策推導）。
#
# ⚠️ 已知保留：
#  - IS 也只有 16 個月、80 筆，純動能的 1.50 仍可能含運氣。要等更長資料才確定。
#  - 動能策略在反轉期會集體失靈，需搭配市場濾網（VIX / 大盤 MA200）當總開關。
#  - 回測視窗 = config.SNAPSHOT_END_DATE 鎖住的那天，避免邊界漂移。
FACTOR_WEIGHTS = {
    "momentum": 1.0,  # ★真 alpha：中性化IC +0.098/t2.45、分層單調+0.90、兩半皆正
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

# fixed 模式參數（僅 BT_EXIT_MODE="fixed" 時生效）
BT_HOLD_DAYS = 20        # 固定持有天數
BT_TAKE_PROFIT = 0.25    # 停利 +25%（拉高，不要 10% 就跑）
BT_STOP_LOSS = 0.08      # 停損 -8%

# 組合 / 執行
BT_MAX_POSITIONS = 5     # 同時最多持有檔數（等權重）
BT_ENTRY_NEXT_OPEN = True  # 隔日開盤進場（避免用當日收盤訊號當日成交的未來函數）
BT_FEE = 0.001425        # 手續費（單邊）
BT_TAX = 0.003           # 證交稅（賣出）

# ── 因子 IC / 驗證 ──────────────────────────────────────────────────────
BT_IC_HORIZON = 20       # IC 用的未來報酬視窗（交易日，約一個月＝波段尺度）
# IS/OS 切分（階段三嚴格驗證用；先放著，backtest 已預留接口）
IS_OS_SPLIT = 0.70       # 前 70% 時間 in-sample，後 30% out-of-sample
EMBARGO_DAYS = 20        # IS/OS 之間的緩衝（= IC_HORIZON，避免重疊洩漏）
