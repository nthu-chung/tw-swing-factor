# tw-swing-factor — 台股波段多因子選股系統

用**籌碼面 + 技術面 + 大戶進出**多因子組合，挑選台股波段標的（持有數天～數週，白天可操作、不熬夜），並用**嚴格回測誠實驗證每個因子到底有沒有 edge**。

> 這套系統的定位不是「再做一個會喊強力買進的選股器」，而是一個**能證偽的研究框架**——先用快速原型跑通流程，看哪些因子真的有預測力，再決定要不要相信它。

> ⛔ **2026-07-23 重大稽核更新**：未還原價格已實際污染回測。國巨 2025-08-25
> 的分割前後斷點被誤記為約 -73.6% 的 hard-stop，並可能改變最佳退場規則。
> 因此既有 IS / pseudo-OOS 績效全部降級為「未校正暫存結果」；公司行動與
> 完整 PIT universe 重跑前，只能說「族群＋法人＋突破」值得 forward-test，
> 不能宣稱策略已證明有效。最新稽核見
> [ROTATION_STRATEGY_REVIEW.html](./outputs/ROTATION_STRATEGY_REVIEW.html)。

---

## 為什麼做這個

市面上的台股選股開源專案（已研究 5 個，見最下方）共同弱點是：**選股邏輯花俏，但回測太陽春**（常常只用固定持有 N 天算個勝率就說「有效」）。本專案反過來，把重心放在**驗證**：

- 整體回測：勝率 / 平均報酬 / 累積報酬 / 最大回撤 / 類 Sharpe
- 逐因子 IC（資訊係數）：每個因子對未來報酬的 Spearman 相關，看誰真的有用
- 嚴格防未來函數（point-in-time 對齊、T+1 進場）

---

## 架構

```
tw-swing-factor/
├── config.py        # 所有可調參數：因子權重、門檻、回測設定（調參只改這裡）
├── data.py          # 資料抓取層：FinMind（價格/法人/融資）+ 本地 pickle 快取
├── universe.py      # 選股池：小集合 / 全市場，pre-filter（流動性）
├── factors.py       # 多因子計算：每日因子值 + 0~1 標準化分數 + 綜合評分
├── screener.py      # 選股引擎：算因子→過濾→評分→排序→輸出清單
├── backtest.py      # 回測 + 因子IC：驗證選股到底有沒有用
├── rotation_research.py # 族群/法人預篩→價量突破→T+1買賣的 IS/OOS 研究
├── current_watchlist.py # TWSE 官方資料即時初篩（非回測策略 parity）
├── market_flow_monitor.py # 每日 rank/churn/breadth/法人流監測
├── rank_flow_strategy.py # 四種 causal rank-flow 訊號與 T+1 事件研究
├── quiet_sponsor_strategy.py # 法人吸收＋低波壓縮突破的 forward-only 原型
├── price_integrity.py # 公司行動／異常價格斷點 fail-closed 稽核
├── twse_disposition.py # 上市注意/處置資料層（由注意推導處置期間，derived）
├── tpex_disposition.py # 上櫃注意/處置資料層（官方直接給真實起訖，actual）
├── STRATEGY_REGISTRY.md # 所有既有策略、狀態、偏誤與下一個證偽測試
├── main.py          # 統一入口（命令列）
├── _cache/          # 原始資料快取（自動產生，已 gitignore）
└── outputs/         # 選股清單 / 回測結果 CSV（自動產生）
```

資料流：`data → factors → screener`（選股）／`data → factors → backtest`（驗證）

---

## 安裝與設定

```bash
cd tw-swing-factor
pip3 install -r requirements.txt
```

**FinMind Token**（抓籌碼/法人歷史資料必需）：
- 本專案會自動複用同工作區 `taiwan-industry-analyzer/backend/.env` 內的 `FINMIND_TOKEN`
- 或自行設定環境變數：`export FINMIND_TOKEN="你的token"`

---

## 使用

```bash
# 今日選股（小集合，快速）
python3 main.py screen

# 今日選股（全市場，較慢，第一次會抓很多資料）
python3 main.py screen --full

# 回看某一天當時會選出什麼（驗證用）
python3 main.py screen --date 2026-05-20

# 回測 + 因子IC（核心：看這套到底有沒有用）
python3 main.py backtest
python3 main.py backtest --full --top 5

# 只看因子IC分析
python3 main.py ic
```

輸出會印在畫面，同時存到 `outputs/`。

### Long-only 動態 universe（研究模式）

不要再用期末 top100 固定回套整段歷史。現在可用較寬的候選池，在每個訊號日只依
「截至當日」的近20日平均成交值重排 top100：

```bash
# 先準備 bootstrap 候選池（目前仍是 current top300，非完整 PIT）
.venv/bin/python build_universe.py 300

# long-only；每日 universe top100；每次最多挑5檔
.venv/bin/python main.py backtest --pool 300 --universe-top 100 --top 5

# legacy static universe 只供對照
.venv/bin/python main.py backtest --pool 100 --static-universe --top 5
```

動態 universe 只改變「當日可被選的股票」，不做空、不建立 short leg。現有
current top300 仍有候選池生存者偏誤，所以回測 metadata 會明確標示
`survivorship_free=False`。論文級資料升級建議見
[DATA_SOURCE_RESEARCH.md](./DATA_SOURCE_RESEARCH.md)。

### 族群輪動＋法人＋突破研究

```bash
.venv/bin/python rotation_research.py
```

固定流程為：動態 universe → 粗產業族群強度與法人買盤 → 個股 20 日價量突破 →
次日開盤買入。比較純動能、族群篩選、族群＋法人＋突破，以及 MA10 / MA20 /
固定 20 / 40 日等出場；輸出 `rotation_is_oos.csv`、`rotation_trades.csv` 與
`theme_case_audit.csv`。這是研究候選，不是自動交易建議。

### Live dynamic-universe 與 rank-flow 新策略實驗

```bash
# 官方 TWSE 資料；每日 ADV20 前300後才重算 flow rank
.venv/bin/python market_flow_monitor.py \
  --as-of 2026-07-23 --calendar-days 90 --universe-size 300 --top-n 20

# 四個固定 rank-flow 假說；T+1 open、5/10/20日事件研究
.venv/bin/python rank_flow_strategy.py \
  --metrics outputs/market_flow_metrics_20260723.csv \
  --breadth outputs/market_flow_breadth_20260723.csv

# 下一版：需120個乾淨ATR觀察；歷史不足時應輸出零訊號
.venv/bin/python quiet_sponsor_strategy.py \
  --metrics outputs/market_flow_metrics_20260723.csv
```

Live monitor 會把 >20% 價格斷點及後續20個該股觀察日先 quarantine，再建動態池與
排名，不猜公司行動調整倍數。2026-04-24~07-23 的 62 日探索窗中，四個
rank-flow 變體都沒有跨 5／10／20 日一致超額，故目前**不能單獨作買進策略**；
完整失敗結果與下一版假說見
[RANK_FLOW_EXPERIMENT_REVIEW.md](./outputs/RANK_FLOW_EXPERIMENT_REVIEW.md) 與
[NEW_STRATEGY_EXPERIMENTS.md](./NEW_STRATEGY_EXPERIMENTS.md)。`quiet_sponsor`
目前因只有62日而輸出 `insufficient_history`／零訊號，規則不為此縮短。

---

## 因子說明

### 動能面（找「下一波成長」的核心）
| 因子 | 定義 | 直覺 |
|---|---|---|
| `momentum` | 60日報酬(到+30%滿分) + 貼近季線高點(≥0.90滿分)，取平均 | 強者恆強，趨勢健康的續強股 |

> 這是修正版新增的因子。原型缺少動能訊號，但「找下一波成長股」最核心的特徵就是**強勢續強**（學術界的 momentum factor、實務的 Qullamaggie 突破系統都以此為主軸）。

### 籌碼面（核心假設）
| 因子 | 定義 | 直覺 |
|---|---|---|
| `inst_mid` | 法人(外資+投信)近6日淨買 / 近20日均量 | 主力中期在不在買 |
| `inst_long` | 法人近12日淨買 / 近20日均量 | 主力長期是否未撤 |
| `inst_dip_buy` | 近5日「收黑但法人仍買」的天數 | 洗盤 vs 出貨 |
| `margin_health` | 資券比（融資/融券）落在 2~8 健康區間 | 籌碼結構是否健康 |

> **關鍵工程細節**：法人淨買一律**用近20日均量正規化**，這樣台積電（大型股）和小型股才能公平比較。主力定義為**外資+投信**，排除自營商（避險雜訊）。

### 技術面
| 因子 | 定義 | 直覺 |
|---|---|---|
| `ma_alignment` | 收盤 > MA20 > MA60 | 均線多頭排列 |
| `bb_pullback` | 布林位階在 0~0.5（拉回月線但未跌破） | 波段回檔買點 |
| `ma_squeeze` | MA20/MA60 BIAS 都很小 | 均線糾結、能量壓縮 |
| `vol_dryup` | 近5日均量 / 前5日均量 ≤ 0.5 | 回檔量縮（窒息量） |

### 趨勢保護（硬門檻，任一不過直接淘汰）
`MA20 > MA60` 且 `MA60 上揚` 且 `收盤 > MA60` —— 避免接刀（買在下降趨勢）。

### 綜合評分
各因子輸出 0~1 分數，依 `config.FACTOR_WEIGHTS` 加權後正規化成 0~100。權重可自由調整。

---

## 防未來函數 + 回測正確性（重要）

回測最容易作弊的地方就是偷看未來，以及把投組績效算錯。本系統的防護：

1. **T+1 進場**：訊號在第 T 日收盤後產生，第 T+1 日**開盤**才進場（`BT_ENTRY_NEXT_OPEN`）
2. **資料對齊**：法人/融資資料用 `merge_asof(direction="backward")` 對齊到價格日，只會用「≤ 當日」最近一筆
3. **因果計算**：所有 rolling 指標只看過去
4. **未來報酬隔離**：`fwd_ret`（未來N日報酬）只在 IC 分析時用，絕不進因子計算
5. **真正的每日權益曲線**：等權重最多 `BT_MAX_POSITIONS` 檔並行持倉，逐日 mark-to-market 加總成投組淨值，MaxDD / Sharpe 全部由淨值序列算
6. **跳空填價**：停損/停利當天若開盤已穿價，用開盤價成交（更不利），不用理論價
7. **IC 重疊校正**：`fwd_ret` 視窗重疊造成每日 IC 自相關，t 值用「有效樣本 = 天數 / 視窗」保守校正，避免灌水顯著性

---

## 回測結果（修正版引擎，2024-06 ~ 2026-06，小集合 14 檔）

> **退場模式：trend（波段）** — 跌破 MA20 或硬停損 -8% 或抱滿 120 天才出場，讓獲利奔跑。

| 指標 | 數值 | 說明 |
|---|---|---|
| 交易筆數 | 35 | 平均持有 12.7 天（最長可達 120 天） |
| 勝率 | 28.6% | 低勝率 |
| 賺賠比 (payoff) | 4.96 | **靠少數大贏家獲利** — 趨勢波段策略的典型特徵 |
| 累積報酬 | +16.39% | |
| 年化報酬 | +8.53% | |
| **最大回撤** | **-12.35%** | 由每日淨值算 |
| Sharpe (年化) | 0.76 | 由每日淨值算 |

> ⚠️ **35 筆樣本太少，不能下定論。** 低勝率高賺賠比符合「找成長股、讓獲利奔跑」的設計目標，但需擴大 universe 驗證穩定性。

### 逐因子 IC（重疊校正後）

| 因子 | mean IC | t_stat | 判讀 |
|---|---|---|---|
| ma_alignment（均線多頭） | +0.087 | +1.28 | 弱訊號，未達顯著 |
| margin_health（資券健康） | +0.071 | +0.95 | 弱訊號，未達顯著 |
| momentum（動能） | +0.061 | +0.83 | 弱訊號，未達顯著 |
| inst_long（法人12日） | +0.045 | +0.64 | 弱訊號，未達顯著 |
| inst_mid（法人6日） | +0.027 | +0.41 | 弱訊號，未達顯著 |
| inst_dip_buy（跌時法人買） | +0.006 | +0.09 | 無明顯預測力 |

> ⚠️ **修正前後的關鍵差異**：舊版宣稱 ma_alignment / bb_pullback / margin_health「★有預測力」，但那是**沒做重疊校正**的灌水結果。重疊校正後 **所有因子 t_stat 都 < 2（未達統計顯著）** —— 這才是 14 檔小樣本該有的誠實結論：**現階段沒有任何因子被證明有效**，必須擴大 universe 才能下判斷。
>
> 舊版「最大回撤 -35.69%」也是**序列複利算錯**的假數字，修正後實際為 **-12.35%**。

---

## 路線圖

- [x] **階段一（已完成）**：快速原型——資料層、因子、選股、回測閉環跑通
- [ ] **階段二**：擴大 universe 到全市場（數百檔），重算因子 IC，確認哪些因子真有 edge
- [ ] **階段三**：上嚴格驗證——IS/OS 70/30 切分 + Embargo 緩衝 + 統計檢驗（t-test、Bootstrap CI、子期間穩定性），杜絕過擬合
- [ ] **階段四**：依驗證結果重新分配因子權重（讓有 edge 的因子主導），加入退場機制
- [ ] **階段五**：接 LINE/Telegram 推播（可複用 tw-stock-linebot-reporter）

---

## 參考的開源專案

| 專案 | 借鑑的點 |
|---|---|
| `taiynlee/institutional-investors` | 主力未撤回檔邏輯、籌碼用股本正規化、雙時間窗確認 |
| `vivianlin0529-coder/taiwan-chip-wave-screener` | 籌碼集中度 + 法人 + 資券比 + 黃金回撤的加權評分 |
| `hu0937/FinPilot` | 多因子自動探索、三關驗證、防未來函數（公告日對齊） |
| `kevin801221/stock-strategies-only` | 基本面+技術面+夜盤期貨、GitHub Actions 自動化 |

---

## 免責聲明

本專案為**研究與學習用途**，所有回測結果僅供參考，不構成投資建議。回測有效不代表未來有效，實盤請自負風險。
