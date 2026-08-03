# AGENTS.md — 在這個 repo 工作的規則

> 這是台股 long-only 波段選股的**量化研究** repo,不是一般應用程式。
> 這裡的「對」不是「跑得動」,而是「數字沒有被偏誤污染」。
> 一個會產生假 Sharpe 的 bug,比一個會 crash 的 bug 嚴重得多 —— crash 看得見,
> 假 Sharpe 會被當成結論寫進報告。

## 環境鐵則

```bash
.venv/bin/python              # 一律用這個。系統 python3 太新,套件不在
PYTHONPATH=. .venv/bin/python tests/test_xxx.py    # 測試是 unittest,不是 pytest
```

- 資料鎖 `config.SNAPSHOT_END_DATE`(現 `2026-06-22`)。快照戳編進快取檔名,
  改快照 = cache miss = 真重抓。**不要為了跑快而繞過快照。**
- 環境變數覆寫:`SWING_SNAPSHOT_END` / `SWING_ALLOW_UNADJUSTED` /
  `SWING_SELF_ADJUST` / `SWING_ALLOW_FUTURE_POOL` / `SWING_MODEL_DISPOSITION`
- FinMind 免費層 **600 次/小時**,超額回 402。全市場重抓要寫額度感知的背景腳本
  (參考本 repo 做過的作法:查 `api.web.finmindtrade.com/v2/user_info` 的 `user_count`)。

## fail-closed 閘門:不要繞過,要理解

`backtest._assert_price_integrity` 會在資料不合格時 **raise**。這是刻意的。

判定順序:
1. 資料集是還原價 → 放行
2. `SELF_ADJUST_PRICES`(預設開)→ 對**還原後**序列跑殘留斷點掃描,有殘留就擋
3. `ALLOW_UNADJUSTED_BACKTEST=1` → 印警告放行,結果戳 `integrity_bypassed=True`
4. 否則未還原價一律 raise

**被擋住時的正確反應是排除有問題的股票,不是開逃生門。**
`outputs/price_integrity_excluded.json` 就是這樣來的(283/300 檔乾淨池)。
開了逃生門產出的數字不可寫進任何報告。

歷史教訓:曾經的閘門是「未還原價 **且** 審計命中」才擋,等於把「掃描沒掃到」
當成「價格乾淨」的證據。但除息缺口 3~5% 在 ±10% 漲跌停帶內,掃描結構上看不到。
現在放行只看資料集是否還原,審計降級為診斷。

## 已知會產生假結果的陷阱(都真的發生過)

### 1. panel 稀疏 → ts_ 算子失真

```python
# 錯:預設只留動態 universe 成員日
panel = backtest._prepare_panel(syms, ...)
o.ts_ir(ret, 20)   # 算的是「20 列」,一檔間歇進出 universe 的股票會橫跨 60+ 個日曆日

# 對:算因子用稠密 panel,成員過濾留到選股時
panel = backtest._prepare_panel(syms, ..., keep_non_members=True)
score = build_signal(panel)                     # 在稠密 panel 上算
picks = panel[panel["in_dynamic_universe"]]     # 選股時才篩
```

這個坑在 wide 矩陣格式下不可能發生(日期是 index),但我們用 long panel,
對齊責任在寫程式的人身上。

### 2. 只報單一再平衡相位 = 挑路徑

同一訊號的不同執行相位,Sharpe 可以從 **-0.09 擺到 +1.09**。
**永遠跑滿所有等價相位,報中位數與最小值,不是最大值。**
參考 `chip_momentum_strategy.evaluate()`。

### 3. 基準要跟引擎同慣例,且先算報酬再篩成員

```python
# 錯:先篩成員再 pct_change → 成員進出的日期斷點被當成單日巨幅報酬
#     (實測基準年化被灌到 +1150%、Sharpe 28)
# 對:
full["r"] = full.groupby("stock_id")["close"].pct_change()   # 先算
full = full[full["in_dynamic_universe"] == True]              # 後篩

# 年化用算術慣例,與 backtest 引擎一致
ann = r.mean() * 252 ; vol = r.std(ddof=1) * np.sqrt(252)
# 用幾何報酬配算術波動會在極端多頭把 Sharpe 從 4.20 灌到 10.48
```

### 4. 候選池的 look-ahead

候選池(`outputs/universe_top*.json`)是**單一日期**的排名。用它回套整段歷史 =
用今天知道誰熱門去決定兩年前能選誰。實測舊池 283 檔有 83 檔在回測起點連前 200
名都排不進去。修法見 `pit_universe.py`(逐時點重建,含下市股)。

動態 universe(每日 top100)本身是 PIT 的、沒問題 —— 問題只在它上面那層候選池。

### 5. 網路瞬斷靜默回空表

交易所端點會偶發 `ChunkedEncodingError`。若回空表會被當成「該期間無資料」,
**靜默漏掉整年**。一律重試,耗盡後 raise。

## 研究紀律

完整版見 `RESEARCH_OPERATING_PROTOCOL.md`。最低限度:

- **永遠分 IS/OS 看**(`config.IS_OS_SPLIT = 0.70`)。只看全期會被普漲 OS 騙 ——
  這個 repo 已經被騙過至少兩輪(見 `STRATEGY_REGISTRY.md` 的 S02)。
- **和基準比,不是和零比。** 動態 universe 等權買進持有在 IS 就有 Sharpe 1.17。
  策略贏不過基準就不是 alpha。
- **選參數用穩健性,不用最大值。** 挑相位中位數、看鄰域是否一致。
- **負面發現要寫進 `STRATEGY_REGISTRY.md`**,避免後人重做。
  (已證偽的:買弱/接刀、天真 vol 節流、rank-flow、winner_dna、融資餘額下降=散戶退場)
- IC 顯著 ≠ 可上線。發掘層 → 嚴格回測 → freeze/forward,證據等級逐級升。

## 程式碼慣例

- 註解與文件用**繁體中文**,程式碼識別字用英文。
- 註解寫「**為什麼**」與「踩過什麼坑」,不要寫程式碼已經說明的事。
  這個 repo 的註解密度偏高是刻意的 —— 多數陷阱不寫下來就會重犯。
- 新策略要註冊進 `STRATEGY_REGISTRY.md`(狀態、規則、證據等級、已知偏誤、
  下一個可證偽測試)。
- 因子一律用 `operators.py` 的算子組(對齊 WorldQuant 語意,全因果)。
- 測試用 `unittest`,放 `tests/`,**離線**(mock 掉 HTTP)。
  修完 bug 要留回歸測試,並在 docstring 說明那個 bug 是什麼。

## 專案地圖

| 檔案 | 作用 |
|---|---|
| `config.py` | 所有可調參數。改參數只改這裡 |
| `data.py` | FinMind 資料層 + 快取(含快照戳) |
| `price_adjust.py` | **自建還原價**(除權息回溯) |
| `price_integrity.py` | 斷點稽核(診斷用,非放行條件) |
| `pit_universe.py` | **PIT 候選池**(交易所逐日快照,含下市股) |
| `universe.py` / `dynamic_universe.py` | 候選池載入 / 每日 top-N 成員 |
| `factors.py` | 傳統因子與 0~1 分數 |
| `operators.py` | WorldQuant 式算子庫(ts_ / cs_ / group_ / regression_) |
| `backtest.py` | 事件驅動回測引擎(T+1 開盤、漲跌停、處置禁倉) |
| `chip_momentum_strategy.py` | S19 策略單元 |
| `live_signal.py` | 精簡資料路徑(只用 price+inst,省 API 額度) |
| `twse_disposition.py` / `tpex_disposition.py` | 注意/處置資料層 |
| `DATA_SOURCES.md` | **免費資料源實測盤點 —— 找資料先看這裡** |
| `STRATEGY_REGISTRY.md` | 策略台帳(狀態/證據/已證偽) |
| `PORTFOLIO_BUILDING.md` | 組合建構前置盤點(相關性矩陣) |
| `RESEARCH_OPERATING_PROTOCOL.md` | 研究鐵則 |

## 架構備註

因子層是 **long panel**(每列一個 `(date, stock_id)`),不是你可能習慣的
wide 矩陣(日期 × 股票)。`operators.PanelOps` 在 long 上模擬 wide 語意:
`ts_*` 用 `groupby(stock).rolling`、`cs_*` 用 `groupby(date)`。
兩者數學等價(實測 130,930 個值最大差異 2.84e-14),但 wide 快約 6 倍,
且不會發生上面第 1 個陷阱。

執行層是**事件驅動**,不是 `weights × returns` 向量化 —— 因為要表達路徑相依的
執行真實性(一字漲停買不到、MA 跌破次日開盤才成交、處置期間禁新倉)。
兩層的介面是 `picks_by_date`,可以只換因子層。
