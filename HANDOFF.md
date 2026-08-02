# 交接文檔（HANDOFF）

> 給下一個接力研究的對話。**請先讀完這份，再讀 README.md。**
> 最後更新：2026-07-23

---

## 0. 環境（先做，否則跑不動）

- 系統 `python3` 是 3.14，套件裝不起來。**一律用 `.venv/bin/python`**（已建好，python3.11）。
- 若 `.venv` 不在：`python3.11 -m venv .venv && .venv/bin/pip install pandas numpy requests scipy yfinance`
- FinMind token 自動複用 `../taiwan-industry-analyzer/backend/.env`，免設定。
- **重現任何 top100/200/300 回測前,先跑 `.venv/bin/python build_universe.py`
  建池**(產 `outputs/universe_top*.json`,已 gitignore、不進版控)。否則
  `uni.get_universe(top_n=100)` 會**靜默降級到 14 檔小集合**,回測數字與報告完全對不上。
- 資料快照(2026-06-22 加):`config.SNAPSHOT_END_DATE="2026-06-22"` 鎖住資料截止日。
  在 snapshot 鎖住時 **`_cache/` 永久有效**(換 snapshot 才主動推進);若要重抓,
  改 SNAPSHOT 或手動清 cache(top100 約 2~3 分鐘)。設成 `""` 退回 `datetime.now()`,
  探索 / debug 用,**正式回測請維持鎖日**(否則會發生第 2 節描述的漂移)。
- git 已 init,initial commit 是研究框架的快照。

---

## 1. 這個專案是什麼

台股波段多因子選股「**能證偽的研究框架**」——重心是嚴格回測驗證因子有沒有 edge，
不是喊買進的選股器。資料 FinMind 免費版（2 年、成交值 top100/200/300 池）。

核心檔：`data.py`(抓取) → `factors.py`(因子) → `screener.py`(選股) / `backtest.py`(回測+IC)。
`config.py` 集中所有可調參數。入口 `main.py`。

---

## 2. 目前累積的研究結論（依可信度排序）

### ⛔ 2026-07-23 P0：公司行動已實際污染回測與模型選擇
- `_cache/price__2327.pkl` 中，國巨收盤由 2025-08-13 的 546 跳至
  2025-08-25 的 143；這是分割／公司行動斷點，不是普通 -73.8% 報酬。
- `outputs/rotation_trades.csv` 將 2025-07-24 進場的國巨於 8/25 記為
  `hard_stop_gap`、報酬 -73.61%。
- 這筆錯誤重創 `rotation_breakout + ma10` 的 IS，可能使 `hold20` 被錯選為
  最佳退場。既有 IS/pseudo-OOS 與 robustness 表全部降級為未校正暫存結果。
- 最優先工作是：PIT 歷史證券池＋公司行動一致化價格＋持股股數／成本調整；
  修正前不可宣稱策略已驗證有效。

### ✅/❌ 2026-07-23 新策略實驗：dynamic rank-flow 已實作，但 standalone entry 未通過
- `market_flow_monitor.py` 現在先用截至 T 的 ADV20 建每日前300動態池，再在池內
  重算動能／成交／法人 z-score、flow rank、churn 與動態 breadth。
- 2380（2026-06-29）與 6944（2026-07-23）原始價斷點已被偵測；live monitor
  會從斷點起 quarantine 21 個該股觀察日，且在橫斷面計算前排除，不猜調整因子。
- `rank_flow_strategy.py` 實作 `confirmed_entrant`、`persistent_leader`、
  `breadth_expansion`、`rank_flow_persistence`；T+1 open、正向跳空 >5% 不追，
  5／10／20日同動態池 benchmark 事件研究。
- 2026-04-24~07-23 共62日只屬 exploratory IS。四變體 signal-date cohort
  超額都沒有跨 horizon 一致為正；rank path 單獨作 entry **不晉級**。
- 下一步不是在同一短窗繼續調 rank 門檻，而是把 rank 降為確認層，優先 forward
  測 `quiet_sponsor_compression`（法人吸收＋壓縮突破）與取得 PIT 產業鏈後的
  `sector_relay`。見 `STRATEGY_REGISTRY.md`、`NEW_STRATEGY_EXPERIMENTS.md`、
  `outputs/RANK_FLOW_EXPERIMENT_REVIEW.md`。
- `quiet_sponsor_strategy.py` 已實作硬性120日乾淨 ATR 基準、T+1 及 >4% 不追；
  目前62日面板正確輸出零訊號／`insufficient_history`，禁止為了出結果縮短。

### ⚠️ 2026-07-23 修正：static bias 存在，但單一 -4.4% 不能否定動能
- 新增 `dynamic_universe.py` 與 `outputs/DYNAMIC_UNIVERSE_REPORT.md`。
- 相同 momentum-only long-only 策略：
  - static current top100：累積 +339.1%、Sharpe 2.27、MaxDD -21.9%。
  - current top300 內每日動態 top100：累積 -4.4%、Sharpe 0.11、MaxDD -37.8%。
- 動態排名只用訊號日以前含當日的20日平均成交值/量；每日中位100檔、全期曾入池234檔。
- 後續相位敏感度發現：五種等價的每五日進場相位，累積為 -4.4%、+144.7%、
  +49.6%、+218.5%、+274.9%；原報告剛好只引用唯一負值。
- 動態 top-5 在每五日形成日的未來20日平均 +8.23%，同日 universe +5.38%，
  超額 +2.84 個百分點，重疊校正 t=2.70。**因此舊報告「edge 被否定」撤回。**
- 動態版候選仍是 current top300，`survivorship_free=False`；需 FinMind 還原價+
  下市股／全市場歷史，或 TEJ PIT 才能做最終判斷。資料盤點見 `DATA_SOURCE_RESEARCH.md`。

### ⚠️ 目前較可信：動能有排序力，但執行規則高度敏感
- 因子體檢（`factor_audit.py` → `outputs/FACTOR_AUDIT_REPORT.md`）：
  **買強有效、買弱/拉回無效甚至反向**。
- 動態 universe 的平均 IC +0.045/t1.07 單獨看不顯著；但 top-5 事件研究與五相位
  敏感度顯示，不能只靠平均 IC 或一條投組路徑下結論。
- `ma_squeeze` 是教科書級**反向**因子（分層 Q5-Q1 −3.39%、單調 −1.00）。
  `vol_dryup`/`bb_pullback`/`inst_long` 該砍（翻號/冗餘/無效）。

### ⚠️ 待重跑的研究假說：族群／法人預篩，再用價量突破確認
- `sector_scan.py` → `outputs/sector_scan_report.md`：
  族群動能延續性 **OS_IC ret20=0.13 / inst6d=0.11 / breadth=0.07，全 >0.03**。
  強勢族群會續強，「提早抓族群 + 找領先/補漲股」這條路值得深化。
- 新增 `rotation_research.py`：前三強族群＋法人6日淨買為正＋20日突破＋量比1.2，
  T+1開盤買；-8%硬停損；比較四種出場。
- 原始未還原價下，僅依 IS 選出的 `rotation_breakout + hold20`：
  - IS 2024-07-17~2025-11-17：+77.9%，TAIEX +15.5%，Sharpe 1.77。
  - 20日 embargo 後 pseudo-OOS 2025-12-16~2026-06-18：+226.6%，
    TAIEX +68.7%，Sharpe 4.62。
- 這個 OOS 已被人類看過2026題材，仍用 current top300/raw price，且現已確認
  corporate-action 污染；**只能作 implementation 與錯誤重現，不能當有效證據**。
- top100/top200、3/5/10檔與量比1.0/1.2/1.5大多仍為正；top200三檔最不穩。
- 記憶體核心與國巨/華新科都在候選池且有早期訊號；top100會漏部分二線被動元件。

### ⚠️ 重大發現:資料漂移會讓決策站不住(2026-06-22 修)
- `data._date_range()` 用 `datetime.now()` 算結束日,每天視窗滑 1 天 +
  `_load_cache()` 12h 過期重抓 → FinMind 籌碼資料被回補,**同程式同權重隔兩天
  跑 IS Sharpe 從 0.41 變 1.33**(實證見 `outputs/WEIGHT_FIX_REPORT.md`)。
- 已修:`config.SNAPSHOT_END_DATE` 鎖快照、`_load_cache` 在快照下永久有效。
  驗證在同 snapshot 下兩次跑 bit-identical。**所有後續回測都基於這個快照**,
  推進資料要改 SNAPSHOT 並手動重抓。

### ❌ 已撤回：純動能 momentum_only 上線結論(2026-06-22)
- `outputs/WEIGHT_FIX_REPORT.md`(**務必讀**)。8 組權重對比 + 決策推導。
- 歷史決策:**`momentum_only` (mom 1.0)**——static IS Sharpe **1.50** / 年化 +40.5% /
  MaxDD -19.4% /(snapshot 2026-06-22, top100 trend 退場)。
- 舊 `mom_quality` 在新快照下 IS Sharpe 1.33 < 純動能,且依賴邊際因子,移除上線。
  保留在 `config.FACTOR_WEIGHTS_LEGACY_MOMQ` 備查。
- **證偽**:加 margin_health / ma_alignment / inst_dip_buy 全部都拖累或在誤差內。
  IC 強 ≠ 回測賺;以後不要直接從 IC 推權重。

### ⚠️ 重要反轉(歷史保留):上線權重 mom_quality 是過擬合(現已修)
- 前一輪用「全期 +1.53」決策,但 OS +267% 純粹是普漲 beta(top100 等權買持
  平均 +170%、98% 個股漲)。`outputs/OOS_VALIDATION_REPORT.md` 紀錄此發現,
  本次接力進一步證實並修了上線權重。

### ❌ 失敗發現：個股單點 DNA 無樣本外 edge
- `winner_dna.py` → `outputs/winner_dna_report.md`：OS lift ≈ 1.04（≈隨機）。
  ⚠️ **有瑕疵**：飆漲定義用「未來期間最高點」使基準率虛高到 52%，lift 失去鑑別力。
  若要救活，須改成「未來 N 日**持有報酬**」或拉高 gain 門檻。

---

## 3. 當前狀態 / 已知問題

- `config.py` 的 `FACTOR_WEIGHTS` = **`{"momentum": 1.0}`**，目前只作最簡單的
  research baseline，不代表上線／實盤建議。
  `FACTOR_WEIGHTS_LEGACY_MOMQ`(舊上線) / `FACTOR_WEIGHTS_LEGACY_9`(更舊) 保留備查。
- 資料快照:`config.SNAPSHOT_END_DATE="2026-06-22"`,所有回測以此為截止日。
- **最大瓶頸**:候選池仍是 current top300、原始價且只有 2 年，
  **結構上做不出可信的 survivorship-free OOS**。
  這比微調權重更根本。

---

## 4. 下一步候選（建議優先序）

1. **先換論文級資料**：FinMind 還原價+全市場歷史/下市股，或 TEJ PIT。
2. **將 rotation 流程接到正式 screener**：top200研究池→族群/法人→突破→候選清單。
3. **補月營收公告時點、重訊全文與細產業鏈標籤**，再做真正的「題材先篩」消融。
4. **修正式 backtest 的 MA20 同收盤退場**：rotation研究器已是T+1，舊引擎仍待修。
5. **重跑所有因子、族群與濾網報告**：舊報告一律視為 static-universe 歷史結果。
6. **walk-forward 評估漂移**:每月推一次 SNAPSHOT,記錄純動能 IS Sharpe 隨快照
   變動的範圍。若範圍仍大,代表問題不只是抓取視窗,還有更深的不穩定性需要處理。
7. **修 winner_dna** 的基準率問題（改持有報酬），重測是否真無 edge。

> 動手前建議用 AskUser 跟 user 對齊要走哪一條，別預設。

---

## 5. 檔案地圖（這次研究新增的，非原始 repo）

| 檔案 | 用途 |
|---|---|
| `factor_audit.py` | 因子體檢：相關矩陣 / 產業中性 IC / 分層報酬 / 子期間 |
| `experiment_weights.py` | 權重對比回測（⚠️ 只看全期，會被 OS 騙，需配合 validate_oos） |
| `validate_oos.py` | 階段三 IS/OS + bootstrap 驗證（**最重要**;2026-06-22 擴充候選） |
| `winner_dna.py` | 飆漲股 DNA（失敗發現，有瑕疵） |
| `sector_scan.py` | 族群輪動掃描（正向，建議深化） |
| `rotation_research.py` | 族群/法人→價量突破→T+1執行，IS/OOS與題材案例稽核 |
| `market_flow_monitor.py` | ADV20前300動態池、rank/churn、全市場/池內breadth、價格quarantine |
| `rank_flow_strategy.py` | 四個 rank-flow 假說與 T+1 事件研究（standalone 未通過） |
| `quiet_sponsor_strategy.py` | 法人吸收＋低波壓縮突破 forward 原型；需120日乾淨warmup |
| `price_integrity.py` | 原始價斷點稽核；歷史回測 fail-closed |
| `STRATEGY_REGISTRY.md` | 策略永久台帳、證據狀態與下一個可證偽測試 |
| `NEW_STRATEGY_EXPERIMENTS.md` | E01失敗紀錄與 E02~E06 預註冊規格 |
| `run_pipeline.sh` | 串選股 + DNA 的 pipeline |
| `outputs/ROTATION_STRATEGY_REVIEW.html` | 最新第一讀研究結論與資料採購清單 |
| `outputs/WEIGHT_FIX_REPORT.md` | 2026-06-22 上線權重修正 + 資料漂移修法 |
| `outputs/*_REPORT.md` | 各研究的報告 |

---

## 6. 鐵則（這個專案的研究紀律，別違反）

- **誠實面對失敗**：edge 不存在就說不存在，回測漂亮先懷疑是不是 bug / 行情 / 過擬合。
- **永遠分 IS/OS 看，不要只看全期**——前一輪就是吃了全期的虧。
- **回測必鎖 SNAPSHOT_END_DATE**——2026-06-22 才發現的:沒鎖視窗,IS Sharpe 會被
  資料邊界漂移和 FinMind 籌碼回補放大到 3 倍幅度,任何決策都站不住。
- 防未來函數：T+1 進場、merge_asof backward、rolling 只看過去、fwd_ret 不進因子。
- 複雜度↔穩定性反向：因子/參數越少越好，加東西要先證明有增量 edge。
- **小樣本下不為小差距改決策**:IS 80 筆下 Sharpe 差 <0.05 / <3% 都是噪音,
  絕不為這個級別的差距加因子或改權重。
