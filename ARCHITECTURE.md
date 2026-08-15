# 系統架構與搬遷邊界

本 repo 的核心產品是「可稽核的台股 long-only 量化選股系統」。自動下單不在目前
範圍內；實際投資流程在人工決策前停止。

## 兩條明確分離的流程

```text
研究／回測：資料 → PIT universe → fields → operators → strategy
           → 目標持股 → execution 模擬 → backtest／IS-OS 評估

每日人工流程：資料 → PIT universe → fields → operators → strategy
             → 量化候選清單 → AI 基本面／新聞研究 → 人工決策與手動下單
```

`execution/` 只屬於第一條流程。它讓回測回答「這個訊號當時是否可能成交、成本與
限制是什麼」，不會連券商 API，也不會替使用者送出訂單。每日人工流程可以引用同一
套規則產生警示，例如漲停、處置、全額交割，但輸出仍只是候選資料。

## 七個不可協商的管線不變式

這些不是風格偏好，每一條都對應一個實際發生過、會產生假結果的缺陷。改動任何一層
之前先確認沒有破壞它們。

| # | 不變式 | 強制點 | 沒有它會怎樣 |
|---|---|---|---|
| 1 | **候選池只用完整的上一曆月** | `universes/monthly_pit.py`（`candidate_rule=month_M_uses_only_calendar_month_M_minus_1`）；逐日快照缺任何平日即 fail-closed。入口為 `universes.historical_pit_universe()`，引擎邊界 `backtest._resolve_universe_source` 在 dynamic 正式歷史回測沒有 provider 時 raise（不再從 `symbols is None` 推測意圖）；legacy 單日池要顯式 `static_universe_comparator=True`，結果標 `formal_evidence_eligible=False` | 當月行情或今天的熱門名單回頭改寫歷史成員；實測舊條件因為每個入口都會傳 `symbols=` 而從未觸發，預設其實是單日排名池回套歷史 |
| 2 | **每日 universe 只用截至訊號日的資料** | `dynamic_universe.add_membership`（ADV20 rolling 含當日、不含未來） | 成員資格偷看未來 |
| 3 | **因子在稠密 panel 上算** | 公開入口 `backtest.build_research_panel()`（**預設稠密**；`members_only=True` 只給純橫斷面統計，要顯式指定）。`_prepare_panel` 降為引擎內部函式並在 `panel.attrs["panel_density"]` 戳稠密度；`factor_engine/panel_density.py` 提供 `require_dense()`，`PanelOps` 的 `ts_*` 在 `members_only` panel 上 fail-closed raise（`cs_*`／`group_*` 照常放行）。成員過濾延到選股階段套 `in_dynamic_universe`；`strategies/` 禁止直接用 `_prepare_panel`（`tests/test_dense_panel_factors.py` 以 AST 掃描釘住） | `ts_` 的「20 列」橫跨 60+ 個日曆日，算子全面失真。實測 `rotation_research` 用預設稀疏 panel 算 `breakout_20`／`breakout_volume_ratio`／`positive_day_share_20`：突破訊號翻轉約 3%、命中率相對灌水約 +9.6%，而這三欄直接決定 `rotation_breakout` 的 eligible 與 `signal_score` |
| 4 | **field / operator 分界：有視窗才是 operator** | `factor_engine/data_fields.py` vs `factor_engine/operators.py` | 視窗長度被寫死，搜尋空間只涵蓋教科書版本 |
| 5 | **執行層是事件驅動，不是 `weights × returns`** | `backtest.py` 事件迴圈＋`execution/` | 表達不了路徑相依：一字漲停買不到、MA 跌破次日開盤才成交、處置期間禁新倉 |
| 6 | **價格完整性 fail-closed** | `backtest._assert_price_integrity` | 公司行動斷點被當成真實報酬（實測：-73.6% 的假 hard-stop，並改變「最佳」退場規則的選擇） |
| 7 | **快取 key 必須含所有影響內容的輸入** | `data.CacheScope`（dataset／stock_id／快照結束日／範圍戳；歷史型資料集少了範圍維度就 raise），舊格式檔一律視為 miss | 實測 `fetch_price('2330')` 與 `fetch_price('2330', history_days=2000)` 命中同一檔、回傳相同 482 列且零警告——「抓更長歷史（含空頭段）」變成靜默 no-op |

評估邊界另有兩條，屬於 `evaluation/`：IS／embargo／OS 由 `evaluation/splits.py`
單一入口建立且互不重疊（未來標籤視窗 > embargo 時拒跑）；每段**跑滿所有等價再平衡
相位**並報中位數、最小值與最差 MaxDD——同一訊號換相位，Sharpe 實測可以從 -0.09
擺到 +1.09，只報一條路徑等於挑路徑。強制點是 `evaluation/phases.py` 的
`sweep_phases()`／`PhaseSweep.stats()`：正式 IS/OS（`backtest.run_full`）、策略單元
（`s19.evaluate_sweep`）與 `forward_test.py` 共用**同一份**掃描與聚合，
`tests/test_phase_sweep.py` 以 AST 掃描禁止任何模組再手寫 `for phase in range(...)`。
「最差 MaxDD」的定義是**所有相位裡最糟的那一個**（帶號取 min，不是中位或平均）；
慣例翻成正值時直接 raise，因為那會變成回報最好的相位。單相位只能 debug：
`single_phase_debug` 由呼叫端的**意圖**決定並標進 summary（舊版 `forward_test`
用 `len(df) == 1` 反推，把「20 相位只有 1 個有結果」誤標成 debug、也會把再平衡
天數為 1 的正式全相位掃描誤標），forward 收到 debug 掃描一律 raise。引擎另有
`summary["eval_audit"]` 稽核評估窗上界，`days_beyond_last_pick` 必須為 0，
否則 IS 會借用 OS 的績效。

第三條屬於 freeze／forward：**凍結必須凍到全部規則**。強制點是
`freeze_manifest.py`（config 的大寫參數預設全凍，排除要寫進 `NOT_FROZEN` 附理由）
加上 `strategies/spec.py` 的 `StrategySpec`（訊號視窗／權重與持股數／再平衡天數／
MA 出場／停損）。沒有它會怎樣：手維護的 `FROZEN_KEYS` 只列 34 個而 config 有 92 個，
`BT_ORDER_SIZE_MODE`、漲跌停／處置模型、IS-OS／embargo 全部漏凍；S19 的 10 檔／
20 日更是在 manifest 產生**之後**才被寫進 config，改成 3 檔／5 日 `rules_sha256_16`
一個字都不會變。`forward_test.py` 只接受 `manifest_schema=2` 且通過
`validate_manifest` 的 manifest（legacy／不完整／被改過一律 raise），套用凍結規格後
跑滿所有相位、附等權基準，輸出不可覆寫並追加 append-only ledger。

兩層之間唯一的介面是 `picks_by_date`，所以因子層可以整層抽換而不動執行層。

## 目標模組邊界

> ⚠️ 這是**目標**狀態，不是現況。目前實際存在的套件只有 `universes/`、
> `factor_engine/`、`strategies/`、`execution/`、`evaluation/`；
> `market_data/`、`portfolio/`、`backtesting/`、`analyst_research/`、`research/`
> **尚未建立**，其責任目前仍散在根目錄的 `data.py`、`backtest.py` 與研究腳本裡。
> 下一節列出已完成的部分與搬遷順序。

| 模組 | 唯一責任 | 不應包含 |
|---|---|---|
| `market_data/` | 來源 adapter、快取、快照、公司行動與欄位正規化 | 策略分數、回測績效 |
| `universes/` | PIT 上市狀態、流動性資格、每日成員 | 使用期末名單回套歷史 |
| `factor_engine/data_fields.py` | 無可調視窗的衍生欄位 | RSI、ATR 等視窗參數 |
| `factor_engine/operators.py` | 因果的 ts/cs/group/elementwise 算子 | 資料抓取、策略權重 |
| `factor_engine/legacy_factors.py` | 現有傳統因子與分數，等待逐步改寫成算子組合 | 成交模擬 |
| `strategies/` | 訊號、硬閘門、排序與凍結策略參數 | 資料下載、券商下單 |
| `portfolio/` | 集中度、權重、再平衡與風險限制 | 交易所規則 |
| `execution/` | 台股成交可行性、價格合法化、成本與交割模擬 | Alpha 訊號、自動下單 |
| `backtesting/` | 事件迴圈、部位、現金、成交紀錄與權益曲線 | 選參數、資料抓取 |
| `evaluation/` | IS/embargo/OS、walk-forward、基準與穩健性統計 | 依 OS 修改策略 |
| `analyst_research/` | AI 基本面／新聞研究封包與稽核紀錄 | 靜默改寫量化排名 |
| `research/` | 尚未採用的實驗與負面結果 | 被 live 流程直接匯入 |

## 目前已完成的第一階段搬遷

- `universes/monthly_pit.py`：M 月只用完整 M-1 曆月建立候選池；缺逐日快照即停止。
- `universes/entry.py`：新策略取得候選池的最短路徑
  （`historical_pit_universe()` → `PITUniverse.backtest_kwargs()`）。
  `universe.get_research_candidates()` 的單日靜態池降級為顯式對照組。
- `factor_engine/operators.py`：正式算子實作。
- `factor_engine/data_fields.py`：從 operators 拆出的八個無視窗欄位。
- `factor_engine/panel_density.py`：panel 稠密度標籤與 `ts_`／rolling 的 fail-closed
  閘門（不變式 3 的第二道防線；預設安全來自 `backtest.build_research_panel()`）。
- `factor_engine/legacy_factors.py`：既有傳統因子。
- `evaluation/splits.py`：統一 IS/OS 切割。
- `evaluation/phases.py`：統一相位掃描與聚合（`sweep_phases` / `PhaseSweep` /
  `phase_stats`）。正式 IS/OS、策略 `evaluate_sweep` 與 forward 共用這一份；
  呼叫端只提供「一個相位怎麼跑」，掃滿與中位／最小／最差 MaxDD 由它負責。
- `strategies/spec.py`：可凍結的 `StrategySpec`（策略的全部可調參數）與策略註冊表；
  `freeze_manifest.py` 凍的就是它，`forward_test.py` 套回去的也是它。
- `strategies/s19_chip_momentum.py`：S19 策略單元；證據狀態仍是 blocked
  （參數改由 `SPEC` 提供，舊模組常數只是它的投影）。
- `execution/tradability.py`：回測使用的一字漲跌停與處置禁倉資料載入。
- `execution/taiwan_rules.py`：普通股 tick、精確 10% 漲跌停與首五日例外介面。
- `execution/costs.py`：研究小數股、整張、零股代理及券商成本。

根目錄的 `operators.py`、`factors.py`、`evaluation_split.py`、
`chip_momentum_strategy.py` 暫時保留為相容入口（薄轉發，不含邏輯；
`tests/test_package_migration.py` 用 `assertIs` 釘住它們指向新實作，
避免相容層悄悄長出第二份行為）。既有研究腳本不必在同一次搬遷全部修改；
新程式應直接使用新的套件路徑。

## 工程閘門（與研究正確性同層）

| 閘門 | 內容 |
|---|---|
| `.github/workflows/ci.yml` | Python 3.11、`pip install -r requirements.txt`、`compileall` 語法 smoke、`preflight.py`、`unittest discover`。**市場資料測試離線**：不設 `FINMIND_TOKEN`，所以任何誤走真實資料路徑的測試會 fail-closed；checkout 與依賴安裝本身仍需網路 |
| `preflight.py` | 對 **git 追蹤中**的檔案檢查密鑰檔名／私鑰內容、`_cache`／`outputs` 資料產物誤追蹤、必要公開文件、`.gitignore` 覆蓋、`.env.example` 空值。命中只印規則與行號，不印內容 |
| `tests/` | `unittest`（非 pytest）、離線、HTTP 全 mock。修完 bug 要留回歸測試並在 docstring 說明原 bug |

授權條款（LICENSE）**刻意未定**，由 repo owner 決定；`preflight.py` 將其列為
owner decision 而非失敗，稽核腳本不代替決定。

## 後續搬遷順序

1. 驗證並快取官方逐日 `reference_price / limit_up / limit_down`，取代一般日的
   `derived_prev_close`；新上市與轉板例外要接 PIT lifecycle。
2. 將其餘資料來源與快照搬到 `market_data/`；月頻 PIT provider 已先搬到
   `universes/`，舊的抓取／解析函式暫留 `pit_universe.py` 作相容層。搬遷時
   `data.CacheScope` 是快取檔名的唯一推導點——研究腳本要路徑請用
   `data.cache_scope()` / `data.cache_glob()`，不要自己拼字串（自己拼就是
   不變式 7 的下一次破口；舊快取加範圍戳用 `migrate_cache_range.py --apply`）。
3. 把 `backtest.py` 拆成 `backtesting/engine.py`、`portfolio/` 與 `execution/`。
   在成交紀錄 parity 測試通過前，根目錄引擎仍是唯一正式入口。
4. 最後才搬研究腳本。已證偽與 blocked 策略仍保留在策略台帳，不因整理資料夾而
   消失或改名成已驗證策略。
5. 建立 `analyst_research/` 的候選封包；AI 研究結果獨立保存，並用純量化 A 組對照
   「量化＋AI 篩選」B 組，未經 untouched OOS／forward 證明前不覆寫量化核心。
