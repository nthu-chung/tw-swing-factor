# 交接文檔（HANDOFF）

> 給下一個接力研究的對話。**請先讀完這份，再讀 README.md。**
> 最後更新：2026-06-22

---

## 0. 環境（先做，否則跑不動）

- 系統 `python3` 是 3.14，套件裝不起來。**一律用 `.venv/bin/python`**（已建好，python3.11）。
- 若 `.venv` 不在：`python3.11 -m venv .venv && .venv/bin/pip install pandas numpy requests scipy yfinance`
- FinMind token 自動複用 `../taiwan-industry-analyzer/backend/.env`，免設定。
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

### ✅ 高可信：台股 2024-26 是「動能市」
- 因子體檢（`factor_audit.py` → `outputs/FACTOR_AUDIT_REPORT.md`）：
  **買強有效、買弱/拉回無效甚至反向**。
- `momentum` 是真 alpha（產業中性化後 IC +0.098/t2.45 不降反升、分層單調 +0.90、子期間穩定）。
- `ma_squeeze` 是教科書級**反向**因子（分層 Q5-Q1 −3.39%、單調 −1.00）。
  `vol_dryup`/`bb_pullback`/`inst_long` 該砍（翻號/冗餘/無效）。

### ✅ 中可信：族群輪動可被抓（唯一 OS 站得住的線）
- `sector_scan.py` → `outputs/sector_scan_report.md`：
  族群動能延續性 **OS_IC ret20=0.13 / inst6d=0.11 / breadth=0.07，全 >0.03**。
  強勢族群會續強，「提早抓族群 + 找補漲股」這條路成立。**建議優先深化。**

### ⚠️ 重大發現:資料漂移會讓決策站不住(2026-06-22 修)
- `data._date_range()` 用 `datetime.now()` 算結束日,每天視窗滑 1 天 +
  `_load_cache()` 12h 過期重抓 → FinMind 籌碼資料被回補,**同程式同權重隔兩天
  跑 IS Sharpe 從 0.41 變 1.33**(實證見 `outputs/WEIGHT_FIX_REPORT.md`)。
- 已修:`config.SNAPSHOT_END_DATE` 鎖快照、`_load_cache` 在快照下永久有效。
  驗證在同 snapshot 下兩次跑 bit-identical。**所有後續回測都基於這個快照**,
  推進資料要改 SNAPSHOT 並手動重抓。

### ✅ 上線權重已修正:純動能 momentum_only(2026-06-22)
- `outputs/WEIGHT_FIX_REPORT.md`(**務必讀**)。8 組權重對比 + 決策推導。
- 新上線:**`momentum_only` (mom 1.0)**——IS Sharpe **1.50** / 年化 +40.5% /
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

- `config.py` 的 `FACTOR_WEIGHTS` = **`{"momentum": 1.0}`**（2026-06-22 修;見第 2 節)。
  `FACTOR_WEIGHTS_LEGACY_MOMQ`(舊上線) / `FACTOR_WEIGHTS_LEGACY_9`(更舊) 保留備查。
- 資料快照:`config.SNAPSHOT_END_DATE="2026-06-22"`,所有回測以此為截止日。
- **最大瓶頸**:FinMind 免費版只有 2 年、且兩年都偏多頭,**結構上做不出可信的 OOS**。
  這比微調權重更根本。

---

## 4. 下一步候選（建議優先序）

1. **加市場濾網**(現在底已經是乾淨的純動能,加濾網不會被過擬合放大):VIX 高 /
   大盤跌破 MA200 → 整體部位 0%。簡單規則,先看是否真能改善 MaxDD 而不顯著損失年化。
2. **深化族群輪動**(第 2 節唯一 OS 站得住的線):補漲股選股流程化、做成可回測策略。
3. **walk-forward 評估漂移**:每月推一次 SNAPSHOT,記錄純動能 IS Sharpe 隨快照
   變動的範圍。若範圍仍大,代表問題不只是抓取視窗,還有更深的不穩定性需要處理。
4. **修 winner_dna** 的基準率問題（改持有報酬），重測是否真無 edge。
5. **解資料瓶頸**:設法取得 >3 年、含空頭的台股資料（FinMind 付費版 / 其他來源），
   否則所有「樣本外」結論都站不穩。

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
| `run_pipeline.sh` | 串選股 + DNA 的 pipeline |
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
