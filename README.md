# tw-swing-factor — 台股波段多因子選股系統

用**籌碼面 + 技術面 + 大戶進出**多因子組合，挑選台股波段標的（持有數天～數週，白天可操作、不熬夜），並用**嚴格回測誠實驗證每個因子到底有沒有 edge**。

> 這套系統的定位不是「再做一個會喊強力買進的選股器」，而是一個**能證偽的研究框架**——先用快速原型跑通流程，看哪些因子真的有預測力，再決定要不要相信它。

> ⛔ **目前沒有任何策略達到 clean OOS。** 這個 repo 公開的是**研究框架與失敗紀錄**，
> 不是一組可用的績效。
>
> 起因是 2026-07-23 的稽核：未還原價格已實際污染回測（國巨 2327 在 2025-08-25
> 的公司行動斷點被當成約 -73.6% 的 hard-stop，並可能改變「最佳」退場規則的選擇）。
> 之後又發現評估窗溢出讓 IS 借用了 OS 的績效。**兩者都會直接改變結論，不只是誤差。**
>
> 這兩個缺陷現在都已由程式 fail-closed（見
> [價格完整性](#價格完整性fail-closed不是警告) 與
> [IS/embargo/OS](#isembargoos-切割統一入口)），候選池也改成
> [每月 PIT](#兩層-pit-universe正式回測路徑)。但**修好閘門不等於重新證明策略**——
> 舊報告的絕對績效一律標為 historical/invalid，必須在合格資料上重跑才會有新結論。
> 逐策略狀態見 [STRATEGY_REGISTRY.md](./STRATEGY_REGISTRY.md)，
> 稽核範圍見 [PUBLIC_REPO_AUDIT.md](./PUBLIC_REPO_AUDIT.md)。

---

## 為什麼做這個

市面上的台股選股開源專案（已研究 5 個，見最下方）共同弱點是：**選股邏輯花俏，但回測太陽春**（常常只用固定持有 N 天算個勝率就說「有效」）。本專案反過來，把重心放在**驗證**：

- 整體回測：勝率 / 平均報酬 / 累積報酬 / 最大回撤 / 類 Sharpe
- 逐因子 IC（資訊係數）：每個因子對未來報酬的 Spearman 相關，看誰真的有用
- 嚴格防未來函數（point-in-time 對齊、T+1 進場）

---

## 架構

完整的模組責任、人工投資流程與分階段搬遷方式見
[ARCHITECTURE.md](./ARCHITECTURE.md)。目前已開始採套件化架構，根目錄舊檔名保留為
相容入口，避免搬遷同時改變歷史研究語意。

正式回測的資料流：

```text
交易所逐日快照 → 每月 PIT 候選池(只用完整 M-1 月)
                   → 每日 dynamic universe(截至當日 ADV20 取 top-N)
                   → 稠密 panel 上算 data fields + operators
                   → strategy 訊號與硬閘門
                   → picks_by_date
                   → 事件驅動回測引擎(T+1 開盤、漲跌停、處置禁倉)
                   → IS / embargo / OS × 所有等價再平衡相位
```

兩個容易看漏的細節：**因子在稠密 panel 上算**（保留非成員列，否則 `ts_` 算子的
「20 列」會橫跨 60+ 個日曆日），成員過濾留到選股那一步；**回測是事件驅動**，不是
`weights × returns` 向量化，因為要表達路徑相依的成交真實性。

```
tw-swing-factor/
├── factor_engine/
│   ├── data_fields.py   # 無視窗衍生欄位(vwap/returns/true_range…)＝data
│   ├── operators.py     # 有視窗的因果算子(ts_/cs_/group_)＝operator
│   └── legacy_factors.py# 既有傳統因子與 0~1 分數
├── universes/monthly_pit.py  # 每月 PIT 候選池 provider(只用完整上一曆月)
├── strategies/          # 可重複執行的策略單元；S19 在此且仍為 blocked
├── execution/           # 回測成交限制；不是券商下單或自動交易
│   ├── taiwan_rules.py  # 升降單位、10% 漲跌停、新上市例外
│   ├── tradability.py   # 一字鎖停、處置禁倉
│   └── costs.py         # 整張／零股代理／研究小數股與券商成本
├── evaluation/splits.py # IS／embargo／OS 切割與驗證邊界
├── config.py            # 可調參數與凍結資料邊界
├── data.py              # 現行 FinMind 資料與快取入口（待第二階段搬遷）
├── price_adjust.py      # 自建還原價（除權息回溯）
├── price_integrity.py   # 斷點稽核（診斷用，不是放行條件）
├── pit_universe.py      # 交易所逐日快照抓取／解析與 PIT 池建構
├── dynamic_universe.py  # 每日成員判定（截至訊號日的 ADV20 排名）
├── universe.py          # legacy static universe 入口（僅供對照）
├── backtest.py          # 現行唯一事件驅動回測入口 + 因子 IC
├── screener.py          # 選股引擎與人工候選清單
├── preflight.py         # 公開前的離線密鑰／產物／文件檢查
├── operators.py / factors.py / evaluation_split.py / chip_momentum_strategy.py
│                        # 舊匯入相容層（薄轉發，無邏輯）
├── STRATEGY_REGISTRY.md # 所有既有策略、狀態、偏誤與下一個證偽測試
├── main.py              # 統一入口（命令列）
├── _cache/              # 原始資料快取（自動產生，不進版控）
└── outputs/             # 研究輸出；只有 .md 紀錄與少數 fixture 進版控
```

人工流程停在：`量化候選 → AI 基本面／新聞研究 → 人工決策`。`execution` 僅讓回測
符合可成交性，**不會連券商 API，也不會自動送單**。

---

## Quickstart（乾淨環境）

從零開始到驗證 repo：建立環境與安裝依賴可能需要連 Python 套件站；安裝完成後的
測試與 preflight **不需要 token，也不呼叫 FinMind／TWSE／TPEx**。

```bash
git clone <this-repo> tw-swing-factor
cd tw-swing-factor

# 1) 環境。系統 python3 可能太新導致套件裝不起來，固定用 3.11 建 .venv。
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2) 離線驗證（不需 FINMIND_TOKEN；測試全部 mock 掉 HTTP）
PYTHONPATH=. .venv/bin/python -m unittest discover -s tests -p 'test_*.py'

# 3) 公開前檢查（密鑰檔名／內容、資料產物是否誤追蹤、必要文件是否齊全）
PYTHONPATH=. .venv/bin/python preflight.py
```

以上兩步就是 CI 跑的內容（[.github/workflows/ci.yml](./.github/workflows/ci.yml)，
Python 3.11、市場資料離線）。CI 的 checkout 與依賴安裝仍需網路；**只有抓真實市場
資料才需要 FinMind token**，見下一段。

> 專案慣例：一律用 `.venv/bin/python`，測試是 `unittest` 不是 pytest。
> 完整工作規則見 [AGENTS.md](./AGENTS.md)。

### FinMind Token（只有要抓真實資料時才需要）

- **只從環境變數 `FINMIND_TOKEN` 讀取。** 程式不會搜尋其他 repo 的 `.env`，
  也不會把 token 放進 URL（一律走 `Authorization: Bearer` header，避免落進
  shell history、process list 或伺服器 log）。

```bash
export FINMIND_TOKEN="..."            # 或用 direnv／密碼管理器注入
```

- `.env.example` 是**無值樣板**。要用就複製成 `.env` 由 shell/direnv 載入；
  程式本身不讀 `.env`。`.env` 與 `.env.*` 已在 `.gitignore`，`preflight.py` 會再擋一次。
- API 連線／額度／認證失敗會**明確報錯**，不會回空表冒充「沒有資料」——
  靜默回空表會讓整年資料被漏掉還看起來正常。
- FinMind 免費層 600 次/小時，超額回 HTTP 402。

### 資料與快取不進版控

| 路徑 | 進版控？ | 原因 |
|---|---|---|
| `_cache/` | ❌ 一律不 | 原始資料快取，體積大且與 `SNAPSHOT_END_DATE` 綁定 |
| `outputs/*.md` | ✅ | 研究紀錄（含已撤回結論），append-only |
| `outputs/universe_top*.json` | ✅ | 重現 legacy static 對照組所需的 fixture |
| `outputs/FROZEN_MANIFEST_*.json` | ✅ | 凍結規則，依定義不可覆寫 |
| `outputs/` 其餘（csv/json/log/pickle） | ❌ | 可重新產生的研究產物 |
| `.env` / 私鑰 / 憑證 | ❌ | 密鑰 |

`preflight.py` 會對 **git 追蹤中的檔案**（不是工作樹）檢查上表，並掃描私鑰標頭與
已填值的 token 指派。命中時只印規則與行號，**不印內容**——把疑似 token 印進 CI log
等於再洩一次。

---

## 使用

> 以下需要 `FINMIND_TOKEN` 與本機 `_cache/`。第一次跑會抓很多資料。

```bash
# 今日選股（小集合，快速 smoke）
.venv/bin/python main.py screen

# 今日選股（全市場，較慢）
.venv/bin/python main.py screen --full

# 回看某一天當時會選出什麼（驗證用）
.venv/bin/python main.py screen --date 2026-05-20

# 回測 + 因子IC。IS／embargo／OS 兩段各跑滿所有等價再平衡相位
.venv/bin/python main.py backtest
.venv/bin/python main.py backtest --full --pool 300 --universe-top 100 --top 5

# 只看因子IC分析
.venv/bin/python main.py ic
```

輸出會印在畫面，同時存到 `outputs/`（大部分不進版控）。

### 所有等價再平衡相位（不是挑一條路徑）

`main.py backtest` 對 IS 與 OS **各跑滿 `--rebalance N` 個等價進場相位**，印出每個
相位並彙總**中位數與最小值**。這不是排場：同一訊號換個起跑日，Sharpe 實測可以從
-0.09 擺到 +1.09，S19 的相位標準差 0.509 幾乎等於訊號效果本身。
**只報單一相位＝挑路徑。** 決策看中位數與最差值，不看最大值。

### IS/embargo/OS 切割（統一入口）

所有研究腳本都使用 `evaluation_split.py`，IS、embargo、OS 不重疊。預設依交易日
70/30 切割；若因子標籤使用未來 N 日，embargo 不足會直接拒跑。

```bash
# 前 70% IS、後段 OS，中間 20 個交易日 embargo
export SWING_EVAL_SPLIT_MODE=ratio
export SWING_IS_RATIO=0.70
export SWING_EMBARGO_DAYS=20

# 或固定長度：資料尾端 26 週作 OS，往前留 embargo，再取 52 週作 IS
export SWING_EVAL_SPLIT_MODE=weeks
export SWING_IS_WEEKS=52
export SWING_OS_WEEKS=26
```

`SNAPSHOT_END_DATE` 仍要固定；它控制資料快照，IS/OS 則在凍結資料內切割，兩者不能
互相取代。參數選擇只能看 IS；OS 一旦被用來選權重或規則，就只能標為 pseudo-OOS。

### 台股成交與股數模式

台股普通股升降單位、10% 漲跌停、整張／零股與成本規格見
[TAIWAN_MARKET_RULES.md](./TAIWAN_MARKET_RULES.md)。預設 `research_fractional` 只供
純量化效果比較；要檢查實際整張資金限制，請明確設定：

```bash
export SWING_ORDER_SIZE_MODE=regular_lot
export SWING_INITIAL_CAPITAL=1000000
export SWING_MIN_COMMISSION=20  # 依自己的券商修改
```

`odd_lot_proxy` 沒有使用獨立零股成交行情，不能視為精確可成交回測。

### 兩層 PIT universe（正式回測路徑）

不要再用期末 top100／top300 固定回套整段歷史。正式回測固定使用兩層 PIT 規則：

1. M 月候選 top300 只用**完整 M-1 曆月**的交易所逐日成交值建立。
2. 每個訊號日只在該月已鎖定的候選池內，依截至當日近20日平均成交值重排 top100。

因此當月行情與今天的熱門名單都不可能回頭改寫歷史 universe：

```bash
# 第一次使用先補齊交易所逐日快照（之後逐日快取重用）
.venv/bin/python -c "import pit_universe as p; p.load_history('2024-06-01', '2026-06-22')"

# long-only；上月 PIT 候選 top300；每日 universe top100；每次最多挑5檔
.venv/bin/python main.py backtest --full --pool 300 --universe-top 100 --top 5

# legacy static universe 只供對照
.venv/bin/python main.py backtest --pool 100 --static-universe --top 5
```

動態 universe 只改變「當日可被選的股票」，不做空、不建立 short leg。正式路徑
metadata 會標示 `candidate_rule=month_M_uses_only_calendar_month_M_minus_1` 與
`candidate_membership_survivorship_free=True`；若逐日快照缺任何平日則 fail-closed。
`screen` 使用當下名單是合理的即時用途，但該名單不得回套歷史。已下市股價格仍
可能缺漏，所以整體 `survivorship_free` 仍維持 `False`，直到價格覆蓋也通過稽核。

**因子要在稠密 panel 上算。** 動態 universe 的成員會間歇進出，若先過濾成員再算
`ts_` 算子，「20 列」可能橫跨 60+ 個日曆日。引擎因此保留非成員列（`keep_non_members`）
＋`in_dynamic_universe` 旗標，成員過濾留到選股那一步。

### 價格完整性：fail-closed，不是警告

未還原價格會直接產生假交易（國巨 2327 的公司行動斷點曾被記成 -73.6% 的 hard-stop）。
`backtest._assert_price_integrity` 因此在資料不合格時 **raise**，判定順序：

1. 資料集本身是還原價 → 放行
2. `SELF_ADJUST_PRICES`（預設開）對**還原後**序列掃殘留斷點，有殘留就擋
3. `SWING_ALLOW_UNADJUSTED=1` → 印警告放行，但結果戳 `integrity_bypassed=True`
4. 否則未還原價一律 raise

**斷點掃描不是放行條件。** 除息缺口 3~5% 落在 ±10% 漲跌停帶內，掃描結構上看不到，
所以「掃描 0 命中」不等於「價格乾淨」。被擋住時的正確反應是排除有問題的股票
（`outputs/price_integrity_excluded.json`），不是開逃生門；開了逃生門產出的數字
不得寫進任何報告。

### field 與 operator 的分界

> **無視窗參數的衍生量 → field；有視窗的 → operator**

`factor_engine/data_fields.py` 提供 8 個無視窗欄位（`vwap`、`returns`、`true_range`、
`gap`、`intraday_ret`、`close_loc`、`dollar_volume`、`amihud`）；
`factor_engine/operators.py` 提供有視窗的因果算子（`ts_*` / `cs_*` / `group_*`）。
這是 WorldQuant 把 `vwap`/`returns` 當 data、而 RSI/ATR 不是的同一條線：後者的視窗
長度該進搜尋空間，不該寫死。所有 `ts_` 算子都有測試釘住「附加未來資料不改變過去的值」。

### 族群輪動＋法人＋突破研究

```bash
.venv/bin/python rotation_research.py
```

固定流程為：動態 universe → 粗產業族群強度與法人買盤 → 個股 20 日價量突破 →
次日開盤買入。比較純動能、族群篩選、族群＋法人＋突破，以及 MA10 / MA20 /
固定 20 / 40 日等出場；輸出 `rotation_is_oos.csv`、`rotation_trades.csv` 與
`theme_case_audit.csv`。這是研究候選，不是自動交易建議。

### Live dynamic-universe 與 rank-flow 新策略實驗

```bash
# 官方 TWSE 資料；每日 ADV20 前300後才重算 flow rank
.venv/bin/python market_flow_monitor.py \
  --as-of 2026-07-23 --calendar-days 90 --universe-size 300 --top-n 20

# 四個固定 rank-flow 假說；T+1 open、5/10/20日事件研究
.venv/bin/python rank_flow_strategy.py \
  --metrics outputs/market_flow_metrics_20260723.csv \
  --breadth outputs/market_flow_breadth_20260723.csv

# 下一版：需120個乾淨ATR觀察；歷史不足時應輸出零訊號
.venv/bin/python quiet_sponsor_strategy.py \
  --metrics outputs/market_flow_metrics_20260723.csv
```

Live monitor 會把 >20% 價格斷點及後續20個該股觀察日先 quarantine，再建動態池與
排名，不猜公司行動調整倍數。2026-04-24~07-23 的 62 日探索窗中，四個
rank-flow 變體都沒有跨 5／10／20 日一致超額，故目前**不能單獨作買進策略**；
完整失敗結果與下一版假說見
[RANK_FLOW_EXPERIMENT_REVIEW.md](./outputs/RANK_FLOW_EXPERIMENT_REVIEW.md) 與
[NEW_STRATEGY_EXPERIMENTS.md](./NEW_STRATEGY_EXPERIMENTS.md)。`quiet_sponsor`
目前因只有62日而輸出 `insufficient_history`／零訊號，規則不為此縮短。

---

## 因子說明

### 動能面（找「下一波成長」的核心）
| 因子 | 定義 | 直覺 |
|---|---|---|
| `momentum` | 60日報酬(到+30%滿分) + 貼近季線高點(≥0.90滿分)，取平均 | 強者恆強，趨勢健康的續強股 |

> 這是修正版新增的因子。原型缺少動能訊號，但「找下一波成長股」最核心的特徵就是**強勢續強**（學術界的 momentum factor、實務的 Qullamaggie 突破系統都以此為主軸）。

### 籌碼面（核心假設）
| 因子 | 定義 | 直覺 |
|---|---|---|
| `inst_mid` | 法人(外資+投信)近6日淨買 / 近20日均量 | 主力中期在不在買 |
| `inst_long` | 法人近12日淨買 / 近20日均量 | 主力長期是否未撤 |
| `inst_dip_buy` | 近5日「收黑但法人仍買」的天數 | 洗盤 vs 出貨 |
| `margin_health` | 資券比（融資/融券）落在 2~8 健康區間 | 籌碼結構是否健康 |

> **關鍵工程細節**：法人淨買一律**用近20日均量正規化**，這樣台積電（大型股）和小型股才能公平比較。主力定義為**外資+投信**，排除自營商（避險雜訊）。

### 技術面
| 因子 | 定義 | 直覺 |
|---|---|---|
| `ma_alignment` | 收盤 > MA20 > MA60 | 均線多頭排列 |
| `bb_pullback` | 布林位階在 0~0.5（拉回月線但未跌破） | 波段回檔買點 |
| `ma_squeeze` | MA20/MA60 BIAS 都很小 | 均線糾結、能量壓縮 |
| `vol_dryup` | 近5日均量 / 前5日均量 ≤ 0.5 | 回檔量縮（窒息量） |

### 趨勢保護（硬門檻，任一不過直接淘汰）
`MA20 > MA60` 且 `MA60 上揚` 且 `收盤 > MA60` —— 避免接刀（買在下降趨勢）。

### 綜合評分
各因子輸出 0~1 分數，依 `config.FACTOR_WEIGHTS` 加權後正規化成 0~100。權重可自由調整。

---

## 防未來函數 + 回測正確性（重要）

回測最容易作弊的地方就是偷看未來，以及把投組績效算錯。本系統的防護：

1. **T+1 進場**：訊號在第 T 日收盤後產生，第 T+1 日**開盤**才進場（`BT_ENTRY_NEXT_OPEN`）
2. **資料對齊**：法人/融資資料用 `merge_asof(direction="backward")` 對齊到價格日，只會用「≤ 當日」最近一筆
3. **因果計算**：所有 rolling 指標只看過去
4. **未來報酬隔離**：`fwd_ret`（未來N日報酬）只在 IC 分析時用，絕不進因子計算
5. **真正的每日權益曲線**：等權重最多 `BT_MAX_POSITIONS` 檔並行持倉，逐日 mark-to-market 加總成投組淨值，MaxDD / Sharpe 全部由淨值序列算
6. **跳空填價**：停損/停利當天若開盤已穿價，用開盤價成交（更不利），不用理論價
7. **IC 重疊校正**：`fwd_ret` 視窗重疊造成每日 IC 自相關，t 值用「有效樣本 = 天數 / 視窗」保守校正，避免灌水顯著性
8. **統一 IS/OS 邊界**：比例或固定週數都由 `evaluation_split.py` 建立；未來標籤
   視窗大於 embargo 時 fail-closed
9. **評估窗上界**：外部選股訊號預設截到最後訊號日，`summary.eval_audit` 必須確認
   `days_beyond_last_pick == 0`

---

## 回測結果 — ⛔ HISTORICAL / INVALID（2024-06 ~ 2026-06，小集合 14 檔）

> ### ⛔ 這一節的數字**已失效，不得引用為績效**
>
> 保留原文是因為它記錄了「舊版怎麼錯、怎麼被抓到」，這個過程本身有價值。
> 但下表產生時的三個前提都已經不成立：
>
> | 當時 | 現在的規則 | 影響 |
> |---|---|---|
> | 14 檔 static sample universe | 兩層 PIT（上月候選 → 每日 top-N） | 選股母體不同，不可比 |
> | 未還原價 `TaiwanStockPrice` | 價格完整性 fail-closed，未還原價直接 raise | 公司行動斷點曾被記成真實虧損 |
> | 單一再平衡相位、單一全期路徑 | IS／embargo／OS × 跑滿所有等價相位 | 單一路徑等於挑路徑 |
>
> 也就是說：**這組數字在現行程式下根本跑不出來**（未還原價會被閘門擋下）。
> 它是 historical record，不是 baseline，更不是可重現的結果。
> 目前有效的結論一律以 [STRATEGY_REGISTRY.md](./STRATEGY_REGISTRY.md) 為準。
>
> **退場模式：trend（波段）** — 跌破 MA20 或硬停損 -8% 或抱滿 120 天才出場。

| 指標 | 數值（已失效） | 說明 |
|---|---|---|
| 交易筆數 | 35 | 平均持有 12.7 天（最長可達 120 天） |
| 勝率 | 28.6% | 低勝率 |
| 賺賠比 (payoff) | 4.96 | **靠少數大贏家獲利** — 趨勢波段策略的典型特徵 |
| 累積報酬 | +16.39% | |
| 年化報酬 | +8.53% | |
| **最大回撤** | **-12.35%** | 由每日淨值算 |
| Sharpe (年化) | 0.76 | 由每日淨值算 |

> ⚠️ **35 筆樣本太少，不能下定論。** 低勝率高賺賠比符合「找成長股、讓獲利奔跑」的設計目標，但需擴大 universe 驗證穩定性。

### 逐因子 IC（重疊校正後）— 同樣為 HISTORICAL / INVALID

> 同一批 14 檔 static、未還原價的資料算出來的，數值不可引用。
> **但底下那個「沒有任何因子被證明有效」的負面結論並未被推翻**——它至今仍成立，
> 而且後續在更大的池子上又被證實了一輪（見 `STRATEGY_REGISTRY.md` S01/S02）。

| 因子 | mean IC（已失效） | t_stat（已失效） | 判讀 |
|---|---|---|---|
| ma_alignment（均線多頭） | +0.087 | +1.28 | 弱訊號，未達顯著 |
| margin_health（資券健康） | +0.071 | +0.95 | 弱訊號，未達顯著 |
| momentum（動能） | +0.061 | +0.83 | 弱訊號，未達顯著 |
| inst_long（法人12日） | +0.045 | +0.64 | 弱訊號，未達顯著 |
| inst_mid（法人6日） | +0.027 | +0.41 | 弱訊號，未達顯著 |
| inst_dip_buy（跌時法人買） | +0.006 | +0.09 | 無明顯預測力 |

> ⚠️ **修正前後的關鍵差異**：舊版宣稱 ma_alignment / bb_pullback / margin_health「★有預測力」，但那是**沒做重疊校正**的灌水結果。重疊校正後 **所有因子 t_stat 都 < 2（未達統計顯著）** —— 這才是 14 檔小樣本該有的誠實結論：**現階段沒有任何因子被證明有效**，必須擴大 universe 才能下判斷。
>
> 舊版「最大回撤 -35.69%」也是**序列複利算錯**的假數字，修正後實際為 **-12.35%**。

---

## 目前的研究證據限制（讀任何數字前先看這裡）

**這個 repo 沒有可用的策略績效。** 有的是一套會擋住自己說謊的框架，加上一份誠實的
失敗清單。以下限制目前都還沒解除：

1. **沒有 clean OOS。** 既有 OS 區段都已被研究者看過、或曾參與參數／權重選擇，
   依 `RESEARCH_OPERATING_PROTOCOL.md` 的證據等級只能算 **pseudo-OOS**。
   升級路徑唯一：`freeze_manifest.py` 凍結規則 → 累積凍結後的新資料 → `forward_test.py`。
2. **價格仍預設未還原。** 官方 `TaiwanStockPriceAdj` 在免費層被鎖；`price_adjust.py`
   自建還原只處理除權息，**不含分割與減資**。正式回測因此預設 fail-closed。
3. **候選成員 PIT，價格覆蓋不是。** 每月 PIT 池含當時在市、後來下市的股票
   （`candidate_membership_survivorship_free=True`），但下市股的完整價格序列可能缺，
   所以整體 metadata 的 `survivorship_free` 仍是 **`False`**。
4. **產業分類不是 PIT。** 歷史日期套用當前 FinMind 標籤，族群策略保留 `industry_pit=False`。
5. **資料只有約兩年，且是單一偏多頭 regime。** 沒有足夠空頭樣本，任何檢定的檢定力都低。
6. **處置／下市資料不完整。** TWSE 歷史處置是由注意名單推導的 proxy（`source="derived"`），
   只有 TPEx 是 `actual`。下市現金清算／換股條件沒有正式資料。
7. **漲跌停與成交容量是近似。** 一字鎖停由 OHLC 判斷，沒有逐日委託簿與撮合量，
   結論仍需滑價與容量敏感度測試。
8. **`outputs/` 裡的舊報告是 append-only 研究紀錄。** 不要因為檔名含 `REPORT`、`OOS`
   或有漂亮 Sharpe 就當成目前有效；判讀順序見 [outputs/README.md](./outputs/README.md)。

> 一句話版本：**看到任何 Sharpe 之前，先確認 `integrity_bypassed == False`、
> `eval_audit["days_beyond_last_pick"] == 0`、相位跑滿、且比較對象是被動基準而不是零。**
> 動態 universe 等權買進持有在 IS 就有 Sharpe 1.17；贏不過它就不是 alpha。

---

## 路線圖

- [x] **階段一（已完成）**：快速原型——資料層、因子、選股、回測閉環跑通
- [ ] **階段二**：擴大 universe 到全市場（數百檔），重算因子 IC，確認哪些因子真有 edge
- [x] **階段三基礎設施**：統一 IS/OS 比例／固定週數切分 + Embargo + 評估窗稽核；
  各策略仍須逐一以乾淨 PIT 資料與未見 OS 重跑，不能沿用舊報告宣稱通過
- [x] **公開工程包**：離線 CI（Python 3.11 + unittest + 語法 smoke）、
  `preflight.py` 密鑰／資料產物／文件檢查、無密鑰 `.env.example`
- [ ] **階段四**：依驗證結果重新分配因子權重（讓有 edge 的因子主導），加入退場機制
- [ ] **階段五**：接 LINE/Telegram 推播（可複用 tw-stock-linebot-reporter）

> 授權條款（LICENSE）**尚未決定**，需由 repo owner 選定；在那之前本 repo 沒有
> 明示授權。詳見 [PUBLIC_REPO_AUDIT.md](./PUBLIC_REPO_AUDIT.md) 的 owner decision。

---

## 參考的開源專案

| 專案 | 借鑑的點 |
|---|---|
| `taiynlee/institutional-investors` | 主力未撤回檔邏輯、籌碼用股本正規化、雙時間窗確認 |
| `vivianlin0529-coder/taiwan-chip-wave-screener` | 籌碼集中度 + 法人 + 資券比 + 黃金回撤的加權評分 |
| `hu0937/FinPilot` | 多因子自動探索、三關驗證、防未來函數（公告日對齊） |
| `kevin801221/stock-strategies-only` | 基本面+技術面+夜盤期貨、GitHub Actions 自動化 |

---

## 免責聲明

本專案為**研究與學習用途**，所有回測結果僅供參考，不構成投資建議。回測有效不代表未來有效，實盤請自負風險。
