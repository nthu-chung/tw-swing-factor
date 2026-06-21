# 交接文檔（HANDOFF）

> 給下一個接力研究的對話。**請先讀完這份，再讀 README.md。**
> 最後更新：2026-06-20

---

## 0. 環境（先做，否則跑不動）

- 系統 `python3` 是 3.14，套件裝不起來。**一律用 `.venv/bin/python`**（已建好，python3.11）。
- 若 `.venv` 不在：`python3.11 -m venv .venv && .venv/bin/pip install pandas numpy requests scipy yfinance`
- FinMind token 自動複用 `../taiwan-industry-analyzer/backend/.env`，免設定。
- 資料快取在 `_cache/`（pickle，12h 有效）；過期會自動重抓（top100 約 2~3 分鐘，會打 API）。
- git **尚未 init**，所有成果只在本地。

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

### ⚠️ 重要反轉：上線權重 mom_quality 其實是過擬合
- `validate_oos.py` → `outputs/OOS_VALIDATION_REPORT.md`（**務必讀**）：
  - OS 段(2025-12~2026-06)年化爆衝 +267% 是**普漲行情 beta，不是 alpha**：
    該段 top100 等權買進持有平均 +170%、98% 個股上漲。bootstrap CI 下界正值是假象。
  - **真正能分辨權重的是 IS 段**：`momentum_only` Sharpe **1.40** 完勝
    `mom_quality`(目前上線) **0.41**。加 margin_health/ma_alignment **反而拖累**。
  - 全期回測顯示 mom_quality「1.53 不錯」純粹被 OS 普漲拉高 = 過擬合。

### ❌ 失敗發現：個股單點 DNA 無樣本外 edge
- `winner_dna.py` → `outputs/winner_dna_report.md`：OS lift ≈ 1.04（≈隨機）。
  ⚠️ **有瑕疵**：飆漲定義用「未來期間最高點」使基準率虛高到 52%，lift 失去鑑別力。
  若要救活，須改成「未來 N 日**持有報酬**」或拉高 gain 門檻。

---

## 3. 當前狀態 / 已知問題

- `config.py` 的 `FACTOR_WEIGHTS` = **mom_quality**（momentum0.5/ma_alignment0.2/margin_health0.3），
  `FACTOR_WEIGHTS_LEGACY_9` 保留備查。**但第 2 節證明這組是過擬合，待修。**
- **最大瓶頸**：FinMind 免費版只有 2 年、且兩年都偏多頭，**結構上做不出可信的 OOS**。
  這比微調權重更根本。

---

## 4. 下一步候選（建議優先序）

1. **修上線權重**：往純動能靠（調高 momentum、砍 margin_health/ma_alignment），
   用 **IS Sharpe** 當篩選指標（**不要看全期**，會被 OS 普漲騙）。改完用 `validate_oos.py` 複驗。
2. **深化族群輪動**（第 2 節唯一 OS 站得住的線）：補漲股選股流程化、做成可回測策略。
3. **加市場濾網**（VIX / 大盤 MA200 當總開關），防動能在反轉期集體失靈。
4. **修 winner_dna** 的基準率問題（改持有報酬），重測是否真無 edge。
5. **解資料瓶頸**：設法取得 >3 年、含空頭的台股資料（FinMind 付費版 / 其他來源），
   否則所有「樣本外」結論都站不穩。

> 動手前建議用 AskUser 跟 user 對齊要走哪一條，別預設。

---

## 5. 檔案地圖（這次研究新增的，非原始 repo）

| 檔案 | 用途 |
|---|---|
| `factor_audit.py` | 因子體檢：相關矩陣 / 產業中性 IC / 分層報酬 / 子期間 |
| `experiment_weights.py` | 權重對比回測（⚠️ 只看全期，會被 OS 騙，需配合 validate_oos） |
| `validate_oos.py` | 階段三 IS/OS + bootstrap 驗證（**最重要**） |
| `winner_dna.py` | 飆漲股 DNA（失敗發現，有瑕疵） |
| `sector_scan.py` | 族群輪動掃描（正向，建議深化） |
| `run_pipeline.sh` | 串選股 + DNA 的 pipeline |
| `outputs/*_REPORT.md` | 各研究的報告 |

---

## 6. 鐵則（這個專案的研究紀律，別違反）

- **誠實面對失敗**：edge 不存在就說不存在，回測漂亮先懷疑是不是 bug / 行情 / 過擬合。
- **永遠分 IS/OS 看，不要只看全期**——這次就是吃了全期的虧。
- 防未來函數：T+1 進場、merge_asof backward、rolling 只看過去、fwd_ret 不進因子。
- 複雜度↔穩定性反向：因子/參數越少越好，加東西要先證明有增量 edge。
