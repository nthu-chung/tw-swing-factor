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

### 4.3a regime 的 provenance 物件（2026-08-15，**owner 驗收後補**）

**原缺陷（重現）**：上面三條的執行方式從來沒有寫下來，實作上傳進
`backtest_portfolio(regime_by_date=...)` 與 `policy.decide(regime=...)` 的都只是
`"risk_on"` 這種**裸字串**，而 summary 的 `regime_pit_provenance` 是
`bool(regime_by_date)` —— 「有傳東西」被當成「有 PIT provenance」。於是拿今天的
大盤走勢回頭標歷史每一天的 regime，回測照跑、`formal_evidence_eligible` 照樣是
`True`，結果還蓋上一個 regime 有 PIT 出處的章。

**新語意**（不含 regime 判定演算法，那仍屬另一份規格）：

```python
from strategies.position_policy import RegimeProvenance, RegimeState

regime = RegimeState(
    label="caution",
    provenance=RegimeProvenance(
        source="regime.taiex_ma200_v1",   # 誰算的，事後要能找回同一份計算
        as_of=pd.Timestamp("2026-03-06"),  # 這個標籤用到的資料截止時間
        hysteresis="confirm_2_days",       # 遲滯設定；沒有遲滯的 regime 只是雜訊
    ),
)
```

- `policy.decide(regime=...)` 同時接受裸字串與 `RegimeState`。裸字串**不被拒絕**
  （v1 還沒有 regime 判定規格，拒絕等於讓整條路徑不能用），但一定是
  `decision.regime_verified=False`。
- `provenance.as_of > as_of` → fail-closed raise：那是用未來資料回寫歷史 regime。
- `RegimeProvenance` 的 `source` / `hysteresis` 為空白字串 → raise。空白來源等於
  沒有來源。
- regime 的出處進 `decision.fingerprint`：同一天同一個 label，一個有 PIT
  provenance、一個沒有，是兩份不同可信度的決策，不該有相同指紋。

引擎與 summary：

| 情況 | `regime_evidence` | `regime_pit_provenance` | `formal_evidence_eligible` |
|---|---|---|---|
| 完全不給 `regime_by_date` | `none_constant_risk_on` | `False` | 不受影響 |
| 每一天都帶 `RegimeState(provenance=...)` | `verified` | `True` | 不受影響 |
| 任何一天是裸字串 | `unverified` | `False` | **降級** |

「完全不給」不降級是刻意的：那是「不做 regime overlay」的宣告，沒有用到任何外部
資料，也就沒有東西需要 PIT 出處。逐日 `decision_log` 另存 `regime_verified`，
否則事後分不出「有依據的 risk_off」與「有人手打的 risk_off」。

回歸測試在 `tests/test_strategy_position_policy_regime.py`。

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
- `snapshot_complete`：**誠實反映實際採用的完整性判定**，缺省為 `False`（見 §9B.1）。
  只有 signal frame 自己宣告 `snapshot_complete=True` 才算完整排名母體；缺少此旗標時，
  股票未出現只能解讀為 unknown，不可自動賣。
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

`exit_pending` 缺值時預設 **`False`**（2026-08-15 owner 驗收後由 `True` 改回，
理由見 §9A.1a）。事件引擎一律顯式提供這一欄。

#### 9A.1a 缺值預設為什麼是 `False`（2026-08-15，**owner 已同意**）

第一版把缺值預設寫成 `True`，理由是「與 §5 的 `snapshot_complete` 同一套
fail-closed 哲學」。那個類比是錯的：**兩個旗標的安全方向不同**。

| 旗標 | 缺值取 | 缺值時的行為 | 方向 |
|---|---|---|---|
| `snapshot_complete` | `False` | 不因「沒看到這一列」而賣出 | 保守（不亂賣） |
| `exit_pending`（舊） | `True` | 不再產生 `risk_stop` | **不保守（漏停損）** |

fail-closed 的定義是「資訊不足時不得放過風險控制」，不是「資訊不足時一律不動作」。
`exit_pending=True` 的語意是「退出意圖已經送過了」——把這件事**假設成真**，等於在
沒有證據的情況下宣稱風控已經執行過。

實測（預設 spec、§5 的最小 `holdings`、未帶 `exit_pending`）：

```text
舊行為  -9% / -15% → exit/risk_stop
        -19% / -25% / -50% → hold/stop_breached_earlier_exit_pending(_assumed)
新行為  以上全部 → exit/risk_stop
```

也就是說，任何照 §5 最小契約呼叫 `policy.decide()` 的人，手上跌超過
`hard_stop_pct + 一根跌停`（預設 18%）的部位，在舊行為下一筆退出意圖都不會產生。

新預設的代價是**重複**：呼叫端沒給 `exit_pending` 時，深跌部位可能每天都重新產生
一次 `risk_stop`。這是刻意的取捨——重複的退出意圖看得見（`policy.state()` 的
`n_stop_repeated_unknown_exit_pending` 會計數，並隨 summary 的
`strategy_position_policy.policy_state_delta` 回傳），漏掉的停損在任何輸出裡都
看不見。事件引擎一律顯式帶欄位，所以正式回測路徑上這個計數恆為 0。

同步調整的測試資料（**只改輸入，不改斷言**）：contract test
`test_small_weight_drift_does_not_rebalance_or_average_down` 的 holdings 原本是
`("LAGGARD", 0.07, entry 100.0, close 70.0, 20)`＝ -30%。那條測試要測的是
「權重掉到 7% 不得機械式攤平」，close 70 卻同時穿過 hard stop，於是它在舊實作下
反過來把「缺資訊時不停損」釘成了契約。close 改為 `95.0`（-5%，在停損之上），
權重仍是 0.07，測的東西不變。三種情形（缺欄位 / `False` / `True`）由
`tests/test_strategy_position_policy_engine.py::ExitPendingDefaultTest` 逐條釘住。

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

## 9B. 契約澄清：訊號快照的完整性語意（2026-08-15，**owner 已同意**）

本節記錄一次**外部語意的變更**（不是實作細節），依 §9 的規定先寫下理由並取得
owner 同意。獨立審查者用實際重現找出下列兩個缺陷，兩者是同一個病灶的兩面：
policy 把「我今天沒看到這一列」當成「這檔已經掉出排名母體」的證據。

### 9B.1 `snapshot_complete` 缺省值改為 `False`

**原缺陷（重現）**：`strategies/position_policy.py` 舊版 `_normalize_signals()` 以
`snapshot_complete = True` 起始，只有 signal frame 帶了 `snapshot_complete` 欄才會
改變。於是「持有 B、今天的訊號只有 A、frame 沒有完整性旗標」會讓 B 被判
`exit / not_ranked` 賣掉。§5 要求這種情況視為 unknown、不得自動賣出；舊行為會直接
改變換股次數、交易成本與績效。`frame.empty` 與「截至 as_of 沒有任何有效快照」時
舊版直接 `return (..., True)`，是同一個 bug 的另外兩個出口——後者等於用一張空表
把整個組合清空。

**新語意**：

1. 缺少 `snapshot_complete` 旗標 = 完整性**未知** = `False`。
2. 空 DataFrame、或截至 as_of 找不到有效快照時，若沒有獨立於資料列之外的完整性
   metadata，一律 `snapshot_complete=False`。v1 **沒有**這種獨立 metadata 通道
   （不採用 `DataFrame.attrs` 之類會在 groupby／copy 之間靜默失傳的管道），因此
   這兩種情況恆為 `False`。
3. 完整性只取自**實際被採用的那一個快照日**的列（見 §9B.2）；較舊快照宣告完整
   不能替最新快照背書。任一列為 `False`／NaN 則整個快照視為不完整。
4. `decision.snapshot_complete` 必須誠實反映實際採用的判定。owner 明確**不採用**
   「decision 永遠回報 `True`、內部卻不賣」的方案：那會讓稽核紀錄與實際行為
   不一致，事後無法由 decision_log 重建當天到底用了什麼語意。

**為什麼在 contract test 的 `_signals()` fixture 加旗標不等於放寬測試**：該 fixture
的每一個案例本來就在描述「當日完整排名母體」（所有斷言都預設沒列出的股票是真的
不在母體裡），加上 `snapshot_complete=True` 只是把這個一直存在的前提寫出來，讓輸入
自己說清楚。斷言一條都沒有刪除或放寬——`test_decision_is_complete_and_auditable`
的 `assertTrue(d.snapshot_complete)` 原封不動保留，而「缺旗標時必須是 `False`」
另由 `tests/test_strategy_position_policy_snapshot.py` 正面釘住。換句話說：改的是
**輸入的誠實度**，不是**輸出的驗收標準**。

### 9B.2 多日 signals 只採用截至 as_of 的最新一個快照

**原缺陷（重現）**：舊版對多日 signals 做
`sort_values("_asof").drop_duplicates(subset=["stock_id"], keep="last")`，取的是
**每檔各自的最新列**，不是「截至 as_of 的最新那一個快照日的全部列」。於是一檔在
最新快照裡已經整列消失（掉出榜外）的股票，會沿用它舊快照的 rank 繼續被當成今天的
有效訊號：既可能被當成 top-10 買進，也會因為「還在名單裡」而躲掉 `not_ranked`，
而且輸出裡完全看不出那個 rank 是舊的。

**新語意**：選出截至 as_of 的**最新一個快照日**，只用那一天的列；跨快照日合併
rank 一律禁止。同一個快照日出現重複 `stock_id` 時 fail-closed raise（舊版靠
`drop_duplicates` 靜默留下最後一列，決策取決於列順序，還會讓同一檔佔掉兩個資金槽；
與 §9A.3 對引擎的同一條 fail-closed 一致）。`date <= as_of` 的過濾維持不變，
`test_appending_future_signals_does_not_change_past_decision` 仍然成立。

### 9B.3 對現有路徑的影響

- 事件引擎每天只餵**單一快照日**的列給 `policy.decide()`，所以 §9B.2 不改變引擎
  路徑的排名結果。
- 但 §9B.1 會改變引擎行為：`signal_frame` 若不宣告 `snapshot_complete`，所有決策日
  都算不完整，`not_ranked` 退出不會發生，且
  `summary["strategy_position_policy"]["snapshot_complete_all_days"]` 為 `False`。
  **要讓 `not_ranked` 生效，signal frame 必須自己宣告完整性。** 這個方向是刻意的：
  賣錯股票要有明確依據，缺資訊時不動作。

## 9C. 證券別閘門與每次 request 的排除統計（2026-08-15，owner 驗收後補）

### 9C.1 `signal_frame` 進引擎前必須通過證券別閘門

普通股白名單原本只裝在 `backtest._prepare_panel()` 裡，而 policy 路徑與
`picks_by_date` 路徑**正好都不經過 panel**。實測：把已知 DR 代號 `9103` 放進外部
picks，回測照樣建立持倉（`summary["open_positions_end"] == 1`），而
`summary["universe"]["excluded_by_security_type"]` 是 `{"total": 0, ...}` ——
結果反而背書「這份池沒有洩漏」。

現在兩條外部訊號路徑都在進引擎前過同一份 `security_type.filter_ids`：

- 已知的非普通股（興櫃 / DR / 創新板 / ETF / ETN / 受益證券 / 特別股）→ 整列從
  `signal_frame`（或該日的 picks）剔除並記進排除紀錄簿；
- 證券別**判不出來**（不在 TaiwanStockInfo、欄位空白、沒見過的產業別）→
  `SecurityTypeError` fail-closed。缺資訊不得預設放行。
- `signal_frame` 被擋光 → `ValueError`，不得靜默跑出一份沒有標的的績效。

剔除整列而不是把 `eligible` 標成 `False`：`eligible=False` 的語意是「訊號那端說
今天不合格」，和「這根本不是上市櫃普通股」不同，混在一起之後 summary 分不出被擋
掉的是哪一種。

### 9C.2 排除統計是 request 級，不是 process 級

統計原本存在 module 級全域 list，且 `reset_exclusion_log()` 明文假設「一個
process = 一次研究執行」。那對「同一 process 連續跑兩次回測」（第二次的 summary
含第一次的排除數，已重現）與「平行 GA 搜尋」都是錯的。

現在 `backtest_portfolio` 每次 request 開一本 `security_type.ExclusionCollector`
（隨 request 建立、隨 summary 回傳），與 §4.1「資金情境是 immutable request，
不寫回全域 config」同一條原則。呼叫端要把**池建構**的排除也算進同一份結果時，
用 `security_type.exclusion_scope()` 把兩段包起來，或顯式傳
`exclusion_collector=`。`security_type.exclusion_summary()`（process 全域）降級成
純觀察用途，不得再當 summary 數字的來源。

## 10. 完成報告邊界

實作者完成後只可聲稱：

- `StrategyPositionPolicy` 行為契約與事件引擎整合已通過離線測試。
- legacy parity、PIT／T+1／cash／tradability／provenance 閘門通過。

不得聲稱：

- 新退出規則提高績效。
- 已通過 clean OOS／forward。
- 50 萬個人帳戶一定可獲利。
- odd-lot proxy 等同真實零股成交。
