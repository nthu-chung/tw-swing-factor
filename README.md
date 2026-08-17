# tw-swing-factor — 台股波段多因子選股系統

用**籌碼面 + 技術面 + 大戶進出**多因子組合，挑選台股波段標的（持有數天～數週，白天可操作、不熬夜），並用**嚴格回測誠實驗證每個因子到底有沒有 edge**。

> 這套系統的定位不是「再做一個會喊強力買進的選股器」，而是一個**能證偽的研究框架**——先用快速原型跑通流程，看哪些因子真的有預測力，再決定要不要相信它。

## 專案目標：量化先選，AI 再研究

這個專案的完整願景分成兩個清楚隔離的階段：

```text
階段 A：量化選股（本 repo 目前實作）
PIT 資料 → 動態 universe → 數學因子／策略排名 → 事件驅動回測
         → 可稽核的候選股票

階段 B：AI 分析師（未來規劃，尚未實作）
量化候選 + 當時可得的財報／公告／新聞 → AI 基本面與事件研究
                                        → 人工決策 → 手動買賣
```

因此，**目前這個 repo 是量化選股與回測系統，不是 AI 選股產品**。它的責任是先用
可重現的數學規則縮小研究範圍，並誠實回答策略是否真的勝過基準。未來 AI 層只會針對
量化候選補充公司基本面、產業脈絡、公告與新聞風險，幫助人類做第二層判斷。

為了知道 AI 是否真的增加價值，未來會保留兩組獨立結果：

- A 組：純量化排名直接形成的候選組合。
- B 組：相同候選名單再經 AI 研究後的人工篩選組合。

兩組只能以凍結後的 forward／untouched OOS 比較。AI 研究輸出必須保留 as-of 時間、
來源與理由，不能回頭改寫量化分數，也不能把事後新聞塞回歷史回測。目前規劃中的
`analyst_research/` 尚未建立；`execution/` 只是回測成交模擬，不是券商下單系統。

> 這個 repo 公開的是**研究平台本身**——策略骨架、事件驅動回測引擎，以及那一整套
> 會擋住你自己說謊的閘門。策略的證據狀態逐支記在
> [STRATEGY_REGISTRY.md](./STRATEGY_REGISTRY.md)；請以那份為準，不要從 repo 裡
> 任何一個數字反推「這套有效」。

---

## 為什麼做這個

市面上的台股選股開源專案共同弱點是：**選股邏輯花俏，但回測太陽春**（常常只用固定持有 N 天算個勝率就說「有效」）。本專案反過來，把重心放在**驗證**：

- 整體回測：勝率 / 平均報酬 / 累積報酬 / 最大回撤 / 類 Sharpe
- 逐因子 IC（資訊係數）：每個因子對未來報酬的 Spearman 相關，看誰真的有用
- 嚴格防未來函數（point-in-time 對齊、T+1 進場）

---

## 架構

完整的模組責任與人工投資流程見 [ARCHITECTURE.md](./ARCHITECTURE.md)。

2026-08-16 完成套件化:**功能一律住在套件裡,根目錄只留基礎設施與使用者入口**。
舊的相容轉發層(`factors.py` / `operators.py` / `evaluation_split.py` /
`chip_momentum_strategy.py`)已移除 —— 同一個東西有兩條匯入路徑,遲早會有人以為
它們是兩個不同的模組。

這個 repo 是一個**研究平台**:未來的參數搜尋(GA)會放在獨立資料夾,import 這裡的
`build_panel` / 策略 registry / golden path,而不是長在這個 repo 裡面。

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
├── backtest/
│   ├── __init__.py      # 只有說明,不轉出任何東西 —— 兩個引擎都必須指名
│   └── event_backtest.py# 事件驅動引擎(T+1、漲跌停、處置、整股、成本)
│                        # 慢但精確,**唯一可作正式證據**
├── strategies/          # **純策略** —— 這裡每個 .py 都是一支策略,沒有機器
│   ├── h1_volume_breakout.py  h3_short_reversal.py
│   ├── h11_oversold_bounce.py h13_margin_washout.py
│   │                    # 以上四支都是**已被證偽**的假說,留作平台的可執行範例;
│   │                    # 這不是候選池,也不是研究全貌(見 STRATEGY_REGISTRY.md)
│   └── s19_chip_momentum.py   # legacy 端到端模組(會改全域 config)
├── strategy_kit/        # **機器** —— 策略要用的東西,但它們不是策略
│   ├── signal_builder.py# 分數 → 合格 SignalFrame;每支策略只寫 score()
│   ├── registry.py      # allowlist,逐檔顯式註冊(不自動掃描目錄)
│   ├── position_policy.py # 分數 → 想要的部位(含 -20% 災難停損)
│   └── contracts.py / spec.py
├── research/
│   ├── golden_path.py   # **唯一正式入口**:strategy → validator → 五相位
│   │                    #   → 引擎 → artifacts
│   ├── signal_validation.py # SignalFrame 的唯一 validator(內外共用不開特例)
│   ├── screening.py     # 人類可讀候選清單(薄視圖,不重算不重排)
│   ├── holdout.py       # 單次 IS／embargo／locked-OS 資料閘門
│   └── fixtures.py / artifacts.py / contracts.py
├── data/
│   ├── __init__.py      # FinMind 資料層 + 快取(含快照戳)
│   ├── price_adjust.py  # 自建還原價(除權息回溯)
│   ├── price_integrity.py # 斷點稽核(診斷用,不是放行條件)
│   └── twse_disposition.py / tpex_disposition.py / return_convention.py
├── universes/
│   ├── monthly_pit.py   # **正式候選池**:M 月只用完整 M-1 曆月
│   ├── pit_snapshots.py # 交易所逐日快照(含下市股)
│   ├── dynamic.py       # 每日 ADV20 top-N 成員
│   └── legacy_static.py / build.py  # legacy,僅供對照,不得回套歷史
├── factor_engine/
│   ├── operators.py     # 因果算子(37 ts_ / 8 cs_ / 9 group_)
│   │                    #   cs_/group_ 的**排名母體**由 PanelOps 的 ranking_mask
│   │                    #   決定,ts_ 一律看完整序列
│   ├── data_fields.py   # 無視窗衍生欄位(vwap/returns/true_range…)
│   └── legacy_factors.py
├── execution/           # 回測成交限制;不是券商下單或自動交易
│   └── taiwan_rules.py / tradability.py / costs.py
├── evaluation/
│   └── splits.py / phases.py / holdout.py  # 切割 / 唯一相位掃描 / 揭露台帳
├── config.py            # 可調參數與凍結資料邊界
├── security_type.py provenance.py         # 普通股白名單 / git 指紋
├── main.py screener.py current_watchlist.py  # legacy CLI(僅供對照)
├── preflight.py         # 公開前的離線密鑰／產物／文件檢查
├── _cache/              # 原始資料快取(自動產生,不進版控)
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

### Golden Path：策略、回測與人類可讀候選清單

```bash
# 完全離線跑通 strategy → validator → 五相位 → 事件引擎 → artifacts
PYTHONPATH=. .venv/bin/python -m research.golden_path \
  --strategy s19_reference_make_signals --fixture synthetic \
  --capital research --output-dir /tmp/tw-swing-runs

# 顯示該 run 最後一個完整訊號快照的候選股票（不是交易指令）
PYTHONPATH=. .venv/bin/python -m research.screening --run-dir <上一步的 run_dir>
```

每個 Golden Path run 會包含 `candidate_screen.csv` 與 `candidate_screen.txt`。它們直接
來自已驗證的 `signals.csv`，不會另外用另一套因子重算或重排；實際部位、退出與成交
仍以 `decisions.csv`、`orders.csv` 及事件引擎結果為準。

### 所有等價再平衡相位（不是挑一條路徑）

回測**跑滿每一個等價進場相位**（週頻＝5 個），印出每個相位並彙總**中位數與最小值**。
這不是排場：同一訊號換個起跑日，Sharpe 實測可以從 -0.09 擺到 +1.09，相位標準差
0.509 幾乎等於訊號效果本身。**只報單一相位＝挑路徑。** 決策看中位數與最差值，
不看最大值。相位掃描只有一份實作（`evaluation/phases.py`），AST 守衛禁止第二份。

### IS/embargo/OS 切割（統一入口）

所有研究腳本都使用 `evaluation/splits.py`，IS、embargo、OS 不重疊。預設依交易日
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
.venv/bin/python -c "from universes import pit_snapshots as p; \
  p.load_history('2024-06-01', '2026-06-22')"
```

候選池與每日成員的預設值在 `config.py`（`DYNAMIC_UNIVERSE_CANDIDATE_POOL` /
`DYNAMIC_UNIVERSE_TOP_N` / `DYNAMIC_UNIVERSE_LOOKBACK`），正式路徑一律走
`universes.historical_pit_universe()`，不接受呼叫端自己湊 symbols 清單。

動態 universe 只改變「當日可被選的股票」，不做空、不建立 short leg。正式路徑
metadata 會標示 `candidate_rule=month_M_uses_only_calendar_month_M_minus_1` 與
`candidate_membership_survivorship_free=True`；若逐日快照缺任何平日則 fail-closed。
已下市股的價格序列仍可能缺漏，所以整體 `survivorship_free` 維持 `False`，
直到價格覆蓋也通過稽核。

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

### Live dynamic-universe 監控

```bash
# 官方 TWSE 資料；每日 ADV20 前300後才重算 flow rank
.venv/bin/python market_flow_monitor.py \
  --as-of 2026-07-23 --calendar-days 90 --universe-size 300 --top-n 20
```

Live monitor 會把 >20% 價格斷點及後續20個該股觀察日先 quarantine，再建動態池與
排名，不猜公司行動調整倍數。

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
8. **統一 IS/OS 邊界**：比例或固定週數都由 `evaluation/splits.py` 建立；未來標籤
   視窗大於 embargo 時 fail-closed
9. **評估窗上界**：外部選股訊號預設截到最後訊號日，`summary.eval_audit` 必須確認
   `days_beyond_last_pick == 0`
10. **基準是被動組合，不是零**：動態 universe 等權買進持有在 IS 就有 Sharpe 1.17，
    另外還有加權報酬指數（含息）可比。贏不過被動基準的超額不叫 alpha

> 這些是**程式**擋得住的部分。**資料**先天缺的東西擋不住，會限制結論能講多強
> ——見 [DATA_SOURCES.md 的「資料本身的邊界」](./DATA_SOURCES.md#資料本身的邊界讀任何回測數字前先確認)。

---

## 路線圖

- [x] **資料層與兩層 PIT universe**：交易所逐日快照 → 月頻候選池 → 每日 ADV20 成員
- [x] **事件驅動回測**：T+1／漲跌停／處置／整股／現金帳／成本，唯一可作正式證據的引擎
- [x] **評估邊界**：IS／embargo／OS 統一切割 + single-holdout 資料閘門 + 相位跑滿
- [x] **公開工程包**：離線 CI（Python 3.11 + unittest + 語法 smoke）、
  `preflight.py` 密鑰／資料產物／文件檢查、無密鑰 `.env.example`
- [x] **向量化近似引擎**：發掘階段的粗篩（`backtest/vec_backtest.py`），
  結構上不可冒充正式證據
- [ ] **universe 設計的系統性驗證**：候選池大小、每日 top-N、閘門與持股數的交互作用
- [ ] **參數搜尋（GA）**：放在獨立 repo，import 這裡的引擎與閘門，不反向修改本 repo

> 授權條款（LICENSE）**尚未決定**，需由 repo owner 選定；在那之前本 repo 沒有
> 明示授權。詳見 [PUBLIC_REPO_AUDIT.md](./PUBLIC_REPO_AUDIT.md) 的 owner decision。

---

## 免責聲明

本專案為**研究與學習用途**，所有回測結果僅供參考，不構成投資建議。回測有效不代表未來有效，實盤請自負風險。
