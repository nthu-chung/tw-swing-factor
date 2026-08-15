# StrategyPositionPolicy v1 — 單策略持有、退出與資金槽規格

> 狀態：contract-first implementation handoff
>
> 本文件是交給實作者的行為邊界，不指定內部演算法或類別拆分細節。實作者可以調整
> 內部設計，但不得改變本文件的外部語意、研究閘門與驗收案例。

## 1. 實作者任務

在現有唯一事件驅動回測路徑中加入 `StrategyPositionPolicy`，讓外部策略訊號不只提供
「今天可以買誰」，也能形成可稽核的 `enter / hold / resize / exit` 決策，再由事件
引擎依台股成交限制、現金與成本決定實際成交。

請先讀 `README.md` → `AGENTS.md` → `ARCHITECTURE.md` →
`STRATEGY_REGISTRY.md` → `RESEARCH_OPERATING_PROTOCOL.md`，並保留所有既有 PIT、價格
完整性、IS/embargo/OS、全相位、holdout 與 provenance 閘門。

完成條件不是「能跑」，而是：

1. `tests/test_strategy_position_policy_contract.py` 全綠。
2. 現有完整離線 unittest 與 `preflight.py` 全綠。
3. 關閉新 policy 時，legacy `picks_by_date` 路徑行為不變。
4. policy 規則、desired state、realized state 與無法成交原因都能由結果重建。

## 2. 名詞與責任邊界

本專案從此保留下列語意，不再把它們都叫做 portfolio：

```text
Strategy.make_signals
→ StrategyPositionPolicy
→ Event Backtest Engine
→ 單策略 trades / equity / metrics
→ 未來 Multi-Strategy PortfolioAllocator（本次不做）
```

| 層 | 責任 | 不應負責 |
|---|---|---|
| Signal | 每日 raw score、可交易母體內排名、策略硬閘門 | 現金、股數、成交價 |
| StrategyPositionPolicy | 單策略的進場、續抱、退出、風險曝險、目標權重 | 猜成交、計算 Sharpe、挑最佳參數 |
| Event Engine | 事件順序、部位、現金、股數、價格、漲跌停、處置、成本、PnL | 決定 alpha 邏輯、自己最佳化規則 |
| Evaluator/Search | 重跑候選、fold、全相位、比較 metrics | 在引擎執行中改全域 config |
| Multi-Strategy PortfolioAllocator | 未來多策略相關性、IR、sleeve 配置 | 單一股票的策略退出理由 |

`StrategyPositionPolicy` 必須在 backtest **裡被呼叫**，但不能把策略規則寫死在通用事件
引擎裡。分層是為了讓同一引擎可模擬不同 policy，不是把進出場移出損益計算。

## 3. v1 已凍結的基準語意

### 3.1 正常決策頻率

- 訊號可以每日重算與保存。
- 一般進場、排名續抱、排名退出與曝險調整只在**每週決策日**發生。
- T 日收盤後形成決策，最早只能在 T+1 的下一個有效交易時點執行。
- 假日週以該週最後一個有效交易日作為預設決策日，不得假設每五列一定等於同一星期幾。
- 正式研究仍須跑滿所有等價 weekly phase，報中位數、最小值與最差 MaxDD；live／人工
  執行使用凍結的一個 phase，不得事後挑最好星期幾。

### 3.2 每日允許發生的事

一般排名換股不在非決策日發生。下列強制或風險事件可以每日產生 `desired exit`：

- 已確認失去上市／合法交易資格或進入既有 stale/delisting 處理。
- close-confirmed hard stop。
- 已由外部、PIT 的 market-regime policy 宣告需要緊急降曝險。

產生退出意圖不等於成交。一字跌停、停牌或無合法成交價時，部位仍須留在 realized
holdings、繼續 MTM，且不可先使用尚未實現的賣出款買新股票。

### 3.3 進場、續抱與排名退出

v1 使用固定 rank buffer，不把退出門檻交給 GA：

```text
entry_rank = 10
exit_rank = 20

未持有且 rank <= 10  → 可進場
已持有且 rank <= 20  → 續抱
已持有且 rank > 20   → 每週決策日 desired exit
```

規則補充：

- `rank` 必須只在當日 PIT eligible/ranking universe 內計算。
- 非 eligible 股票不得改變 eligible 股票的 rank。
- `rank_pct` 若存在只是相對排名，不是勝率、信心或預期報酬。
- v1 不做 `replacement_gap`：滿倉且既有持股仍在 hold buffer 時，不為新候選強制換股。
  可以記錄 missed opportunity audit，但不得交易。
- v1 不做 take-profit；仍有策略理由的 winner 不因固定獲利百分比被強制賣出。
- v1 不用 MA20／MA60 作所有策略共用的 alpha exit。策略日後可明確宣告
  `thesis_break`，但不能由通用引擎暗中套用。

### 3.4 固定風險與時間保護

- `hard_stop_pct = 0.08` 作為 v1 凍結基準，不進 GA。
- hard stop 使用**收盤確認、下一交易時點嘗試退出**；不得因日內 low 曾穿價就假設
  手動投資人已在理論停損價成交。
- 跳空時使用實際可成交價，不得回填理論停損價。
- `max_hold_days = 120`，只作殭屍／資金長期占用保護，不宣稱是 alpha 的最佳持有期。
- max-hold 於可得資料確認後形成退出意圖，仍受 T+1 與可成交性限制。
- 所有退出必須保留單一主要 `reason_code`，並可另存次要觸發原因。優先序至少能區分：
  `forced_exit` → `risk_stop / regime_reduce` → `thesis_break / rank_decay` → `max_hold`。

## 4. 現金、權重與資金情境

### 4.1 兩個不可混淆的資金情境

```text
research_standard.initial_capital = 1_000_000 TWD
personal_execution.initial_capital = 500_000 TWD
```

- 初始資金屬於 immutable backtest request／execution scenario，不屬於 signal。
- 同一 policy 必須能在兩個資金情境重跑；不得靠修改全域 `config` 造成候選互相污染。
- 100 萬是研究比較基準；任何「個人可執行」主張必須另通過 50 萬情境。
- `research_fractional` 只能作 alpha 研究，不能標 execution-realistic。
- 50 萬、10 檔的正式人工情境需要支援 integer-share 的 odd-lot proxy；proxy 沒有獨立
  零股行情時必須保留 warning，不得宣稱精確成交。

### 4.2 固定資金槽與等權

v1 不用 0～1 score 直接決定部位大小：

```text
max_slots = 10
slot_weight = 0.10
weighting = equal_slot
single_name_cap = 0.15
```

- 權重以決策時 realized equity 為分母，不永遠以初始本金為分母。
- full risk-on 時每個新 slot 的目標約為當時淨值 10%。
- 合格候選少於可用 slots 時，未使用 slots 保留現金；不得把剩餘候選放大到滿倉。
- 不因 score 0.95 高於 0.90 就按比例多配資金；rank 沒有預期報酬尺度。
- v1 不做每週精確恢復等權。只有 entry、exit、regime tier 改變或單檔超過 15% cap
  才產生 resize；微小權重漂移不交易。
- 不能只因權重低於 10% 就機械式加碼下跌部位。

### 4.3 風險曝險以可用 slots 表達

market-regime 的計算公式不屬於本功能；policy 只接受已帶 PIT provenance 的 regime：

| Regime | 可用 slots | 目標曝險上限 |
|---|---:|---:|
| `risk_on` | 10 | 約 100% |
| `caution` | 5 | 約 50% |
| `risk_off` | 0 | 0%，允許全現金 |

- 從 `risk_on` 降為 `caution` 時，在決策日保留規則允許下排名最好的 5 檔，其他形成
  `regime_reduce`；不要求十檔各賣一半。
- `risk_off` 停止新進場並形成全數退出意圖，但實際現金仍以成交結果為準。
- regime 必須有 hysteresis／來源時間戳；其分類演算法另立規格，本次不得用今天資料
  回寫歷史 regime。

## 5. 最小公共契約

內部可以自由拆分，但為了讓策略、測試與 backtest 有穩定接點，至少提供：

```python
from strategies.position_policy import (
    StrategyPositionPolicy,
    StrategyPositionPolicySpec,
)

policy = StrategyPositionPolicy(StrategyPositionPolicySpec())
decision = policy.decide(
    as_of=...,
    signals=...,          # 當日完整排名 snapshot
    holdings=...,         # 唯讀 realized holdings snapshot
    equity=...,
    regime="risk_on",
    is_decision_day=True,
)
```

`signals` 至少能表達：`stock_id / rank / raw_score / eligible`。`holdings` 至少能表達：
`stock_id / weight / entry_price / close / holding_days`。實作者可使用 dataclass 或
DataFrame adapter，但上述呼叫必須可用。

`decision` 至少提供：

- 完整目標持倉與 `target_cash_weight`。
- `enter / hold / resize / exit` actions。
- 每個 action 的 `reason_code`、當下 rank／score、最早可成交時間。
- `snapshot_complete=True`；缺少此旗標時，股票未出現只能解讀為 unknown，不可自動賣。
- 規則／輸入／輸出的 deterministic fingerprint 或可進既有 rules hash 的完整內容。

事件引擎入口須能顯式接收 `signal_frame` 與 `strategy_position_policy`，並允許把
`initial_capital`、`order_size_mode`、`minimum_commission` 放在 immutable request；
為了漸進搬遷，`backtest.backtest_portfolio()` 應提供同名 keyword adapter。保留 legacy
`picks_by_date` 路徑；新路徑不得在執行中修改全域 config。

## 6. 事件引擎整合不變式

每個交易日的最小順序為：

```text
取得截至 T 可得的 signal / regime
→ policy 形成 desired actions
→ 先嘗試合法退出
→ 只把實際成交 proceeds 加入 cash
→ 再以真實 cash 嘗試 entry / resize
→ close MTM
→ 保存 desired vs realized 差異
```

以下行為 fail-closed：

- T 日收盤資訊在 T 日收盤或更早成交。
- 跌停／停牌賣不掉卻刪除部位或釋放現金。
- 無足夠現金仍買進 replacement。
- target weights 加 target cash 明顯不等於 1，或存在負 long-only 權重。
- signals snapshot 不完整卻把未列出的持股當 target 0。
- ranking universe／as-of／policy spec 缺 provenance，卻標正式證據可用。
- policy 規則改變但 strategy/rules hash 不變。

既有台股成本、tick、漲跌停、處置、整張／零股、價格完整性與下市處理必須重用，
不得在 policy 內另做一套 execution engine。

## 7. 稽核輸出

backtest 結果至少新增或等價提供：

- `decision_log`：每次 policy snapshot、actions、reason codes。
- `target_portfolio`：每個決策日完整 desired weights 與 cash。
- `order_log`：送進事件引擎的意圖及成交／未成交原因。
- realized holdings／equity curve。
- summary 中的完整 `strategy_position_policy`、capital scenario、order-size mode。
- desired vs realized 差異統計：跌停未出、停牌、現金不足、lot rounding、處置禁新倉。
- 每種 exit reason 的次數、持有期與 realized return；不得只存最後 Sharpe。

policy 關閉時不要求產生新格式 decision log，但 legacy summary 與 trades 必須保持相容。

## 8. 本次明確不做

- 不做 GA／random search，也不讓搜尋器調 exit 規則。
- 不做 signal decay；它屬於 `SignalTransformSpec`。
- 不做 5／10／20 日 IC decay 報表；它屬於 evaluator。
- 不做多策略 correlation、IR test 或 PortfolioAllocator。
- 不決定 market-regime 公式，只接收有 PIT provenance 的 regime。
- 不做券商 API、自動下單或即時盤中監控。
- 不新增第二套回測引擎或向量化正式績效。
- 不用本次重構產生的新回測數字宣稱策略 edge。

## 9. 驗收測試與執行指令

contract test：

```bash
PYTHONPATH=. .venv/bin/python tests/test_strategy_position_policy_contract.py
```

完整離線回歸：

```bash
PYTHONPATH=. .venv/bin/python -m unittest discover -s tests -p 'test_*.py'
PYTHONPATH=. .venv/bin/python preflight.py
```

交接時 contract test 預期先紅；實作者不得刪除、skip、放寬測試來取得綠燈。若實作者
認為公共契約需要調整，應先在本文件記錄理由並取得 owner 同意，再同步修改規格與測試。

## 9A. 實作者補充：三個必須寫下來的判定細節（2026-08-15）

本節由實作者補上。三處都**不改變**上面任何一條外部語意或驗收案例，只把原文
沒有寫死、但實作必須做選擇的地方記下來，避免下一個人以為是隨手寫的。

### 9A.1 hard stop 的「跨越日」判定

§3.4 只說 hard stop 是「收盤確認、下一交易時點嘗試退出」。policy 本身無狀態，
每天都會重看同一批持股，所以必須能分辨兩件事：

* **今天才跌破**停損價 → 產生新的 `risk_stop` 退出意圖。
* **早就跌破**、退出意圖還卡在成交端（一字跌停、停牌） → 不再產生一次新的
  `risk_stop`，否則同一次停損會被重複計成多個事件，exit 統計膨脹，而且會蓋掉
  真正的原因（那筆其實是「賣不掉」，不是「又跌破一次」）。

判準用台股單日 ±10% 漲跌幅上限推出來，不需要額外參數：昨天收盤若還在停損價
之上（報酬 > `-hard_stop_pct`），今天最差也只能再吃一根跌停，所以今天的報酬
必然 > `0.9 × (1 - hard_stop_pct) - 1`，這個下界對任何 `hard_stop_pct` 都落在
`-(hard_stop_pct + 10%)` 之內。反過來說：**跌幅已經超過
`hard_stop_pct + 一根跌停` 的部位，今天不可能是它的跨越日。**

這種部位只有兩種來源，用引擎提供的 `holdings.exit_pending` 分辨：

| `exit_pending` | 意義 | 行為 |
|---|---|---|
| `True` | 退出意圖已存在、只是還沒成交 | 不重複產生 `risk_stop`；動作記為 `hold` + `reason_code=stop_breached_earlier_exit_pending`（誠實標記，不偽裝成一般續抱） |
| `False` | 資料斷層（長期停牌後跳空重開）讓 policy 錯過跨越日 | 仍然產生 `risk_stop`，不因為錯過跨越日就永遠不停損 |

`exit_pending` 缺值時預設 `True`，與 §5 的 `snapshot_complete` 同一套 fail-closed
哲學：資訊不足時不自動賣。事件引擎一律顯式提供這一欄。

#### 9A.1a 這個預設值的已知限制（2026-08-15 審查後補；待 owner 決定）

審查指出：上面那個預設**方向不是 fail-closed**。它只在報酬低於
`-(hard_stop_pct + 10%)` 的區間才有作用，而在那個區間它關掉的是**停損**，不是自動
賣出。§5 的最小 `holdings` 契約（`stock_id / weight / entry_price / close /
holding_days`）又不含 `exit_pending`，所以任何照最小契約呼叫 `policy.decide()` 的
人，手上跌超過 18% 的部位一律得到 `hold`，一筆退出意圖都不會產生。

實測（預設 spec、未帶 `exit_pending`）：`-9% / -15%` → `exit/risk_stop`；
`-19% / -25% / -50%` → `hold/stop_breached_earlier_exit_pending`。

**為什麼這次沒有把預設改成 `False`**：contract test
`test_small_weight_drift_does_not_rebalance_or_average_down`（`tests/
test_strategy_position_policy_contract.py:226-238`）用的 holdings 是
`("LAGGARD", 0.07, entry 100.0, close 70.0, 20)`＝ **-30%**、且刻意不帶
`exit_pending`，並直接斷言 `action == "hold"`。把預設翻成 `False` 或改成缺欄位就
raise，這條測試（以及所有使用最小 holdings 的 contract 案例）會立刻紅。§9 明文
「實作者不得刪除、skip、放寬測試來取得綠燈」，因此這次只能：

1. 把「這是推定、不是事實」變成可稽核：缺欄位時 reason_code 改用
   `stop_breached_earlier_exit_pending_assumed`，並累計
   `policy._state["n_stop_breached_earlier_assumed"]`。事件引擎一律顯式帶欄位，
   所以正式回測路徑上這個計數恆為 0；不為 0 的結果，其停損統計不可直接採信。
2. 用 `tests/test_strategy_position_policy_engine.py::ExitPendingAssumptionTest`
   把三種情形（缺欄位 / `False` / `True`）逐一釘住，讓下一個人看得到這個缺口的
   邊界在哪，而不是靠讀程式碼推。

**待 owner 決定**：若同意「缺 `exit_pending` 時寧可重複產生 `risk_stop`，也不要
靜默不停損」，則需要**同步修改本規格與上述 contract test**（例如把 LAGGARD 的
`close` 從 70 改成 95，讓它測的仍然是權重漂移、而不是順帶關掉停損），再把
`_normalize_holdings` 的預設翻成 `False`。在那之前，policy 的 hard stop 只在
**每天**被呼叫（事件引擎正是如此）時完整成立；每週才呼叫一次的外部呼叫端，可能
在兩次呼叫之間直接跌穿 `-(hard_stop_pct + 10%)` 而錯過停損。

### 9A.2 policy 路徑的評估窗上界

external `picks_by_date` 路徑的安全預設是「截到最後一個訊號日」（AGENTS.md 陷阱
5：訊號用完後既有部位仍在 MTM，等於把 OS 的行情算進 IS）。policy 路徑**不能沿用
同一條規則**：這裡的 signal snapshot 是每週一次，把窗截到最後一個快照日會系統性
砍掉每一段 IS/OS 的最後一週，而且砍掉的正是部位還開著的那一週。

因此 policy 路徑以呼叫端顯式宣告的 `end_date` 為準——那條線本來就是
`evaluation/splits.py` 畫出來的邊界。沒給 `end_date` 時仍退回保守作法（截到最後
一個快照日並印出提示），不會無聲跑到資料末端。`summary["eval_audit"]` 因此多記
`signal_window`、`days_beyond_last_signal_snapshot` 與 `end_date_declared`，讓
「這段到底跑了多遠」可以被檢查，而不是只能相信呼叫端。

### 9A.3 決策日、regime 與快照的三道 fail-closed

* signal_frame 的快照日若不是價格資料裡的交易日 → raise。否則那個決策日會被
  靜默略過，回測照樣跑完，產出一組「訊號從來沒被執行過」的績效。
* signal_frame 同一天同一檔出現兩個 rank → raise（決策會取決於列順序）。
* 給了 `regime_by_date` 就必須逐日給滿；缺值不得當成 `risk_on` —— 缺值放行等於
  在資料缺口上偷偷恢復滿曝險，方向剛好是最該擋的那一邊。

## 10. 完成報告邊界

實作者完成後只可聲稱：

- `StrategyPositionPolicy` 行為契約與事件引擎整合已通過離線測試。
- legacy parity、PIT／T+1／cash／tradability／provenance 閘門通過。

不得聲稱：

- 新退出規則提高績效。
- 已通過 clean OOS／forward。
- 50 萬個人帳戶一定可獲利。
- odd-lot proxy 等同真實零股成交。
