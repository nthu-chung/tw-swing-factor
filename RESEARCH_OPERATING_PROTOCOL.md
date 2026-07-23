# 台股波段研究作業規範

本文件定義 `tw-swing-factor` 接下來的研究節奏。目標不是每天產生一個
看起來很聰明的股票清單，而是持續累積可以被反證、重跑與稽核的市場觀察。

## 1. 分開兩條資料軌

### Frozen research snapshot

- 用於回測、因子比較與論文結果。
- 每次實驗固定資料截止日、universe 定義、參數與 benchmark。
- 不因每日新資料自動改寫歷史結果。
- 新聞、營收與事件只能在實際可得時間之後進入特徵。
- 未還原價出現異常斷點時整個績效實驗 fail-closed；不得刪一筆交易後繼續宣稱。

### Live monitoring snapshot

- 用於觀察目前市場、提出假說與建立候選名單。
- 每次輸出保留 `as_of`、來源、完整／不完整市場標記。
- 不把 live screen 的事後成功直接算成已驗證 alpha。
- 上市與上櫃覆蓋狀態必須分開揭露。
- 若 live 原始價出現 >20% 斷點，斷點日及後20個該股觀察日 quarantine；在
  建動態池、z-score、breadth 與 rank 前排除並重算。這只是安全隔離，不是還原價。

## 2. 每日觀察順序

1. **Market regime**
   - TAIEX 相對 MA20／MA60／MA200。
   - 5 日與 20 日報酬、實現波動、最大單日跌幅。
   - 大盤上漲但 breadth 下降時，標記為集中行情。

2. **Market breadth**
   - universe 中高於 MA20 的比例。
   - 20 日新高／新低比例。
   - 法人淨買超股票比例。
   - 上漲家數、下跌家數與成交值集中度。

3. **Universe flow**
   - 每日只用截至當日 ADV20 建立 causal dynamic liquidity pool。
   - top-100／top-200 新進與退出股票。
   - 1 日、5 日排名變化。
   - top-N churn 與排名穩定度。
   - 新進股票是剛發動、事件跳空，還是單日異常量。

4. **Group flow**
   - 族群相對強弱與族群 breadth。
   - 法人資金是否為多檔同步，而非單一權值股造成。
   - 第一棒、擴散、末端補漲分開標記。

5. **Stock trigger**
   - 個股趨勢、20 日突破、量比與法人方向。
   - 訊號日收盤確認；最早次日開盤成交。
   - 同族群候選不能被誤當成十個獨立投資機會。

6. **Theme verification**
   - 新聞只能解釋或否決量化候選，不得取代進場條件。
   - 優先使用公司公告、MOPS、營收與法說資料。
   - 驗證題材和公司收入、訂單、產品或毛利的實際連結。
   - 只有媒體標籤而沒有曝險證據者標記 `needs exposure attribution`。

## 3. 假說紀錄格式

每個新假說至少包含：

- `hypothesis_id`
- `observed_at`
- 觀察到的異常
- 經濟或市場機制
- 預期受惠族群與反向對照組
- 使用的特徵及其可得時間
- `prove` 指標與觀察期限
- `kill` 指標與停止條件
- 資料缺口
- 尚未測試／IS／embargo／OOS／forward-only 狀態

規格先登記在 `STRATEGY_REGISTRY.md`，完整實驗與失敗結果記在
`NEW_STRATEGY_EXPERIMENTS.md`；跑完不能只保留成功版本。

不能只記錄成功題材。未發動、假突破與被新聞誤導的案例必須保留。

## 4. 研究證據等級

| 等級 | 意義 | 可做的事 |
|---|---|---|
| Observation | 當前數據或新聞現象 | 提出假說 |
| Screen flag | 通過量化初篩 | 納入觀察名單 |
| Research candidate | 題材曝險與觸發條件都有證據 | 進一步研究 |
| IS-supported | 樣本內有效 | 設計 OOS |
| Pseudo-OOS | 時間切割，但研究者已知該段結果或 universe 不乾淨 | 檢查實作 |
| Clean OOS | point-in-time 資料與預先固定規則 | 評估是否有可重複效果 |
| Forward-only | 規則凍結後累積的新資料 | 最高優先的真實檢驗 |

## 5. Agent 分工

- **Terra**
  - 明確、低歧義的資料整理。
  - 報表、監測器、測試樣板與可重跑腳本。
  - 公開來源彙整與候選資料 QA。

- **Sol**
  - 因果對齊、look-ahead 與 survivorship 審查。
  - 研究設計、multiple testing、反證與模型失效分析。
  - 複雜策略實作和高風險程式審查。

- **主 agent**
  - 定義問題、拆解任務與整合衝突。
  - 驗證 agent 產出，不直接照單全收。
  - 保留使用者既有修改與 frozen snapshot。
  - 清楚區分研究候選、正式訊號與尚未驗證的敘事。

## 6. 硬性盲點檢查

每次宣稱策略改善前，至少檢查：

- candidate-pool survivorship bias
- 上市／上櫃與下市股票覆蓋
- 未還原價格、除權息與分割
- 訊號時間與最早可成交時間
- 新聞、營收與公告的 point-in-time 時間戳
- 族群分類是否隨時間改變
- 同族群相關性與表面分散
- 交易成本、漲跌停、跳空與容量
- 多重參數搜尋與選最佳結果偏誤
- 單一市場 regime 或少數 monster winners 支配績效

## 7. 研究停止條件

出現以下任一情況，不把結果升級為策略結論：

- 未來資料擾動會改變過去訊號。
- clean OOS 相對 benchmark 優勢消失。
- 績效主要由一至兩檔股票或一個短期 regime 貢獻。
- 移除題材贏家後策略失效。
- 加入合理成本、跳空或漲停限制後優勢消失。
- 新聞特徵沒有可靠可得時間。
- 無法重建歷史 universe。

## 8. 預設節奏

- **每日**：更新 live market/universe flow，記錄新假說與失效訊號。
- **每週**：檢查 rank persistence、族群擴散、候選命中與假突破。
- **每月**：凍結一次 forward snapshot，禁止回頭調參。
- **每次改策略**：先寫 prove/kill，再跑實驗；保留所有失敗結果。
