# Single-Holdout Evaluation Data Boundary Spec

> 狀態：**V1 DESIGN HANDOFF — 單次 IS／embargo／OS；尚未實作**
>
> 日期：2026-08-16
>
> Owner 決策：第一版只做「研究區與 locked OS 的區段級隔離」。不做逐決策日資料沙盒、
> rolling／walk-forward analysis、vectorized fast evaluator 或 GA。

## 1. 這一版要解決什麼

資料可先下載成同一份不可變 frozen snapshot，但一般研究程序不能因此取得整份資料。
V1 只建立一條固定切割：

```text
Frozen snapshot
├── causal warmup + IS ── 可反覆研究、寫策略、調參
├── embargo             ── 不產生 fitness、不用來選策略
└── locked OS           ── 凍結策略前不可載入；owner 批准後揭露一次
```

本版刻意不再切 TRAIN／VALID，也不跑 rolling folds。所有曾參與策略發想、參數選擇或
淘汰的資料一律視為 IS；只有完全未參與研究的最後一段才可稱 OS。

## 2. V1 的正確性邊界

### 2.1 區段級資料閘門（本版要做）

Research mode 只能建立並傳入：

```text
[warmup_start, is_end]
```

- 正式計分與訊號輸出範圍只能是 `[is_start, is_end]`。
- warmup 只能來自 `is_start` 之前，不能使用 `is_end` 之後資料。
- runner 不得先載入 OS 再只裁掉輸出；audit 必須記錄策略實際收到的 input min/max。
- `is_end`、`os_start`、`os_end`、embargo、成本、phase、benchmark 與資金情境屬於
  protocol，不得由 strategy params 或 `engine_kwargs` 覆寫。

Locked-OS reveal mode 只能建立並傳入：

```text
[os_start 之前必要的 causal warmup, os_end]
```

正式計分與訊號輸出範圍只能是 `[os_start, os_end]`。IS 與 OS 的回測都必須把明確的
`start_date`／`end_date` 傳給唯一事件引擎，且所有 equity、decision、order、trade、
MTM 日期不得越界。

### 2.2 逐決策日資料沙盒（本版不做）

策略在一個獲准區段內可以一次收到完整 long panel，並向量化計算該區段的所有日期；
不會在每個決策日 T 重新建立只到 T 的 process／panel。這能保留研究速度，但代表資料
閘門只能阻止「IS 偷看 locked OS」，不能單獨阻止任意 Python 在 IS 內寫 `shift(-1)`。

因此每個可進正式評估的 Python strategy 仍須：

- 使用因果 operators／rolling 語意，不得自行讀 cache、網路或其他資料路徑。
- 通過 prefix-invariance／future-perturbation 測試：在數個固定截點重算後，截點以前的
  SignalFrame 必須不變。
- 接受 code review；未知或不可稽核的外部 SignalFrame 只能 debug，不能升級證據。

上述測試不是 runtime 的逐日回測，因此不應把 event engine 重跑數百次；但它也不是
物理安全沙盒。若日後要允許完全不受信任的任意 Python，才升級第二層隔離。

## 3. 三種 freeze 不可混用

| Freeze | 固定內容 | 目的 |
|---|---|---|
| Data snapshot | dataset、截止日、PIT universe、檔案內容 hash、readiness | 防止重抓後資料漂移 |
| Evaluation protocol | 單一 IS／embargo／OS 日期、phase、benchmark、成本、資金、期末處理 | 防止換考卷 |
| Strategy rule | strategy code/version、signal params、position/exit policy、universe rule | 防止看完 OS 回頭改規則 |

`strategy_rule_hash` 不含資料日期；同一套規則從 IS 移到 OS 時必須相同。
`evaluation_run_hash` 必須包含 snapshot、固定切割與 evaluator protocol，因此 IS run 與
OS run 可以有不同 run identity，但必須指向同一個 strategy rule hash。

## 4. 單次 OS 揭露流程

1. 在 IS 內完成研究與參數選擇。
2. 凍結 strategy code、參數、position/exit policy、universe rule 與 protocol。
3. 產生 immutable candidate／manifest 與 `strategy_rule_hash`。
4. owner 以獨立 reveal 動作授權；一般 `run(mode="os")` 不得等價於授權。
5. runner 只載入 OS 所需資料，使用同一份凍結策略函式與參數重新計算 OS 訊號。
6. 用唯一事件引擎跑滿所有等價 weekly phases，輸出 benchmark、Sharpe、Sortino、
   MaxDD、turnover、交易紀錄、equity curve 與完整 boundary audit。
7. 成功揭露後 append `evaluation/holdout.py` 揭露紀錄。相同／重疊區間再跑只能標為
   `reproduction`／`previously_seen`，不能重新宣稱 fresh OS。

「同一個訊號」是指同一個凍結的 `make_signals()` 程式與參數在 OS 資料上重算，
不是把 IS 時期已算好的 signal 數值搬到 OS。

## 5. Embargo 與區段結尾

- 使用未來 `H` 日報酬、IC label 或固定持有期作為選擇依據時，embargo 至少為 `H`
  個交易日；沿用 `evaluation/splits.py` 的 `minimum_embargo_days` fail-closed。
- 區段結尾尚未平倉的部位，V1 預設只用 `segment_end` 當日可得價格 MTM，不讀下一段
  的退出價。若日後要改成強制平倉或完整 horizon admission，必須先版本化 protocol。
- `eval_audit.days_beyond_last_pick == 0` 仍要檢查，但不能取代明確的 segment-end audit。

## 6. 必須留下的 artifacts

每個 IS／OS run 至少保存：

- snapshot identity、內容 fingerprint 與資料 readiness。
- 固定 IS／embargo／OS 日期與 protocol hash。
- strategy 實際收到的 input min/max、warmup 與 scoring window。
- signal、equity、decision、order、trade、MTM 的實際 min/max 日期。
- `strategy_rule_hash`、`evaluation_run_hash`、code fingerprint。
- 五個 weekly phases 的逐相位結果與中位／最差摘要。
- benchmark、初始／期末資金、PnL、Sharpe、Sortino、MaxDD、turnover、交易次數。
- OS 是否 locked／fresh／previously seen，以及 holdout ledger sequence。
- 所有失敗原因；失敗 run 不得靜默消失。

## 7. Fail-closed 條件

以下任一情況不得產生正常績效宣稱：

- snapshot／protocol／strategy identity 不完整或 hash 不符。
- research mode 建立、讀取或傳遞任何 `is_end` 之後資料。
- reveal 未經獨立授權，或 strategy rule 尚未凍結。
- OS run 的 strategy rule hash 與入選時不同。
- signal 或事件引擎輸出越過當前 segment end。
- embargo 小於 label／horizon 的最低需求。
- candidate 能覆寫 split、phase、benchmark、成本、資金或 end date。
- 只聚合成功 phase，或 benchmark／價格完整性／PIT universe 任一不合格。
- holdout ledger 無法驗證或 reveal 狀態不明。

## 8. V1 最小驗收測試

1. Research mode 的策略 spy 證明實際 input max date 等於 `is_end`，OS sentinel row 從未
   進入 strategy；不能只證明輸出被裁切。
2. 合法過去 warmup 可用；`is_start` 以前少一日導致 warmup 不足時 fail-closed。
3. strategy／caller 嘗試覆寫 split、phase、benchmark、cost、capital 或 end date 被拒絕。
4. IS 的 signal、equity、decision、order、trade、MTM 全部不越過 `is_end`。
5. 未凍結或未授權時要求 OS 會被拒絕，而且不建立 OS panel。
6. 第一次 reveal 使用與 IS 入選時相同的 strategy rule hash 並寫 ledger；第二次標
   `previously_seen`／`reproduction`。
7. OS 的所有輸出不越過 `os_end`，並跑滿五個 weekly phases與同口徑 benchmark。
8. 改 split、embargo、phase、benchmark、cost 或資金會改 evaluation run hash；只推進
   評估資料不得改 strategy rule hash。
9. 註冊策略在數個固定截點通過 prefix-invariance；這是離線因果契約測試，不是逐日
   runtime data sandbox。

## 9. 明確延後

- TRAIN／VALID 多層切割。
- rolling／walk-forward folds 與 regime-by-fold leaderboard。
- 每個決策日 T 的 process 級或 panel 級資料沙盒。
- vectorized Fast A0／A1 evaluator。
- grid／random／GA、campaign pool/resume 與 portfolio optimization。

這些功能未來可以疊加，但不能阻塞第一個可信的單次 IS → freeze → one-shot OS 流程。
單一 OS 的統計檢定力有限；結果必須連同樣本期間、交易數與市場 regime 揭露，不能因
一次成功就宣稱策略已普遍有效。
