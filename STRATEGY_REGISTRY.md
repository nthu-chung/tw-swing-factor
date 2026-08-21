# 策略登記表(公開版)

> 這是研究狀態帳,不是投資建議或績效排行榜。

## 這份文件公開什麼、不公開什麼

**公開**:試過哪些方向、哪些被證偽、為什麼被證偽的**機制**,以及踩到的資料與
方法論陷阱。這些是這個 repo 想留下的東西 —— 失敗紀錄比績效表有用,因為它讓
下一個人不用重做。

**不公開**:任何策略的績效數字(Sharpe、累積報酬、超額、相位分布),以及仍在
驗證中的因子定義。理由很直接:那些是 owner 的研究成果,而這個 repo 公開的是
**平台**。`strategies/` 裡留下的是方向明確不同、而且確實被證偽的假說,它們是
平台的可執行範例,**不是候選池,也不是研究全貌**。

因此:**不要從這份文件推論哪個方向有效。** 沒有被列出來,不代表沒試過。

## 狀態定義

| 狀態 | 意義 |
|---|---|
| `active hypothesis` | 規則與因果時點已定義,值得在合格資料上驗證;尚非已證明 alpha。 |
| `blocked` | 有可研究的假說,但資料或實作誠信缺口使績效不可採用。 |
| `rejected` | 在現有資料的相對比較已失敗或冗餘;除非資料／定義實質改變,不再加碼。 |
| `monitor only` | 僅做市場情境或候選觀察,不能當作正式下單策略。 |
| `superseded` | 保留做歷史對照,不能作目前決策基線。 |
| `withdrawn` | 已撤出公開 repo(見上節)。狀態與消耗過的 holdout 仍記在這裡。 |

---

## 全域證據閘門(讀任何結論之前)

這幾條是**平台層**的教訓,與任何特定策略無關,而且每一條都曾經真的產生過假結果。

### P0 — 未還原價格會直接製造假交易

歷史價格預設為未還原 `TaiwanStockPrice`,已確認國巨(2327)2025-08 的公司行動
斷點被當成約 **-73.6%** 的交易損失,並可能改變「最佳」退場規則的選擇。

閘門實作在 `backtest._assert_price_integrity`:未還原價一律 raise,
`SELF_ADJUST_PRICES` 對還原後序列掃殘留斷點,有殘留就擋。
**`data/price_integrity.py` 的斷點掃描是診斷,不是放行條件** —— 除息缺口 3~5%
落在 ±10% 漲跌停帶內,掃描結構上看不到,「0 命中」不等於「價格乾淨」。

### P1 — 證券別污染(2026-08-15 發現並修復)

`universe._is_normal_stock(stock_id, market_type)` 收了 `market_type` 卻沒用它,
實際只檢查「4 碼數字非 00 開頭」,因此**興櫃、DR、創新板一路混進候選池**。
實測凍結快照下舊規則放行 2509 檔中有 408 檔不是上市櫃普通股(興櫃 369／
創新板 28／DR 11)。

為什麼是「假 Sharpe」等級:**興櫃沒有 ±10% 漲跌停**。2026-05 單日 |ret| > 10.5%
的比例為上市 0.034%、上櫃 0.042%、興櫃 **3.872%**(約 100 倍),最大 +57.17%
—— 而動能因子找的正是那種標的,偏誤方向是系統性灌高 Sharpe。流動性也擋不住:
最大一檔興櫃日均成交值 14.75 億、全市場 ADV 排名 #188,落在候選池之內。

判定實作見 `security_type.py`,結果側辨識欄位是
`summary["universe"]["excluded_by_security_type"]`。

### P2 — 報酬口徑不一致(2026-08-15 發現並修正機制)

個股序列在 `SELF_ADJUST_PRICES=1`(預設)或 `PRICE_DATASET=TaiwanStockPriceAdj`
下是**含息**的(現金股利被還原回價格),而基準一直用 TAIEX **價格指數**(不含息)。
實測 2024-06-03~2026-06-20:每年約 **2.86 個百分點的假超額、Sharpe 差 0.113**;
2015~2026 逐年差 2.41~4.81pp,**沒有一年為負**(系統性,非雜訊)。

量級剛好落在「看起來像小 alpha」的區間,所以只印警告沒有用。判定與選擇實作見
`data/return_convention.py`,口徑不一致直接 raise。以**等權買進持有**為基準的
結論不受影響 —— 那條基準直接由個股 close 算出,必然與策略序列同口徑。

### P3 — 候選池部分修復,存活偏誤未解

正式回測改走月頻 PIT(`universes/monthly_pit.py`,M 月只用完整 M-1 曆月,含當時
在市後來下市者),但下市股價格覆蓋仍不完整,`survivorship_free` 維持 `False`。
**修好閘門不等於重新證明策略。**

### P4 — 相位運氣不是小事

同一份訊號換一個再平衡起跑日,Sharpe 可以從負值擺到正值;實測某支策略的相位
標準差幾乎等於訊號效果本身。**只報單一相位＝挑路徑。** 所有正式結論必須跑滿
所有等價相位並報中位數與最差值,不報最大值。實作是 `evaluation/phases.py`
(唯一一份,AST 守衛禁止第二份)。

### P5 — IC 高不等於回測好

IC 衡量的是整個分布的秩相關,而策略只買尾端那幾檔。實測出現過「IC 最高的
單因子在回測輸給 IC 較低者」。**因子掃描只能決定去查哪幾個,不能決定用哪個。**

### P6 — 基準是被動組合,不是零

動態 universe 等權買進持有在 IS 就有 Sharpe 1.17,另外還有加權報酬指數(含息)
可比。**贏不過被動基準的超額不叫 alpha。** 早期報告曾出現「贏過大盤」的宣稱,
其中一部分是 P2 的口徑差,一部分是拿零當基準。

---

## 總覽

### 早期研究線(S 系列,legacy 架構)

這些是 2026-06~07 的研究,多數在現行閘門下**跑不出來**(未還原價會被擋)。
保留是為了記錄走過的方向,不是為了引用。

| ID | 方向 | 狀態 | 為什麼停 |
|---|---|---|---|
| S01 | Legacy static 9-factor | `rejected` | 部分因子反向或冗餘,且 static universe 有選擇偏誤。 |
| S02 | Legacy `mom_quality` | `rejected` | 全期好看但 IS 輸給更簡單的 baseline;OS 多為普漲 beta。 |
| S03 | Static `momentum_only` | `superseded` | 是簡潔 baseline,不是可交易結論。 |
| S04 | Dynamic-universe `momentum_only` | `blocked` | 有排序訊號,但對再平衡相位與出場敏感,且資料不合格。 |
| S05 | Defensive RS／抗跌選股 | `rejected` | 與動能冗餘或反向,未改善弱市。 |
| S06 | Market filter overlay | `monitor only` | 可作低成本保險選項,未顯示增量價值。 |
| S07 | Sector rotation(不含突破) | `active hypothesis` | 有正向研究訊號;需合格 PIT 重跑。 |
| S08 | `rotation_breakout` | `blocked` | 原績效已被公司行動與 pseudo-OOS 問題降級。 |
| S09 | 即時 hybrid watchlist | `monitor only` | 產生當日研究候選,與凍結回測不具 parity。 |
| S10 | News／題材 overlay | `active hypothesis` | 只做人工覆核;尚未證明增量價值。 |
| S11 | Market／universe flow monitor | `monitor only` | 描述廣度與資金輪動,不產生交易績效宣稱。 |
| S12 | Winner DNA／break-retest | `rejected` | OS lift 約隨機,且標籤曾含未來最高點的定義瑕疵。 |
| S13 | Rank-flow transition | `rejected` | 四個因果變體都沒有跨天期的一致超額,不能單獨作 entry。 |
| S14 | Breadth-regime exposure | `active hypothesis` | 只控制曝險,不把廣度擴張直接當買點。 |
| S15 | Sector relay／族群第二棒 | `blocked` | 缺 PIT 細產業／供應鏈標籤。 |
| S16 | Shock-resilience reclaim | `active hypothesis` | 規則已預註冊;需累積足夠獨立市場 shock。 |
| S17 | Delayed fundamental confirmation | `blocked` | 需 MOPS／月營收精確時間戳與 PIT 基本面資料。 |
| S18 | Quiet sponsor compression | `active hypothesis` | 已實作 forward 原型;乾淨 warmup 未滿。 |
| S19 | 籌碼確認的風險調整動能 | `blocked` | 評估窗洩漏讓 IS 借用了 OS 的績效,舊數字作廢;重跑後相位中位低且多數相位輸給被動基準。**同時是平台的管線驗收載體**(9 份測試靠它跑通 make_signals → 五相位 → 事件引擎 → artifacts)。 |

### Golden-path 假說(H 系列)

**只列留在公開 repo 的。** 其餘已撤出(見本文開頭的公開範圍說明);撤出的策略
若消耗過 locked OS,消耗紀錄仍留在 `outputs/holdout_ledger.jsonl` 與
`evaluation/holdout.py` —— 揭露紀錄全域只有一本,搬走策略不會讓 OS 回到處女地。

| ID | 方向 | 狀態 | 為什麼被證偽 |
|---|---|---|---|
| H1 | 量能確認的突破 | `rejected` | IS 相位中位為負,且週轉率是同批最高之一 —— 訊號不穩定而成本吃得更兇。 |
| H3 | 短期反轉(**預期失敗的對照組**) | `rejected` | 如預期失敗即為正確結果 —— 它的用途是檢驗管線會不會憑空生出 alpha。2026-08-17 在關掉趨勢閘門後重測(第一次真的測到「反轉」而不是「多頭排列中的拉回」),結論方向相同。 |
| H11 | 純技術超賣反彈(RSI × 布林) | `rejected` | 反向族最差。波動正規化不但沒有增量價值,還比未正規化的絕對報酬更差 —— 它留下的是低波動陰跌股。 |
| H13 | 融資斷頭出清 | `rejected` | 「故事」那一半(融資餘額變化)是拖累,單獨用是負的;撐住結果的是跌深,而跌深已被證明是波動曝險。 |
| (H2) | — | `withdrawn`(OS 已誤耗) | 已撤出公開 repo。**locked OS 於 2026-08-16 被非授權掃過**(forward 檢驗誤用 `run_golden_path()` 未帶 `holdout_protocol`),已登記在 `evaluation/holdout.py` 的 `KNOWN_CONSUMED_HOLDOUTS`。 |
| (H4) | — | `withdrawn` | 已撤出。**locked OS 於 2026-08-16 授權揭露**,見 `outputs/holdout_ledger.jsonl`。 |
| (H5)(H6)(H7)(H8)(H9)(H10)(H12)(H14) | — | `withdrawn` | 已撤出公開 repo。其中 H14 的程式另因與既有策略高度重複而移除。 |

---

## 2026-08-16/17 — 趨勢閘門從硬編碼變成策略參數

**這是架構結論,不是策略結論**,所以留在公開這邊。

`trend_ok = (MA20 > MA60) ∧ (MA60 斜率 > 0) ∧ (收盤 > MA60)`,定義在
`factor_engine/legacy_factors.py`。它原本是 legacy 九因子選股器**自己的**規則,
在三層重構時被搬進共用層,於是曾同時住在三個地方:

1. `strategy_kit/signal_builder.py` 的 `_member_mask()` —— 硬編碼,每支假說無條件繼承
2. `backtest/event_backtest.py` 的 legacy 分支 —— 全域 `config.TREND_GUARD_ENABLED`
3. `screener.py` —— 同一個全域

三個問題:

- **它是一個沒有人宣告過的看法。** 策略作者沒選過它,參數表裡看不到它。
- **它讓相同的 `strategy_rule_hash` 可以買到不同的股票。** 全域 config 進的是
  evaluation_run 身分,不是 strategy_rule 身分 —— 那正是兩層身分制要防的事,
  而且會讓凍結與 forward 的證據等級失去意義。
- **它讓整類假說無法被測。** 「跌深反轉」按定義就是買在均線之下,大部分跌深
  標的過不了閘門,連槽位都填不滿 —— 假說被基礎設施改寫,而不是被證據否定。

處置:

- (1) 改成 per-strategy 參數 `trend_guard`,進 rules hash;預設維持 `True`
  (改預設等於一次改掉所有既有策略的定義,那該是逐支重測後的結論)。
  缺欄位卻宣告 `True` 現在是 fail-closed,不再靜默略過。
- (2) 搬進 `factor_engine.legacy_factors.legacy_selection()` —— 引擎只強制
  **市場強制你的事**(T+1、漲跌停、處置、整股、現金),「MA20 要不要在 MA60
  之上」是策略看法。`tests/test_engine_has_no_trend_opinion.py` 用 AST 擋它長回去。
- (3) 尚未處理(live 選股路徑)。

**閘門的量化影響已在本 repo 之外評估,不在這裡記錄。**

---

## 共同研究與資料紀律

1. **絕對績效需要合格價格。** 未還原價下的絕對報酬、退場優劣與 IS/OOS 選擇都不
   可視為已驗證。
2. **和被動基準比,不和零比。** 見 P6。
3. **跑滿所有等價相位。** 見 P4。
4. **IC 只用來決定去查哪幾個。** 見 P5。
5. **OS 一次性。** 參數選擇只能看 IS;OS 一旦被用來選權重或規則,就只能標為
   pseudo-OOS。揭露走 single-holdout 協議,一次揭露並進 append-only 揭露紀錄。
6. **搬走策略不會洗掉 holdout 紀錄。** 揭露紀錄全域只有一本(設計理由見
   `evaluation/holdout.py`),否則換個 repo 就能把「這段看過了」洗掉。

---

## 關聯檔案

- 平台工作規則:[AGENTS.md](./AGENTS.md)｜模組地圖:[ARCHITECTURE.md](./ARCHITECTURE.md)
- 資料源與資料本身的邊界:[DATA_SOURCES.md](./DATA_SOURCES.md)
- 研究作業協議:[RESEARCH_OPERATING_PROTOCOL.md](./RESEARCH_OPERATING_PROTOCOL.md)
- 台股市場規則:[TAIWAN_MARKET_RULES.md](./TAIWAN_MARKET_RULES.md)
- holdout 揭露紀錄:`outputs/holdout_ledger.jsonl`｜實作 `evaluation/holdout.py`
