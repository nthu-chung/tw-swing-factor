# 策略記憶台帳

> 最後全面盤點：2026-07-23（S19 於 2026-08 單獨更新）。
> 這是研究狀態帳，不是投資建議或績效排行榜。
>
> **全域證據閘門（P0）**：目前歷史價格預設為未還原 `TaiwanStockPrice`，已確認
> 國巨（2327）2025-08 的公司行動斷點被當成約 -73.6% 的交易損失。因此所有用這份
> 價格產生的回測絕對績效、退場優劣與 IS/OOS 選擇，皆不得視為已驗證；必須先以
> 公司行動一致化的還原價與 PIT（含下市）universe 重跑。
>
> 閘門實作在 **`backtest._assert_price_integrity`**：未還原價一律 raise，
> `SELF_ADJUST_PRICES` 對還原後序列掃殘留斷點，有殘留就擋。
> **`price_integrity.py` 的斷點掃描是診斷，不是放行條件**——除息缺口 3~5% 落在
> ±10% 漲跌停帶內，掃描結構上看不到，「0 命中」不等於「價格乾淨」。
>
> **候選池部分修復（2026-08）**：正式回測改走月頻 PIT
> （`universes/monthly_pit.py`，M 月只用完整 M-1 曆月，含當時在市後來下市者），
> 但下市股價格覆蓋仍不完整，`survivorship_free` 維持 `False`。
> **修好閘門不等於重新證明策略**：本表所有既有績效在重跑前一律維持原證據等級。

## 狀態定義

| 狀態 | 意義 |
|---|---|
| `active hypothesis` | 規則與因果時點已定義，值得在合格資料上驗證；尚非已證明 alpha。 |
| `blocked` | 有可研究的假說，但資料或實作誠信缺口使績效不可採用。 |
| `rejected` | 在現有資料的相對比較已失敗或冗餘；除非資料／定義實質改變，不再加碼。 |
| `monitor only` | 僅做市場情境或候選觀察，不能當作正式下單策略。 |
| `superseded` | 保留做歷史對照，不能作目前決策基線。 |

## 總覽

| ID | 策略／研究變體 | 狀態 | 目前最重要判斷 |
|---|---|---|---|
| S01 | Legacy static 9-factor | `rejected` | 買弱、拉回與部分法人因子拖累，且 static universe 有偏誤。 |
| S02 | Legacy `mom_quality` | `rejected` | 全期漂亮但 IS 輸純動能；OS 多為普漲 beta。 |
| S03 | Static `momentum_only` | `superseded` | 是簡潔 baseline，不是可交易結論。 |
| S04 | Dynamic-universe `momentum_only` | `blocked` | 動能有排序訊號，但投組對再平衡相位／出場敏感，且資料不合格。 |
| S05 | Defensive RS／抗跌選股 | `rejected` | 與動能冗餘或反向，未改善弱市。 |
| S06 | Market filter overlay | `monitor only` | MA200 可作低成本保險選項，但未顯示增量價值。 |
| S07 | Sector rotation（不含突破） | `active hypothesis` | 族群動能、法人廣度有正向研究訊號；需合格 PIT 重跑。 |
| S08 | `rotation_breakout` | `blocked` | 最接近使用者流程；原績效已被公司行動與 pseudo-OOS 問題降級。 |
| S09 | 即時 hybrid watchlist | `monitor only` | 可產生當日研究候選，與凍結回測不具 parity。 |
| S10 | News／題材 overlay | `active hypothesis` | 目前只做人工覆核；尚未證明有增量 alpha。 |
| S11 | Market／universe flow monitor | `monitor only` | 描述廣度與資金輪動，不產生交易績效宣稱。 |
| S12 | Winner DNA／break-retest | `rejected` | OS lift 約隨機，且標籤曾含未來最高點的定義瑕疵。 |
| S13 | Rank-flow transition | `rejected` | 已實作四個因果變體；62 日探索窗沒有一致超額，不能單獨作 entry。 |
| S14 | Breadth-regime exposure | `active hypothesis` | 只控制曝險，不再把廣度擴張直接當買點。 |
| S15 | Sector relay／族群第二棒 | `blocked` | 符合題材擴散機制，但缺 PIT 細產業／供應鏈標籤。 |
| S16 | Shock-resilience reclaim | `active hypothesis` | 規則已預註冊；需累積足夠獨立市場 shock。 |
| S17 | Delayed fundamental confirmation | `blocked` | 需 MOPS／月營收精確時間戳與 PIT 基本面資料。 |
| S18 | Quiet sponsor compression | `active hypothesis` | 已實作 forward 原型；目前僅62日，120日乾淨 warmup 未滿、零訊號。 |
| S19 | 籌碼確認的風險調整動能（CRM） | `blocked` | **IS 20 相位中位僅 0.520、只有 3/20 勝過被動基準 1.13**。相位標準差 0.509 ≈ 訊號效果本身，執行時點比訊號更決定結果。先前報過的 1.607／1.938 是評估窗洩漏的產物，**已作廢**。 |

## 策略明細

### S01 — Legacy static 9-factor multifactor

| 欄位 | 登記 |
|---|---|
| 核心規則 | `momentum`、法人中長期／逢低買、融資券、均線排列／糾結、布林拉回、量縮等 9 因子加權；趨勢門檻後排序。 |
| 資料與 universe | FinMind 價格、法人、融資券；期末流動性 top100 的 static universe。 |
| 進出場 | 5 日再平衡、最多 5 檔；T+1 開盤；預設 trend exit 為跌破 MA20、-8% 停損、最長 120 日。固定模式另有 20 日、+25%／-8%。 |
| 證據等級 | 低；舊報告的絕對績效受 static selection、兩年偏多頭與未還原價影響。 |
| 已知失效／偏誤 | `ma_squeeze` 反向；`vol_dryup`、`bb_pullback`、`inst_long` 無效或冗餘；因子越多越易貼合樣本。 |
| 下一個可證偽測試 | 用 PIT 全市場與還原價，預先鎖權重，和 S03/S08 做 rolling walk-forward；若無穩定增量，永久封存。 |

### S02 — Legacy `mom_quality`

| 欄位 | 登記 |
|---|---|
| 核心規則 | 權重：動能 50%、均線排列 20%、融資券健康度 30%。 |
| 資料與 universe | 同 S01。 |
| 進出場 | 同 S01 的 5 日再平衡、T+1、trend exit。 |
| 證據等級 | 已否決的相對結論；不應引用全期 Sharpe 1.53。 |
| 已知失效／偏誤 | IS Sharpe 約 0.41–1.33，低於 static pure momentum 1.50；OS 高績效與全池普漲高度一致，且資料快照漂移曾讓結果大幅翻動。 |
| 下一個可證偽測試 | 無優先權。只有在更長、不同 regime 的 PIT 資料中，預先指定權重仍明顯勝 S03 才重啟。 |

### S03 — Static `momentum_only` baseline

| 欄位 | 登記 |
|---|---|
| 核心規則 | 單一 `momentum` 分數：60 日報酬與接近 60 日高點；趨勢門檻後排名。 |
| 資料與 universe | current top100 static universe，FinMind 原始價及籌碼；snapshot 2026-06-22。 |
| 進出場 | 5 日再平衡、最多 5 檔、T+1 開盤；MA20 trend exit、-8% 硬停損、120 日上限。 |
| 證據等級 | 僅為最少參數的 historical baseline。舊 IS Sharpe 1.50／年化 +40.5% 不可當現行證據。 |
| 已知失效／偏誤 | static current-top100 選擇偏誤；未還原價；樣本約兩年且偏多；動能會在反轉期集體承壓。 |
| 下一個可證偽測試 | 在完整 PIT universe、還原價上重跑，並以不同再平衡相位、成本、持倉數檢驗穩健性。 |

### S04 — Dynamic-universe `momentum_only`

| 欄位 | 登記 |
|---|---|
| 核心規則 | 每日只在截至當日近 20 日成交值／量排名的 top100 選股，再以 S03 動能排序。 |
| 資料與 universe | current top300 bootstrap pool 中的 daily top100；非歷史完整上市櫃池，`survivorship_free=False`。 |
| 進出場 | 同 S03。 |
| 證據等級 | 訊號層可研究：top-5 事件研究曾見 20 日超額約 +2.84pp、重疊校正 t=2.70；投組層未驗證。 |
| 已知失效／偏誤 | 單一路徑為 -4.4%，但五個等價相位差異很大；顯示實作／出場敏感。原始價與 candidate-pool survivorship 仍阻斷結論。 |
| 下一個可證偽測試 | 固定規則後，在 PIT 全池上做多相位、walk-forward 及 block-bootstrap；若相位分布無正向中位超額，否決。 |

### S05 — Defensive RS／抗跌選股

| 欄位 | 登記 |
|---|---|
| 核心規則 | 在純動能中加入 60 日相對大盤報酬、下行 beta／抗跌、下跌日相對報酬。 |
| 資料與 universe | FinMind 個股與 TAIEX；static top100。 |
| 進出場 | 同 S03。 |
| 證據等級 | 已否決的相對結論。 |
| 已知失效／偏誤 | RS 與動能相關約 +0.83、未顯著；抗跌因子在現有樣本反向或未改善股災窗。現有 trend exit 已承擔部分下檔保護；弱市僅一個 V 型反彈 regime。 |
| 下一個可證偽測試 | 除非取得涵蓋多次非 V 型空頭的長期 PIT 資料，否則不重啟。 |

### S06 — Market filter overlay

| 欄位 | 登記 |
|---|---|
| 核心規則 | TAIEX 低於 MA200／MA60／MA20，或 20 日年化波動逾門檻時，封鎖新進場並於 T+1 開盤降至目標曝險。預設選項為 MA200、全空手。 |
| 資料與 universe | TAIEX 加上底層 S03；開關預設 `False`。 |
| 進出場 | risk-off 時先出較弱分數持倉；risk-on 恢復入場。 |
| 證據等級 | 僅風控選項，非 alpha。 |
| 已知失效／偏誤 | 2025 壓力段中，底層 trend exit 已多數退現金；MA200 兩年只多觸發少數出場，MA60／MA20／vol 有明顯 whipsaw 成本。static 基線亦失效。 |
| 下一個可證偽測試 | 在合格資料的多次熊市，預先比較回撤、尾部損失與機會成本；若無改善只保留人工風險開關。 |

### S07 — Sector rotation（族群＋法人，未要求突破）

| 欄位 | 登記 |
|---|---|
| 核心規則 | 以粗產業每日中位相對強度、動能、近高點廣度、法人 6 日正向廣度排名，保留至少 5 檔的前三強族群；個股仍須上升趨勢與動能。 |
| 資料與 universe | dynamic membership 後的 current top300 bootstrap；目前只有當前粗產業分類，不能替代 PIT 題材分類。 |
| 進出場 | `rotation_research.py` 可比較 MA10、MA20、hold20、hold40；訊號收盤確認，T+1 開盤。 |
| 證據等級 | `active hypothesis`；族群掃描曾見正向延續 IC，但歷史 static 候選池報告已降級。 |
| 已知失效／偏誤 | 產業標籤過粗，記憶體／被動元件等細題材不可乾淨回推；原始價、候選池 survivorship、已知 2026 贏家造成研究者污染。**（2026-08-15 追加）**S07 與 S08 共用 `rotation_research` 的 panel，同樣受上述稀疏 panel rolling 缺陷影響（見 S08 欄）；證據等級維持 `active hypothesis`，不因閘門修好而升級。 |
| 下一個可證偽測試 | 使用 PIT 細產業／產品鏈分類與還原價，做「無族群 vs 族群」消融；檢驗不同年度、不同 top-N 下族群訊號是否仍有增量。 |

### S08 — `rotation_breakout`（族群＋法人＋價量突破）

| 欄位 | 登記 |
|---|---|
| 核心規則 | S07 的前三強族群後，個股必須：上升趨勢、法人 6 日淨買 > 0、收盤突破前 20 日高點、量 ≥ 前 20 日均量 1.2 倍；依綜合訊號分數排序。 |
| 資料與 universe | 同 S07；主張的研究池方向為 dynamic top200，以保留二線接棒股。 |
| 進出場 | 收盤訊號、T+1 開盤買；-8% 硬停損；MA10／MA20 退出皆為收盤確認、下一開盤執行；hold20／hold40 為固定持有。 |
| 證據等級 | 規則可表達使用者操作流程，但**績效 blocked**。原始資料中 IS 選出的 hold20 看似最佳，不可保留此選擇。 |
| 已知失效／偏誤 | 國巨公司行動錯誤會污染停損與出場選擇；pseudo-OOS 已知 2026 題材；current top300 候選池有 survivorship；20 日持有未必能抱住長趨勢。**（2026-08-15 追加）**修正前 `rotation_research` 在「只留動態 universe 成員日」的稀疏 panel 上算 `breakout_20`／`breakout_volume_ratio`／`positive_day_share_20`，20 列視窗會橫跨 60+ 個日曆日（獨立模擬:訊號翻轉約 3%、命中率相對灌水約 +9.6%）→ 修正前的任何 rotation 數字一律不可引用；另外該檔的投組迴圈是 research-only 的第二套引擎（無漲停鎖／處置／整張／成本模型），正式績效必須把 picks 餵進 `backtest.backtest_portfolio`。修好閘門不等於重新證明策略，證據等級維持 `blocked`。 |
| 下一個可證偽測試 | 先通過 price-integrity gate，再預註冊 breakout／量比／exit 候選與 selection procedure；在未見期間做 rolling walk-forward，並與 S03、S07 做成本後比較。 |

### S09 — 即時 hybrid watchlist

| 欄位 | 登記 |
|---|---|
| 核心規則 | `current_watchlist.py` 以 TWSE 當日官方價格／法人資料，篩流動性與站上 MA20，將 5 日／20 日報酬、近 20 日高、均線距離、量比、近 5 日法人量化後加權排序。 |
| 資料與 universe | TWSE 上市股近約 70 日公開日資料；沒有 TPEx、細題材 PIT、還原價歷史或凍結回測資料的完整欄位。 |
| 進出場 | 無內建、已驗證的正式交易規則；只能產生「立即觀察／等待突破」候選。 |
| 證據等級 | `monitor only`。 |
| 已知失效／偏誤 | 不是 `rotation_research.py` 的 production parity；只看短窗、五日法人、上市市場，且先前人工新聞覆核改變了最終十檔。 |
| 下一個可證偽測試 | 每日 append-only 保存候選、次日及 5／20 日前瞻報酬，與同日可交易 universe 基準比較；未達門檻不得改稱策略。 |

### S10 — News／題材／營收 overlay

| 欄位 | 登記 |
|---|---|
| 核心規則 | 先由 S08 或 S09 找候選，再用月營收、MOPS、新聞與產業鏈敘事確認催化劑，並檢查族群同步性；不是「新聞一出即買」。 |
| 資料與 universe | 目前為人工查核的公開新聞、公司公告與營收；尚無歷史全文、實際可得時間、細產業 PIT 標籤。 |
| 進出場 | 題材只作曝險／合理性覆核；真正進場仍應由可定義的價量突破，退出沿用底層策略。 |
| 證據等級 | `active hypothesis`，尚未證實增量 alpha。 |
| 已知失效／偏誤 | 容易把事後已知贏家故事化；新聞發佈時點、修訂、存活與人工判讀皆可能 leak。 |
| 下一個可證偽測試 | 做預先定義 A/B/C：A 純價量、B 族群＋法人＋價量、C B＋時間戳新聞／營收特徵；rolling walk-forward、交叉驗證與多重檢定控制。若 C 無穩定增量，保留為解釋層。 |

### S11 — Market／universe flow monitor

| 欄位 | 登記 |
|---|---|
| 核心規則 | `market_flow_monitor.py` 先以截至 T 的 ADV20 建每日流動性前300，再在池內以 5／20 日動能、成交值、法人淨買的橫斷面 z-score 合成 `flow_score`，輸出 top-N、rank 變動、進出名單、churn 與全市場／動態池廣度。 |
| 資料與 universe | TWSE 官方日資料；可輸入已有長格式價量／法人資料做因果計算。 |
| 進出場 | 無；是 regime／輪動觀測器。 |
| 證據等級 | `monitor only`。 |
| 已知失效／偏誤 | score 權重未以正式前瞻績效驗證；高 churn 可能代表健康輪動，也可能代表噪音；未覆蓋 TPEx。價格斷點會先 quarantine 21 個該股觀察日並重算整個橫斷面，不能把此安全措施誤稱為還原價。 |
| 下一個可證偽測試 | 將每日輸出 immutable 保存，測 breadth／churn／rank entrant 對未來市場、族群與候選成功率的條件預測，並和隨機／單一動能基準比較。 |

### S12 — Winner DNA／break-retest

| 欄位 | 登記 |
|---|---|
| 核心規則 | 尋找未來 60 日大漲前的 bias、布林位置、法人與 breakout-retest 特徵；曾以「未來 60 日最高漲幅 ≥30%」標記飆漲。 |
| 資料與 universe | top300、約兩年 FinMind 資料。 |
| 進出場 | 尚無完整、成本後的交易執行策略。 |
| 證據等級 | `rejected`。OS rule lift 約 1.04，接近隨機。 |
| 已知失效／偏誤 | 標籤使用未來期間最高點而非可持有報酬，基準率偏高；資料期短且大多頭；break-retest 的 1.06 lift 不足以形成交易結論。 |
| 下一個可證偽測試 | 若重啟，改用固定持有期實現報酬／可交易進出場、提高門檻、做分年與成本後 OOS；否則不再擴充特徵。 |

### S13 — Rank-flow transition

| 欄位 | 登記 |
|---|---|
| 核心規則 | `rank_flow_strategy.py` 實作 `confirmed_entrant`、`persistent_leader`、`breadth_expansion` 與 `rank_flow_persistence`；只用 T 與過去排名、法人、MA20、廣度及動態池狀態。 |
| 資料與 universe | 2026-04-24 至 2026-07-23 的 TWSE 日資料；每日 ADV20 前300、價格斷點 causal quarantine；沒有 TPEx。 |
| 進出場 | T 日收盤確認、T+1 開盤；正向跳空 >5% 不追；以 5／10／20 日事件研究探索，同股票只在同一假說內去重。 |
| 證據等級 | `rejected` 作為 standalone entry。四個變體的 signal-date cohort 超額沒有跨 horizon 一致為正；詳細數字見 `outputs/RANK_FLOW_EXPERIMENT_REVIEW.md`。 |
| 已知失效／偏誤 | 僅 62 日 exploratory IS、horizon 重疊、無投組成本；rank 跳升常是已發動後的追價，不等於可交易 alpha。 |
| 下一個可證偽測試 | 不再微調同一短窗。保留固定版本做 forward ledger；rank persistence 只可作 S15／S18 的確認因子，增量必須另測。 |

### S14–S18 — 新假說設計隊列

| ID | 機制 | 固定的 prove／kill 重點 |
|---|---|---|
| S14 Breadth-regime | 以 MA20／法人廣度控制 100%／50%／20% 曝險，不換底層選股。 | OOS 回撤與 ES 改善至少20%，CAGR 犧牲不逾25%；否則否決。 |
| S15 Sector relay | 第一棒仍強且族群 breadth／法人擴散後，交易中段第二棒的 prior-10d high 突破。 | 必須勝同族群 matched laggards；離開2026熱門題材仍有效。 |
| S16 Shock reclaim | 大盤急跌時抗跌、法人承接，三日內帶量收復 shock 前收盤。 | 至少8–10次獨立 shock，且勝 beta／動能 matched control。 |
| S17 Delayed fundamental | PIT 營收意外後等待1–5日價量法人確認，不在新聞當下追。 | C 必須穩定勝純價量 A 與未等確認 B；timestamp 缺漏即阻擋。 |
| S18 Quiet sponsor | `quiet_sponsor_strategy.py`：法人持續吸收、波動壓縮、價格尚未延伸，prior-10d high 帶量突破；120日乾淨 ATR 基準未滿不出訊號。 | 勝一般 breakout，且移除月底／指數調整與最大贏家後仍成立。 |

完整進出規格、資料需求與失效條件見 `NEW_STRATEGY_EXPERIMENTS.md`。

## 共同研究與資料紀律

1. 每次新增策略先登記在本檔：假說、可得時間、資料欄位、進出場、失效條件，再跑回測。
2. 不得以當前 top-N 候選池回套歷史宣稱 survival-free alpha；歷史上市、下市、轉板與指數／產業成分均須 PIT。
3. 價格必須可處理除權息、分割、減資等公司行動；任何異常斷點先阻擋、後解釋。
4. 新聞、營收、公告只能使用在當時真正可得的時間戳；人工閱讀不得滲入 OOS 的特徵或參數決定。
5. 所有進場均以 T 日收盤可得訊號、T+1 可成交價格為最低標準；交易成本、滑價、停損跳空必須記錄。
6. 結果至少報告 IS、embargo、未見 OOS、不同相位／持倉／成本敏感度；小樣本的微小 Sharpe 差不作策略選擇。

### S19 — 籌碼確認的風險調整動能（CRM）

| 欄位 | 登記 |
|---|---|
| 核心規則 | `0.5*cs_rank(ts_ir(日報酬,20)) + 0.5*cs_rank((Σ20外資+Σ20投信)/近20日均量)`；閘門為動態 universe 成員 × `trend_ok`。 |
| 資料與 universe | **自建還原價**（`price_adjust.py`）；正式程式現採 **M 月只用完整 M-1 曆月**的交易所逐日全市場快照重建 top300（含下市股，`universes/monthly_pit.py`），再於池內做每日動態 top100；快照缺日 fail-closed。 |
| 進出場 | 10 檔等權、**20 日**再平衡、MA60 出場（次日開盤）、**-15%** 硬停損、T+1 開盤、含手續費與證交稅。 |
| 證據等級 | **不足以宣稱 edge。** 既有 IS/OS 數字使用舊的「生效日前 20 交易日」月池，不等於目前「完整上個曆月」規則；規則修正後尚未重跑，故舊績效只留作歷史診斷、不可引用為現版證據。 |
| 關鍵機制 | 相位標準差 0.509 ≈ 訊號效果本身的量級 —— **「哪天開始執行」比訊號更決定結果**（rebalance timing luck）。實際下單只會走其中一條路徑，所以最小值 -0.412 不是理論數字。 |
| **已作廢的數字** | ① 2026-08-02 版報「IS 五相位全 >1（中位 1.644）」—— 靜態候選池 look-ahead。② 2026-08-03 版報「IS 中位 1.607／OS 1.938」—— **評估窗洩漏**：`run_once` 未傳 `end_date`，IS 權益曲線溢出切點 144 天，把 OS 段的 +87.2% 算進 IS（真實 IS 為 0.306）。③ 兩版的 16 格參數掃描都建立在污染數字上，選出的 (10,20,MA60,0.15) 在乾淨掃描中**墊底**（0.131）。 |
| 已知失效／偏誤 | **OS 不再是乾淨 holdout** —— 洩漏的 IS 已包含 OS 期間走勢，故參數選擇間接看過 OS。相位離散極大；OS 每相位僅 16~21 筆交易無統計意義；單一多頭窗；已下市股價格仍缺（下市虧損被低估）。**多重檢定**：同閘門下隨機選 10 檔的 IS 五相位中位分布為 μ=0.312／σ=0.167，16 格搜尋的運氣門檻 0.706 —— 乾淨掃描最佳 1.205 雖超過門檻，但只有 1/16 勝過被動基準，且該格五相位最小值 -0.016。 |
| 相對隨機 vs 相對被動 | 兩者要分開講：S19 **明顯勝過同集中度的隨機選股**（1.205 vs 0.312），選股有訊號；但**只勉強勝過等權持有全部合格股**（1.126）。即選股技巧大致只夠補償集中到 10 檔的分散度代價。 |
| 下一個可證偽測試 | `freeze_manifest.py` 凍結 → `forward_test.py` 對 snapshot 之後做 forward-only —— 這是唯一還能升級證據等級的路（現有 IS/OS 都已被用過）。次要：補齊已下市股價格（可從 `pit_universe` 快照自帶 OHLCV 取）。 |
| freeze／forward 路徑修復（2026-08-15） | 這條路徑原本本身就不可信：manifest 沒凍到策略參數（10 檔／20 日／MA60／-15% 全在模組常數，改了 hash 不變）、`FROZEN_KEYS` 只列 34 個而 config 有 92 個、label 既不進檔名也不進 hash、forward 只跑單一相位且吃引擎預設 `rebalance_every=5/top_n=3`、沒有基準、同名輸出每次覆寫。現已修（`strategies/spec.py` + `freeze_manifest` 反向 allowlist + `forward_test` 全相位／PIT／基準／不可覆寫，回歸測試見 `tests/test_freeze_forward.py`）。**S19 尚未凍結、也未跑過任何 forward 期，證據等級維持 blocked；修好工具不等於任何策略被驗證。** |
| 相位評估統一（2026-08-15） | 「跑滿所有等價相位」原本有三份實作：`run_full` 掃 `rebalance_every`（CLI 預設 5）、S19 掃 20、`forward_test` 自己第三份聚合且用 `len(df) == 1` 反推 `single_phase_debug`。現已收攏到 `evaluation/phases.py`（回歸測試 `tests/test_phase_sweep.py`）。**這是重構，語意不變：本表所有數字都沒有重跑，S19 證據等級維持 blocked。** |
| 附帶證偽 | **`margin_drop`（融資餘額下降＝散戶退場）IC 為 -0.006／-0.002，假說不成立**；生動能 `mom_ret` 的 IS IC 僅 +0.0012，全期 IC 幾乎全來自 OS 普漲段。 |

## 目前開發優先序

1. 修資料：取得官方 `TaiwanStockPriceAdj` 後先驗收除息、配股、分割與減資案例；
   `price_adjust.py` 的自建除權息還原只保留為 fallback，不視為完整解法。接著補齊
   下市股價格覆蓋、~~TPEx~~（處置資料層已補）與 PIT 細產業／產品鏈標籤。
2. 重跑 S03、S04、S07、S08 的完全相同規則，建立不可變的實驗 manifest。
3. 將 S11 與 S13 固定版本的每日候選及市場狀態 append-only 保存，啟動真正
   forward paper log；不再用 2026-04~07 短窗微調 rank 門檻。
4. S18 已實作，取得至少 120 個乾淨交易日後才啟動 forward 評估；取得 PIT
   產業鏈後再測 S15。
5. 在資料到位後，才測 S10／S17 的 A/B/C 增量；不要先用新聞故事調門檻。

## 關聯檔案

- 基線與執行：`config.py`、`backtest.py`（事件驅動引擎）、
  `factor_engine/`（`data_fields.py` 無視窗欄位／`operators.py` 有視窗算子／
  `legacy_factors.py`；根目錄 `factors.py`、`operators.py` 為相容入口）、
  `execution/`（台股 tick／漲跌停／一字鎖停／處置禁倉／成本）
- Universe：`universes/monthly_pit.py`（正式：M 月只用完整 M-1 曆月）、
  `dynamic_universe.py`（每日 ADV20 top-N）、`pit_universe.py`（交易所逐日快照）、
  `universe.py` + `build_universe.py`（**legacy static，僅供對照，不得回套歷史**）
- 評估邊界：`evaluation/splits.py`（IS／embargo／OS；相容入口 `evaluation_split.py`）
- 族群假說：`rotation_research.py`、`sector_scan.py`
  （`outputs/ROTATION_STRATEGY_REVIEW.html` 是本機產物，**不進版控**，乾淨 clone 沒有）
- 即時觀察：`current_watchlist.py`、`market_flow_monitor.py`、`rank_flow_strategy.py`、`quiet_sponsor_strategy.py`
- 新策略實驗：`NEW_STRATEGY_EXPERIMENTS.md`、`outputs/RANK_FLOW_EXPERIMENT_REVIEW.md`
- 歷史審計（**append-only 紀錄，非目前有效績效**）：`outputs/WEIGHT_FIX_REPORT.md`、`outputs/DYNAMIC_UNIVERSE_REPORT.md`、`outputs/DEFENSIVE_RS_REPORT.md`、`outputs/MARKET_FILTER_REPORT.md`、`outputs/FACTOR_AUDIT_REPORT.md`、`outputs/winner_dna_report.md`
- 價格誠信：閘門在 `backtest._assert_price_integrity`；`price_integrity.py` 與
  `outputs/price_integrity_audit.csv` 是**診斷**，不是放行條件
