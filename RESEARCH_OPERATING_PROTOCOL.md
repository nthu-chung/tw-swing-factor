# 台股波段研究作業規範

本文件定義 `tw-swing-factor` 接下來的研究節奏。目標不是每天產生一個
看起來很聰明的股票清單，而是持續累積可以被反證、重跑與稽核的市場觀察。

## 1. 分開兩條資料軌

### Frozen research snapshot

- 用於回測、因子比較與論文結果。
- 每次實驗固定資料截止日、universe 定義、參數與 benchmark。
- 不因每日新資料自動改寫歷史結果。
- 新聞、營收與事件只能在實際可得時間之後進入特徵。
- 未還原價一律 fail-closed，不論斷點掃描有無命中；不得刪一筆交易後繼續宣稱。

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

## 9. 程式強制機制（2026-07-24 系統稽核後上線）

以前這份規範只是「文件承諾」，程式並未強制。以下修正讓 §1、§6、§7 由程式**硬性強制**：

| 規範承諾 | 強制點（程式） | 行為 |
|---|---|---|
| 未還原價 fail-closed（§1/§6） | `backtest._assert_price_integrity`（`_prepare_panel` 與外部注入 picks 兩條路都擋） | 未還原價 → **一律 raise 拒跑**，並寫 `outputs/price_integrity_audit.csv` 當診斷。斷點掃描**不是**放行條件：除息缺口 3~5% 在 ±10% 漲跌停帶內，掃描看不到，命中 0 筆不代表乾淨 |
| 逃生門（僅 smoke） | `SWING_ALLOW_UNADJUSTED=1` | 放行但 `summary.data.integrity_bypassed=True`，結果不得當已驗證 |
| Frozen snapshot 不漂移（§1） | `data._cache_path` 把 `SNAPSHOT_END_DATE` 編進檔名 + `_load_cache` 裁超過快照的列 | 改 cutoff → cache miss → **真重抓**；不再靜默回舊/未來資料 |
| 訊號時間 vs 可成交時間（§6） | `_check_exit` 的 `pending_ma_exit` | 收盤跌破 MA → **下一交易日開盤**成交，非當根收盤 |
| 法人資料可得時間（§6） | `factors._align` 法人四欄精確日對齊補 0 | 無申報日 flow=0，不向後延用舊值 |
| 上市/上櫃/ETF 覆蓋（§1/§6） | `current_watchlist._regular_equity_mask` | live screen 與 market_flow 排除 00 開頭 ETF |
| 族群分類隨時間改變（§6） | `universe_meta.industry_pit=False` + `industry_asof` | 回測 metadata 明示產業分類非 PIT |
| CI 下界>0 ≠ edge（§7） | `validate_oos` beta-aware verdict + 寫入 buy&hold 基準 | OS 普漲時自動降級結論，不再無條件宣稱「維持上線合理」 |
| 凍結必須凍到全部規則（§8） | `freeze_manifest`：反向 allowlist（config 大寫參數預設全凍，要排除得寫進 `NOT_FROZEN` 並附理由）＋ `strategy_kit/spec.py` 的 `StrategySpec` | 手維護 `FROZEN_KEYS` 只列 34 個、config 有 92 個 → 改 `BT_ORDER_SIZE_MODE`／處置模型／IS-OS 切割，hash 一個字都不會變；S19 的 10 檔／20 日更是在 manifest 之後才寫進 config，完全沒被凍 |
| 凍結版本不可冒充（§8） | `freeze_manifest.validate_manifest` + `apply_rules` fail-closed；label 進檔名不進 hash | legacy／不完整／被改過的 manifest 不得被 forward 使用；同日不同 label 不再互相覆寫 |
| 因子必須在稠密 panel 上算（§6） | 公開入口 `backtest.build_research_panel()` 預設稠密（`members_only=True` 只給純橫斷面統計）＋ `factor_engine/panel_density.py` 的標籤；`PanelOps` 的 `ts_*` 在 `members_only` panel 上 raise | long panel 的 `rolling(20)` 算的是「20 **列**」：間歇進出 universe 的股票會橫跨 60+ 個日曆日。`rotation_research` 的 `breakout_20`／量比／`positive_day_share_20` 正是這樣算出來的（訊號翻轉約 3%、命中率相對灌水約 +9.6%），2026-08-15 修為「稠密算因子、選股才套成員資格」 |
| forward 不得挑相位／缺基準（§7） | `forward_test.run` 走策略單元的全相位掃描 + 等權基準 + 不可覆寫的輸出 | 舊版只跑單一相位、吃引擎預設 `rebalance_every=5/top_n=3`、沒有基準、每次重跑覆寫同名檔 |
| 相位掃描只有一份實作（§7） | `evaluation/phases.py` 的 `sweep_phases()`／`PhaseSweep.stats()`：正式 IS/OS（`backtest.run_full`）、`s19.evaluate_sweep` 與 `forward_test` 共用；`tests/test_phase_sweep.py` 用 AST 掃描禁止再手寫 `for phase in range(...)` | 原本三份各自為政：`run_full` 掃 `rebalance_every`（CLI 預設 5）、S19 掃 20、`forward_test` 自己第三份聚合，MaxDD 欄名還分成 `max_drawdown`／`max_dd` |
| 結果必須自帶 provenance（§6/§7） | `backtest_portfolio` 的 `summary`：`params.factor_weights`／`params.strategy`（`StrategySpec`）／`universe` 的 PIT rule・pool size・**真實** pool as-of・dynamic 設定／`data` 的 dataset・自建還原・`integrity_bypassed`／`universe.future_pool_bypassed`／漲跌停・處置・張數・成本／`evaluation` 的 IS・embargo・OS 固定日期／phase／`provenance.git_state()`／`eval_audit` | 過去 pool as-of 取的是**每日 top-N（100）**那份檔案，而實際候選池是 top300：top100 的 `as_of=2026-06-20`（≤ 快照，看起來合規）、top300 的 `as_of=2026-08-03`（未來池），同一份 metadata 的 `candidate_source` 卻寫著 top300。`SWING_ALLOW_FUTURE_POOL` 只 print 不留欄位；`FACTOR_WEIGHTS` 與 git commit 完全不在結果裡 —— 換一組權重重跑，兩份報告分不出來 |
| 全域參數改寫必須還原（§6） | `market_filter_eval` / `regime_strategy_lab` 的 try/finally 完整還原；summary 另記 `market_filter.config_rule` 等**實際生效值** | `regime_strategy_lab` 只還原 `MARKET_FILTER_ENABLED`，把 `MARKET_FILTER_RULE` 永久留在 `vol`；`market_filter_eval` 收尾用 `_set_filter(*orig)`，而它在 `enabled=False` 時不碰 rule/weight，跑完停在 `('ma60', 0.5)` —— 同 process 後續回測全帶著別人的參數 |
| holdout 只有第一次是 holdout（§1/§7） | `evaluation/holdout.py` 的 append-only 台帳 `outputs/holdout_ledger.jsonl`（雜湊鏈防靜默改寫 + 排他檔案鎖）；`backtest.run_full` 的 OS 段、`s19.main`、`forward_test.run` 每次揭露都 append：strategy hash、OS 起訖、reveal time、git commit | 重疊到看過的區間 → `holdout_previously_seen=True`、`fresh_oos_claim_allowed=False`，並回報 `fresh_os_start`。原本完全沒有這個紀錄，而 IS/OS 切點錨在資料尾端、資料視窗又隨 `SNAPSHOT_END_DATE` 滑動：快照 2026-06-22 的 OS 是 2025-11-19~2026-06-18，推進到 2026-08-06 後 OS 起點變成 2026-01-05 —— 2025-11-19~2026-01-04 **從 OS 變成 IS**，同一段資料會被第二次報成 fresh OOS |
| 凍結必須釘住 holdout 邊界（§8） | `freeze_manifest.holdout_boundaries()` 寫進 `manifest["holdout"]`（不進 `rules`／hash）；`validate_manifest` 缺這段即判不可靠，未解析成日期時出警告 | 只凍 `EVAL_SPLIT_MODE`／`IS_OS_SPLIT`／`EMBARGO_DAYS` 這些**參數**是不夠的：同一組參數在不同快照下解出不同的 OS 區間 |
| 單相位只能 debug（§7） | `single_phase_debug` 由呼叫端的**意圖**傳入並標進 summary／`phase_stats`；forward 收到 debug 掃描直接 raise | 舊版 `forward_test` 用 `len(df) == 1` 反推旗標 —— 拿結果當意圖：20 相位只有 1 個有結果會被誤標成 debug，再平衡天數為 1 的正式全相位掃描也會被誤標 |
| 基準與個股同報酬口徑（§1/§7） | `data/return_convention.py` 是口徑的單一判定入口；`summary["return_convention"]` 記錄兩條序列各自含不含息，不一致直接 raise；`rotation_research.benchmark_metrics` / `market_relative_metrics` 改走 `return_convention.fetch_benchmark_index()`，含息指數抓不到就 raise（**不退回價格指數**） | 個股序列在自建／官方還原價下是**含息**的（`price_adjust` 的比值回溯等同除息日股利再投入），基準卻一直是 TAIEX **價格指數**（不含息）。實測 2024-06-03~2026-06-20：價格指數算術年化 42.38%／Sharpe 1.677，含息報酬指數 45.23%／1.790 —— **每年 2.86pp 的假超額、Sharpe 差 0.113**；2015~2026 逐年差 2.41~4.81pp 且沒有一年為負。這個量級剛好落在「看起來像小 alpha」的區間，所以只印警告沒有用 |

## 10. 標準操作流程（指令級）

> 先決定**價格路線**：未還原價下主回測預設**被擋**。要嘛用還原價（真績效），要嘛開逃生門（僅 smoke）。

### A. 乾淨 clone 後重現既有結果
```bash
python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 1) 離線閘門(不需 token,不連網;CI 跑的就是這兩行)
PYTHONPATH=. .venv/bin/python -m unittest discover -s tests -p "test_*.py"
PYTHONPATH=. .venv/bin/python preflight.py

# 2) 正式回測的候選池走月頻 PIT,不需要 universes/build.py。
#    第一次要補齊交易所逐日快照(之後逐日快取重用):
.venv/bin/python -c "import pit_universe as p; p.load_history('2024-06-01','2026-06-22')"

# 3) legacy static 對照組才需要 outputs/universe_top*.json(已進版控的 fixture)。
#    要重建:
.venv/bin/python universes/build.py 300
```
不寫死「應 N passed」——測試數會隨新增回歸測試變動，用**綠燈**而不是數字當判準。

### B. 推進研究窗（換資料）
```bash
export SWING_SNAPSHOT_END=2026-07-31        # 改截止日 → cache 自動 miss → 真重抓
.venv/bin/python universes/build.py 300      # 重建候選池(注意仍非 PIT,見 §6)
.venv/bin/python data/prefetch.py                # 依需要重抓
```

### C. 跑研究回測 / OOS / factor audit（三選一價格路線）
```bash
# 路線1(推薦、真績效):還原價
export SWING_PRICE_DATASET=TaiwanStockPriceAdj   # 需 FinMind 付費/sponsor token
.venv/bin/python main.py backtest --pool 300

# 路線2 已移除:當年跑它的 validate_oos.py 於 2026-08-16 隨 legacy 研究鏈刪除。
# 未還原價逃生門本身仍在(SWING_ALLOW_UNADJUSTED),但沒有任何現行入口該用它。

# 不設任何一個 → 未還原價一律 fail-closed raise(這是預期行為,不看斷點掃描結果)
```

### D. 要「宣稱」一個策略前（唯一能升級到 Clean OOS/Forward-only 的路）
```bash
.venv/bin/python freeze_manifest.py --strategy s19_chip_momentum --label <標籤>
#   → outputs/FROZEN_MANIFEST_<freeze_date>_<label>.json(immutable,不可覆寫)
#   凍的是「config 的每個 load-bearing 參數」+「策略的 StrategySpec」
#   (訊號視窗/權重 + 持股數/再平衡天數/MA 出場/停損)。label 只進檔名,不進 hash:
#   同一組規則換標籤仍是同一套規則(hash 相同),不同標籤不會互相覆寫。
# …之後推進 SNAPSHOT_END 累積凍結日後的新資料…
.venv/bin/python forward_test.py                       # 只驗證凍結後的 forward 窗
#   → outputs/forward_test_<freeze_date>_<label>_<hash>_<run_stamp>.json
#     + append-only outputs/forward_test_runs.jsonl(每次重跑都留紀錄,不可覆寫)
#     + append-only outputs/holdout_ledger.jsonl(揭露紀錄:誰在何時看過哪一段)
```

manifest 另外釘住 **holdout 邊界**(`manifest["holdout"]`):切割規則 + 有交易日曆時
解出來的 IS／embargo／OS 日期。只凍切割參數不夠 —— OS 區間錨在資料尾端,而資料
視窗隨快照滑動。要一起釘住日期就把全期回測的交易日曆傳進去:

```python
freeze_manifest.run("<標籤>", calendar=res["equity_curve"]["date"])
```
規則凍結後**不得回頭調參**（§8）。forward 交易數太少時持續累積，別急著下結論。

forward 是唯一能升級證據等級的路徑，所以它的閘門最嚴（2026-08-15 修）：

- manifest 必須是 `manifest_schema=3` 且通過 `freeze_manifest.validate_manifest`。
  legacy 格式、缺 load-bearing 參數、缺策略規格、缺 holdout 邊界、或 `rules` 被事後
  改過（hash 對不上）一律 **raise 拒用**，不得冒充可靠凍結版本。
- 套用凍結規則時 config 若已無該參數（改名/移除）→ raise。舊版是 `if hasattr` 靜默
  略過，等於那個凍結值再也沒被套用，而 forward 仍宣稱自己跑的是凍結規則。
- 候選池走策略的 `build_panel()` → `universes.historical_pit_universe()`；每個相位的
  `summary.universe.candidate_pool_pit` 必須為 True、`days_beyond_last_pick` 必須為 0。
- **跑滿所有等價相位**（相位數 = 凍結的再平衡天數），輸出中位數／最小值／最差 MaxDD，
  並附等權買進持有基準；算不出基準就 raise（和零比沒有意義）。掃描與聚合都走
  `evaluation/phases.py`，和正式 IS/OS 是同一份實作；「最差 MaxDD」= 所有相位裡
  最糟的那一個（帶號取 min，不是中位或平均）。
- 掃描帶 `single_phase_debug=True`（只跑 phase 0）時 forward **直接 raise**：單相位
  是一條路徑不是分布，同一訊號換相位 Sharpe 實測從 -0.09 擺到 +1.09。
- 每次成功的 forward 都會 append 進 `outputs/holdout_ledger.jsonl`。窗與已看過的
  區間重疊時**不擋**（重現既有結果是正當需求），但結果會標 `fresh_oos=False`、
  `holdout_previously_seen=True`，並在 `evidence_note` 註明只能當重現 ——
  **這種結果不得被引用成新的樣本外證據**。台帳被事後改寫時讀取端直接 raise。

### E. 每次宣稱前的例行稽核
```bash
.venv/bin/python universe_bias_audit.py     # 量化候選池倖存者/選池前視偏誤上界
# 檢查 summary.data.integrity_bypassed 是否為 True、universe.industry_pit 是否 False
```

### F. 尚未修好的（仍是揭露、非強制）——升級結論前必須先處理
- **候選池倖存者（已部分修復）**：正式回測改走兩層 PIT——`universes/monthly_pit.py`
  用完整 M-1 曆月的交易所逐日快照建 M 月候選（含當時在市、後來下市者），再由
  `universes/dynamic.py` 在池內依截至訊號日的 ADV20 排每日 top-N。因此
  `candidate_membership_survivorship_free=True`。
  **但 `price_history_survivorship_free` 仍為 `False`**：下市股的完整價格序列可能缺，
  所以整體 `survivorship_free` 維持 `False`。`universes/build.py` 的
  `outputs/universe_top*.json` 現在只供 legacy static 對照（`--static-universe`），
  **不得回套歷史**（偏誤上界見 `UNIVERSE_BIAS_REPORT.md`）。
  這條界線現在由結構強制、不再靠自律：正式歷史候選池的入口是
  `universes.historical_pit_universe()`；`backtest` 的 dynamic 正式回測沒有
  `universe_provider` 就 raise，legacy 單日池必須顯式帶
  `static_universe_comparator=True`，且結果會被標記
  `candidate_pool_pit=False` / `formal_evidence_eligible=False`。
  下列腳本目前仍是 research-only 的靜態池對照（其數字不可作正式證據）：
  `factor_audit.py`、`factor_scan.py`、`rotation_research.py`、`defensive_rs.py`、
  `regime_strategy_lab.py`、`market_filter_eval.py`、`experiment_weights.py`、
  `validate_oos.py`、`evaluate_dynamic_universe.py`、`sector_rotation.py`、
  `strategies/s19_chip_momentum.build_panel(use_pit_pool=False)`。
- **還原價**：預設仍未還原；`data/price_adjust.py` 自建只處理除權息，不含分割／減資。
  真績效需 `TaiwanStockPriceAdj` 全量重抓後重跑所有報告。未還原價現在一律
  fail-closed raise（§9），不是警告。
- **報酬口徑不一致（2026-08-15 發現並修正機制，既有超額結論一律需重驗）**：
  個股序列含息、基準用 TAIEX 價格指數（不含息），差額被當成策略的超額報酬。
  實測量級 **2.86pp/年、Sharpe 0.113**（2024-06-03~2026-06-20；逐年 2.41~4.81pp，
  2015~2026 沒有一年為負）。因此**本 repo 所有既有的「超額報酬 / 贏過大盤指數 /
  ann_alpha / relative_wealth」結論,在用一致口徑重跑之前一律視為需重驗**——
  這是標記，不是重新評價：不得因此宣稱任何策略變好或變壞，也不得為了證明修正
  有效而重跑績效。走等權買進持有基準（`s19.equal_weight_baseline`、`forward_test`）
  的結論不受此影響：那條基準直接從個股 close 算，必然同口徑。
  含息指數（`TaiwanStockTotalReturnIndex`）**需 FinMind level 2**，免費層取不到時
  正確行為是 fail-closed，不是換一把尺繼續比。
- **口徑修正的已知殘留**：`factor_engine.legacy_factors._attach_relative_strength`
  的 `rs_excess` / `down_day_excess` 仍以價格指數為基準，含息個股序列下
  `score_rs`（門檻型分數）被系統性灌高。刻意未一起改：因子層改了之後每次建 panel
  都需要付費層的含息指數，免費 token 會完全無法建 panel。這一項寫在
  `summary["return_convention"]["known_residuals"]`，升級任何用到 RS 因子的結論前
  必須先處理。市場濾網／regime 的 MA、波動判定沿用價格指數則是刻意的——那是水準值
  規則，不是報酬比較。另外 `disposition_event_study.py` 的「超額 = 個股 − TAIEX」
  兩邊**都**是不含息（它直接 `read_pickle` 快取、繞過 `fetch_price` 的自建還原），
  口徑碰巧一致，但那是繞過還原路徑的副作用而不是設計——動它之前要連還原價一起處理。
- **產業分類非 PIT**：歷史日期套用當前 FinMind 標籤，族群策略保留 `industry_pit=False`。
- **單一多頭窗**：資料僅 2024–2026，無足夠空頭；clean OOS 檢定力低，靠 forward-only 累積。
- 在上述各項處理完之前，**所有絕對績效一律標「樂觀上界、待重驗」**，不得作為上線依據。
  已標為 historical/invalid 的舊報告不得因為閘門修好而回收再用——**修好閘門不等於
  重新證明策略**，要重新宣稱就得重跑。
