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

## 九個不可協商的管線不變式

這些不是風格偏好，每一條都對應一個實際發生過、會產生假結果的缺陷。改動任何一層
之前先確認沒有破壞它們。

| # | 不變式 | 強制點 | 沒有它會怎樣 |
|---|---|---|---|
| 1 | **候選池只用完整的上一曆月** | `universes/monthly_pit.py`（`candidate_rule=month_M_uses_only_calendar_month_M_minus_1`）；逐日快照缺任何平日即 fail-closed。入口為 `universes.historical_pit_universe()`，引擎邊界 `backtest._resolve_universe_source` 在 dynamic 正式歷史回測沒有 provider 時 raise（不再從 `symbols is None` 推測意圖）；legacy 單日池要顯式 `static_universe_comparator=True`，結果標 `formal_evidence_eligible=False`。**external picks（S19 實際走的路徑）另有兩道結果層驗證**：`_verify_external_picks_are_pit()` 把 `picks_by_date` 攤平後逐列比對 `provider.candidate_mask()`，有 pick 落在當日候選池外就 raise、provider 沒涵蓋該日就把 `candidate_pool_pit` 降為 False；沒附 `strategy_spec` 時強制 `formal_evidence_eligible=False`（訊號規則無 provenance） | 當月行情或今天的熱門名單回頭改寫歷史成員；實測舊條件因為每個入口都會傳 `symbols=` 而從未觸發，預設其實是單日排名池回套歷史。第二次破口同型：只要「傳了 provider 物件」就蓋 PIT 章，而唯一的檢查 `symbols ⊆ all_symbols` 是**跨全期聯集**——實測三月只買二月才在池裡的股票，summary 仍是 `candidate_pool_pit=True` 且 `formal_evidence_eligible=True` |
| 2 | **每日 universe 只用截至訊號日的資料** | `dynamic_universe.add_membership`（ADV20 rolling 含當日、不含未來） | 成員資格偷看未來 |
| 3 | **因子在稠密 panel 上算** | 公開入口 `backtest.build_research_panel()`（**預設稠密**；`members_only=True` 只給純橫斷面統計，要顯式指定）。`_prepare_panel` 降為引擎內部函式並在 `panel.attrs["panel_density"]` 戳稠密度；`factor_engine/panel_density.py` 提供 `require_dense()`，`PanelOps` 的 `ts_*` 在 `members_only` panel 上 fail-closed raise（`cs_*`／`group_*` 照常放行）。成員過濾延到選股階段套 `in_dynamic_universe`；`strategies/` 禁止直接用 `_prepare_panel`（`tests/test_dense_panel_factors.py` 以 AST 掃描釘住） | `ts_` 的「20 列」橫跨 60+ 個日曆日，算子全面失真。實測 `rotation_research` 用預設稀疏 panel 算 `breakout_20`／`breakout_volume_ratio`／`positive_day_share_20`：突破訊號翻轉約 3%、命中率相對灌水約 +9.6%，而這三欄直接決定 `rotation_breakout` 的 eligible 與 `signal_score` |
| 4 | **field / operator 分界：有視窗才是 operator** | `factor_engine/data_fields.py` vs `factor_engine/operators.py` | 視窗長度被寫死，搜尋空間只涵蓋教科書版本 |
| 5 | **執行層是事件驅動，不是 `weights × returns`** | `backtest/event_backtest.py` 事件迴圈＋`execution/` | 表達不了路徑相依：一字漲停買不到、MA 跌破次日開盤才成交、處置期間禁新倉 |
| 6 | **價格完整性 fail-closed** | `backtest._assert_price_integrity` | 公司行動斷點被當成真實報酬（實測：-73.6% 的假 hard-stop，並改變「最佳」退場規則的選擇） |
| 7 | **快取 key 必須含所有影響內容的輸入** | `data.CacheScope`（dataset／stock_id／快照結束日／範圍戳；歷史型資料集少了範圍維度就 raise），舊格式檔一律視為 miss。視窗由呼叫端指定的全市場表（處置／注意）用 `data.window_cache_scope()`（戳 `w{start}_{end}`）；讀取端 `execution.tradability` 必須自己確認快取涵蓋回測區間，涵蓋不到就當缺資料 fail-closed。衍生的稽核 panel 快取（`factor_audit.panel_cache_path` / `defensive_rs.panel_path`）檔名帶快照與 `HISTORY_DAYS` | 實測 `fetch_price('2330')` 與 `fetch_price('2330', history_days=2000)` 命中同一檔、回傳相同 482 列且零警告——「抓更長歷史（含空頭段）」變成靜默 no-op。同一個洞在 `data/__init__.py` 之外重演過一次：處置快取只以快照為 key，先放一份只涵蓋 2026-05-01~05-10 的檔，再請求 2021-01-01~2026-06-22 會零重抓直接回傳那一列——而這層資料決定「處置期間禁新倉」，等於把更早期間全部當成沒被處置而放行進場 |
| 8 | **只有上市／上櫃普通股能進候選池** | `security_type.py` 的證券別白名單，由 `universe.get_universe` / `pit_universe.load_history*` / `current_watchlist` / `build_universe.build` 共用（引擎邊界 `_prepare_panel` 再擋一次非普通股產業別）；判準來自 TaiwanStockInfo 的 `type` + `industry_category` + `stock_name` 後綴，缺任一欄或出現沒見過的產業別一律 fail-closed（`on_unknown` 只有 `raise`／`exclude`，刻意沒有 `allow`）；被擋掉的數量與理由寫進 `summary["universe"]["excluded_by_security_type"]` | `universe._is_normal_stock(stock_id, market_type)` 收了 `market_type` 卻**完全沒用它**，只檢查「4 碼數字非 00 開頭」——而興櫃、DR（91xx）、創新板的代號同樣是 4 碼數字。實測凍結快照下舊規則放行 2509 檔、其中 408 檔不是上市櫃普通股（興櫃 369／創新板 28／DR 11）；PIT 逐日快照混進 28 檔創新板 + 4 檔 DR，`outputs/universe_top100.json` 也含 1 檔創新板。**興櫃沒有 ±10% 漲跌停**：2026-05 單日 |ret|>10.5% 佔比為上市 0.034%／上櫃 0.042%／興櫃 3.872%（約 100 倍），最大 +57.17%；動能因子找的正是那種標的，偏誤方向是系統性灌高 Sharpe。流動性擋不住——最大一檔興櫃日均成交值 14.75 億、全市場 ADV 排名 #188，落在 `DYNAMIC_UNIVERSE_CANDIDATE_POOL=300` 之內 |
| 9 | **基準與個股序列同報酬口徑（含息 vs 不含息）** | `data/return_convention.py` 是唯一判定入口（由 `PRICE_DATASET` + `SELF_ADJUST_PRICES` 推導個股口徑，再決定基準指數：含息 → `TaiwanStockTotalReturnIndex`、不含息 → `TaiwanStockPrice`/TAIEX）；`summary["return_convention"]` 記兩條序列各自的口徑，不一致直接 raise（`config.BENCHMARK_INDEX_DATASET` 顯式指定不一致的基準也 raise）；`rotation_research` 的基準與 alpha/beta 走 `fetch_benchmark_index()`，含息指數取不到就 raise，**不退回價格指數** | 個股在自建／官方還原價下是含息序列（`price_adjust` 的比值回溯等同除息日股利再投入），基準卻是 TAIEX 價格指數 → 差額全部變成假超額。實測 2024-06-03~2026-06-20：價格指數算術年化 42.38%／Sharpe 1.677 vs 含息 45.23%／1.790，**2.86pp/年、Sharpe 0.113**；2015~2026 逐年差 2.41~4.81pp 且沒有一年為負。量級剛好落在「看起來像小 alpha」的區間，只印警告會被捲過去 |

評估邊界另有兩條，屬於 `evaluation/`：IS／embargo／OS 由 `evaluation/splits.py`
單一入口建立且互不重疊（未來標籤視窗 > embargo 時拒跑）；每段**跑滿所有等價再平衡
相位**並報中位數、最小值與最差 MaxDD——同一訊號換相位，Sharpe 實測可以從 -0.09
擺到 +1.09，只報一條路徑等於挑路徑。強制點是 `evaluation/phases.py` 的
`sweep_phases()`／`PhaseSweep.stats()`：正式 IS/OS（`backtest.run_full`）、策略單元
（`s19.evaluate_sweep`）與 `forward_test.py` 共用**同一份**掃描與聚合，
`tests/test_phase_sweep.py` 以 AST 掃描禁止任何模組再手寫相位迴圈——掃的是**行為**
不是寫法：迴圈變數叫 phase／ph／rebalance_phase，或**任何**會重複執行的 body
（`for`／`while`／推導式）直接餵 `rebalance_phase=`，都算違規（巢狀 `def` 裡的
單相位 callback 不算，`run_full` 的 `_run_phase` 就長在 `for segment` 迴圈裡）。
舊版只認「迴圈變數名」與「`for ... in range(...)`」兩種形狀，
`offsets = list(range(n))` + `for off in offsets:` 或改寫成 `while` 就整份繞過去。
「最差 MaxDD」的定義是**所有相位裡最糟的那一個**（帶號取 min，不是中位或平均）；
慣例翻成正值時直接 raise，因為那會變成回報最好的相位。單相位只能 debug：
`single_phase_debug` 由呼叫端的**意圖**決定並標進 summary（舊版 `forward_test`
用 `len(df) == 1` 反推，把「20 相位只有 1 個有結果」誤標成 debug、也會把再平衡
天數為 1 的正式全相位掃描誤標），forward 收到 debug 掃描一律 raise。forward 另外
在**結果層面**比對相位數：`sweep.n_phases_full` 必須等於凍結的 `rebalance_days`
且 `sweep.full_sweep` 為 True，否則 raise——否則「掃滿」只是策略模組的自律
（實測把 `evaluate_sweep` 換成只掃 3 個相位，forward 會完整跑完並寫出 payload）。引擎另有
`summary["eval_audit"]` 稽核評估窗上界，`days_beyond_last_pick` 必須為 0，
否則 IS 會借用 OS 的績效。

第三條評估邊界是 **holdout 只有第一次是 holdout**：`evaluation/holdout.py` 是
append-only 的揭露紀錄（`outputs/holdout_ledger.jsonl`）。`backtest.run_full`
的 OS 段、`s19.main` 與 `forward_test.run` 每次跑出 OS／forward 數字就 append
一列，記 strategy hash、OS 起訖、reveal time 與 git commit；重疊到看過的區間就
標 `holdout_previously_seen=True`、`fresh_oos_claim_allowed=False`，並回報
`fresh_os_start`（這次真正還沒被看過的起點）。沒有它會怎樣：IS/OS 切點完全由
凍結資料自身的首尾日決定（`splits.py` 錨在 `dts[-1]`），而資料視窗兩端隨
`SNAPSHOT_END_DATE` 滑動（`start = end - HISTORY_DAYS`）——實測快照 2026-06-22
的 OS 是 2025-11-19~2026-06-18，推進到 2026-08-06 之後 OS 起點變成 2026-01-05，
**2025-11-19~2026-01-04 從 OS 變成 IS**，同一段資料會被第二次當成 holdout 報成
fresh OOS。五個設計點各對應一種會讓揭露紀錄失效的失敗模式：比對用**區間交集**
（滑動窗永遠不會日期字串相等）、每列帶 `prev_sha256` 形成雜湊鏈（既有列被靜默
改寫或抽掉就讀不出來——這正是它存在的意義）、寫入時取排他檔案鎖（併發揭露不會
雙方都讀到空揭露紀錄而各自宣稱 fresh）、另存一份**長度指紋**
`holdout_ledger.jsonl.checkpoint.json`（列數＋末列 `record_sha256`；雜湊鏈只在
檔案還在時有意義，實測 `os.remove(ledger)` 之後同 hash 同窗立刻回報 fresh、零
警告——列數倒退或末列對不上一律 fail-closed），以及 `fresh_oos_claim_allowed`
的**雙口徑**：`holdout_previously_seen` 只看同一個 `strategy_hash`（那是「這套
規則重現過嗎」），另外報不分規則的
`window_previously_revealed_any_rules`／`window_reveal_count_any_rules`，兩者**都**
沒看過才允許宣稱 fresh OOS。沒有第二個口徑會怎樣：規則 hash 涵蓋 79 個 config
參數，而參數研究迴圈正是消耗 holdout 的主要途徑——實測同一段 OS 用 H1 揭露後，
只把 `config.BBANDS_K` 從 2.0 改成 2.5（S19 與 FACTOR_WEIGHTS 都不讀它）重算
hash，同一段 OS 就回報 `fresh`、`fresh_oos_claim_allowed=True`。揭露紀錄**刻意不放
績效數字**：它回答「這段未來資料被誰看過幾次」，`outputs/forward_test_runs.jsonl`
才記「那次跑出什麼」，兩份用 `strategy_hash`／`output` 對照、語意不重疊。揭露紀錄
上線前就已消耗的 holdout（S19 的 OS）寫在 `KNOWN_CONSUMED_HOLDOUTS` 常數而不是
某台機器的 jsonl——狀態只存在檔案裡的話，換一台 clone 就變回 clean。同理，這兩份
jsonl（與那份指紋）是**稽核紀錄不是資料產物**，已加進 `.gitignore` 例外與
`preflight.OUTPUT_ALLOWLIST`，可以進版控、刪掉會在 git status 看得見。
覆蓋範圍要誠實：目前只有上述三個入口入帳，`validate_oos.py`、`factor_scan.py`
等 research-only 腳本仍未接（它們的 OS 本來就標成 pseudo-OOS）。

回測 `summary` 是這條流程的**可稽核產出物**：一個數字必須自己說得出它是怎麼算
出來的，否則報告寫下去之後沒有人能重建它。強制內容為 factor weights（
`params.factor_weights` 與 `factor_weights_applied`）、全部策略與投組參數（含
`params.strategy` 的 `StrategySpec`）、PIT candidate rule／pool size／**真實**
pool as-of、dynamic universe 設定、price dataset 與自建還原及
`data.integrity_bypassed`、`universe.future_pool_bypassed`、
`universe.excluded_by_security_type`（被證券別白名單擋掉的檔數與理由——修正證券別
過濾會改變候選池組成，沒有這一欄就分不出兩份結果用的是哪一種池；這一欄只算**本次
request**，不跨回測累積，否則第二次回測會借到第一次的排除數）、
`return_convention`（個股序列與基準序列各自含不含息、基準用哪個指數資料集，以及
仍以價格指數為基準的 `known_residuals`——沒有這一欄，「超額報酬 +X%」事後無從判斷
分子分母是不是同一把尺）、漲跌停／處置／
張數／成本設定、`evaluation` 的 IS／embargo／OS 固定日期、phase、
`provenance.git_state()` 的 git commit，以及 `eval_audit`。兩個實測過的失真點：pool as-of 曾經取
`build_universe.load_asof(universe_top_n)`，那是**每日 top-N（100）**那份檔案，
而真正套進歷史的是 top300——top100 的 `as_of=2026-06-20`（≤ 快照 2026-06-22，
看起來合規）、top300 的 `as_of=2026-08-03`（未來池），同一份 metadata 的
`candidate_source` 卻誠實寫 top300；現在 as-of 一律由「symbols 是哪一份池的子集」
決定，比對不到就記 `None`，不拿快照日頂替。`SWING_ALLOW_FUTURE_POOL` 過去只
print 一行就放行，現在會記事件並把 `formal_evidence_eligible` 降級。全域 config
被就地改寫的研究腳本（`market_filter_eval`、`regime_strategy_lab`）必須 try/finally
還原**全部**被改的參數；summary 另記 `market_filter.config_rule` 等實際生效值，
讓還原漏洞事後看得見。

第三條屬於 freeze／forward：**凍結必須凍到全部規則**。強制點是
`freeze_manifest.py`（config 的大寫參數預設全凍，排除要寫進 `NOT_FROZEN` 附理由）
加上 `strategy_kit/spec.py` 的 `StrategySpec`（訊號視窗／權重與持股數／再平衡天數／
MA 出場／停損）。沒有它會怎樣：手維護的 `FROZEN_KEYS` 只列 34 個而 config 有 92 個，
`BT_ORDER_SIZE_MODE`、漲跌停／處置模型、IS-OS／embargo 全部漏凍；S19 的 10 檔／
20 日更是在 manifest 產生**之後**才被寫進 config，改成 3 檔／5 日 `rules_sha256_16`
一個字都不會變。manifest 另外固定記錄 **holdout 邊界**（`manifest["holdout"]`：
切割規則 **加上解出來的** IS／embargo／OS 日期）——只凍切割**參數**是不夠的，
同一組參數在不同快照下解出不同的 OS。「有欄位」也不等於「有釘住」：`calendar=`
原本只是選用關鍵字而 CLI **沒有**對應選項，所以走正式路徑產出的 manifest 一律
`resolved=False`（`is_window`／`os_window` 全是 null），`validate_manifest` 只給
一個 warning、forward 印一行就照跑。現在 `freeze_manifest.run()`（CLI 的唯一路徑）
自己用 `trading_calendar()` 解日曆（離線，只讀 TAIEX 一條序列並裁到個股資料視窗，
不觸發全市場抓取），且 `resolved=False` 一律判 `ok=False`。這段刻意**不進
`rules`／hash**：解出來的日期是資料的函數，進 hash 會讓同一套規則在推進快照後
變成另一套規則（與 `SNAPSHOT_END_DATE` 同理）。`forward_test.py` 只接受
`manifest_schema=3` 且通過 `validate_manifest` 的 manifest（legacy／不完整／缺
holdout 邊界／邊界未解析成日期／被改過一律 raise），套用凍結規格後跑滿所有相位、
附等權基準，輸出不可覆寫並追加兩份 append-only 紀錄：執行紀錄
`forward_test_runs.jsonl` 與揭露紀錄 `holdout_ledger.jsonl`。凍結的策略參數是
**六個 load-bearing 的值**（mom／flow／vol 視窗、訊號權重、停損），`build_signal`
一律讀 `spec`、模組常數只是投影；`tests/test_freeze_forward.py` 正反兩面釘住
（改 spec 必須改變分數、改模組常數不得改變分數），停損／MA 出場／持股數則在
引擎呼叫的當下比對 `config` 實際值——那三個是 `_apply_portfolio_config()` 的
副作用，只驗簽章參數完全驗不到。

最後一條屬於**舊入口的邊界**：`screener.py`（每日人工流程的 live 入口）先從大盤
（TAIEX）序列解出**市場參考交易日**，`--date` 落在非交易日就退到最近一個有效交易
日；接著每檔股票必須在**那一天**有 bar 才算候選，只有更舊 bar 的（停牌／暫停交易／
已下市）排除並標 `stale_bar`，寫進回傳值的 `attrs["screen_diagnostics"]`。沒有它會
怎樣：舊版讓每檔各自取「自己最後一根 `<= as_of` 的 bar」，實測資料斷在 2026-04-01
的股票會出現在 `screen(as_of='2026-05-20')` 的候選裡，`date` 欄誠實寫著 2026-04-01
——停牌 7 週的股票用兩個月前的收盤價冒充當日候選，而同一份資料丟
`dynamic_universe.add_membership` 只會回另一檔。同一條線上的 legacy 流動性
pre-filter 也從 `price.tail(20)` 改成「截至參考日的 20 根」——回看模式下前者拿的是
資料末端的量，等於用未來人氣決定當時能不能被選。輸出檔名也改戳參考交易日而不是
`datetime.now()`（週末或盤前跑會產生以非交易日命名的檔案）。⚠ 技術債：「當日必須有
一根 valid bar」這條成員規則目前有**三份**獨立實作（`screener.py`、
`current_watchlist.build_screen`、`dynamic_universe.add_membership`），本次只補上
screener 缺的那半，收斂成同一份仍未做；改動任何一份時三份一起看。

`rotation_research.py` 維持 **exploratory research**：它自製的 positions／cash／MTM
迴圈（無一字漲停鎖、無處置禁倉、無整張／零股與券商成本，用的是小數股）**不會**升格
成正式引擎。要正式投組績效走同檔的 `formal_portfolio()`／`formal_portfolio_sweep()`
——把 `build_signal_table()` 的 picks 轉成 `picks_by_date` 餵進
`backtest.backtest_portfolio()`，`start_date`／`end_date` 一律往下傳（不然引擎會跑到
資料末端），相位掃描共用 `evaluation/phases.py`。候選池仍是 legacy 單日排名，所以
不傳 PIT `universe_provider` 時引擎會誠實標 `formal_evidence_eligible=False`。
直讀 `_cache` 原始價的事件研究（`disposition_event_study.py`、
`universe_bias_audit.py`）繞過還原價與完整性 fail-closed 閘門，維持 research-only
標記，不得升格成正式策略入口。

兩層之間唯一的介面是 `picks_by_date`，所以因子層可以整層抽換而不動執行層。

## 目標模組邊界

> ⚠️ 這是**目標**狀態，不是現況。目前實際存在的套件只有 `universes/`、
> `factor_engine/`、`strategies/`、`execution/`、`evaluation/`；
> `market_data/`、`portfolio/`、`backtesting/`、`research/`
> **尚未建立**，其責任目前仍散在根目錄的 `data/__init__.py`、`backtest/event_backtest.py` 與研究腳本裡。
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
| `research/` | 尚未採用的實驗與負面結果 | 被 live 流程直接匯入 |

AI 基本面、新聞評分、prompt、模型與人工決策紀錄不是本公共 repo 的目標模組。它們若
由 Project Owner 建立，會位於獨立私人專案，僅消費本系統凍結且可稽核的候選輸出；
不得反向修改量化分數或把事後資料送回歷史回測。

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
- `evaluation/holdout.py`：append-only 的 holdout 揭露紀錄（雜湊鏈 + 檔案鎖 +
  長度指紋 `*.checkpoint.json`，整檔刪除／截斷會 fail-closed）與
  `KNOWN_CONSUMED_HOLDOUTS`（揭露紀錄上線前就已消耗的 holdout，例如 S19 的 OS）。
  `rules_fingerprint()` 是規則雜湊的唯一實作，`freeze_manifest.rules_hash` 轉呼叫
  它——揭露紀錄的 `strategy_hash` 與 manifest 的 `rules_sha256_16` 必須是同一個東西。
- `provenance.py`：git 狀態的單一實作（回測 `summary["provenance"]` 與
  `freeze_manifest` 共用；dirty 工作樹 = 對不到 commit = 無法重現，必須看得見）。
- `security_type.py`：「哪些證券可以進池」的**單一判定**（上市／上櫃普通股白名單，
  排除興櫃／DR／創新板／ETF／ETN／受益證券／特別股）。`universe`、`pit_universe`、
  `current_watchlist`、`build_universe` 與引擎邊界共用這一份；證券別缺失時
  fail-closed（`on_unknown` 只有 `raise` / `exclude`，沒有 `allow`），排除統計進
  `summary["universe"]["excluded_by_security_type"]`。
  引擎的兩條**繞過 panel** 的外部訊號路徑（`picks_by_date` 與 policy 的
  `signal_frame`）在進引擎前也走同一份判定——只補 summary 擋不住任何東西
  （實測：DR 代號 9103 注入外部 picks 仍成功建倉，而排除數顯示 0）。
  排除統計是**每次 backtest request 自己的** `ExclusionCollector`
  （`exclusion_scope()` 用 contextvars 綁定），不是 process 全域累積；
  `exclusion_summary()` 降級成純觀察用途，不得當 summary 數字的來源。
- `strategy_kit/spec.py`：可凍結的 `StrategySpec`（策略的全部可調參數）與策略註冊表；
  `freeze_manifest.py` 凍的就是它，`forward_test.py` 套回去的也是它。
- `strategies/s19_chip_momentum.py`：S19 策略單元；證據狀態仍是 blocked
  （參數改由 `SPEC` 提供，舊模組常數只是它的投影）。
- `execution/tradability.py`：回測使用的一字漲跌停與處置禁倉資料載入。
- `execution/taiwan_rules.py`：普通股 tick、精確 10% 漲跌停與首五日例外介面。
- `execution/costs.py`：研究小數股、整張、零股代理及券商成本。
- `strategy_kit/position_policy.py`：`StrategyPositionPolicy` v1（下一節說明責任邊界）。

根目錄的 `operators.py`、`factors.py`、`evaluation_split.py`、
`chip_momentum_strategy.py` 暫時保留為相容入口（薄轉發，不含邏輯；
`tests/test_package_migration.py` 用 `assertIs` 釘住它們指向新實作，
避免相容層悄悄長出第二份行為）。既有研究腳本不必在同一次搬遷全部修改；
新程式應直接使用新的套件路徑。

## StrategyPositionPolicy 的責任邊界

`strategy_kit/position_policy.py` 是訊號層與事件引擎之間新增的一層。它存在的理由
不是「多一個抽象」，而是原本這三件事沒有地方擋：退出理由不可稽核（`exit_reason`
只有引擎內建那幾種，策略自己的「排名掉出去了」無處可放）、desired 與 realized
混為一談（跌停賣不掉、現金不夠只是「跳過」，回測看起來永遠想買就買到）、資金
情境被寫進全域 `config`（100 萬與 50 萬情境互相污染）。完整行為規格見
[STRATEGY_POSITION_POLICY_SPEC.md](./STRATEGY_POSITION_POLICY_SPEC.md)。

```text
Strategy.make_signals → StrategyPositionPolicy → Event Backtest Engine
                        (desired state)          (realized state)
```

| 這一層**負責** | 這一層**不負責** |
|---|---|
| 進場 / 續抱 / 退出 / 集中度 resize 的決定與 `reason_code` | 猜成交價、算股數、判斷買不買得到 |
| 目標權重與 `target_cash_weight`（等權資金槽，候選不足保留現金） | 現金餘額、券商成本、整張／零股湊單 |
| regime 層級對應的可用 slots（10 / 5 / 0） | regime 分類公式（外部提供，且必須帶 PIT provenance） |
| 「最早可成交時間」的宣告（T 日收盤 → T+1） | 實際成交日與成交價（引擎依交易日曆與 K 棒決定） |

**不得在 policy 內另做一套 execution。** 台股成本、tick、漲跌停、處置禁新倉、
整張／零股、價格完整性 fail-closed 與下市處理全部**重用**既有元件
（`execution/`、`backtest._assert_price_integrity`、`_limit_lock`、
`size_long_order`、stale/delist 處理）。引擎裡每天的順序是固定的：

```text
執行昨天形成的退出意圖 → 只把實際成交的 proceeds 加進現金
→ 集中度修剪 → 用真實現金嘗試進場 → 收盤 MTM → policy 形成明天的 desired state
```

倒過來就會出現「A 一字跌停賣不掉，卻已經用它的賣出款買了 B」——曝險與績效
憑空多一份，而且從 summary 完全看不出來。賣不掉的部位留在 realized holdings
繼續 MTM，退出意圖不清掉，下一個交易日再試。

四個容易看漏的邊界：

1. **決策日來自 `signal_frame` 的快照日期**，引擎不自己算「每 N 個交易日」或
   星期幾。舊的 `rebalance_every` / `rebalance_phase` 是交易日計數，一旦訊號那端
   用的是「每週最後一個交易日」，有假日的週兩者就會錯開，而錯開的方向剛好是
   「用還沒發生的訊號」或「漏掉整週」。快照日不是交易日一律 raise。
2. **`initial_capital` / `order_size_mode` / `minimum_commission` 是 immutable
   request**，只影響該次呼叫，**不寫回 `config`**。要比較 100 萬研究情境與 50 萬
   個人情境，兩次呼叫互不污染。
3. **policy 關閉時 legacy `picks_by_date` 路徑逐位元不變**：回傳結構
   （`summary` / `trades` / `equity_curve`）不長新 key，`decision_log`、
   `order_log`、`summary["strategy_position_policy"]` 只在 policy 開啟時出現。
   相位掃描（`evaluation/phases.py`）與 IS/embargo/OS、PIT、holdout、provenance
   閘門一律照舊套用。
4. **斷 bar／下市的 fail-closed 對每一檔持股每天生效**，兩條路徑共用
   `backtest._settle_stale_delisted`。這一條特別容易寫錯成「只檢查已經有退出意圖
   的部位」——下市股在排名快照裡本來就會消失、常常一個退出意圖都沒有，漏掉的結果
   是它永遠留在帳上、以凍結的最後收盤計價，下市虧損整段被忽略（方向是樂觀偏誤），
   而且「拒絕假設可用最後收盤賣出」永遠不會觸發。判定必須在**當日 MTM 之前**，
   否則 `last_bar_di` 已被更新，stale 會永遠少算一天。

稽核輸出（規格 §7）：`decision_log`（每次 snapshot 的 actions 與 reason codes）、
`order_log`（送進引擎的意圖與未成交原因）、`target_portfolio`（每個決策日的完整
desired weights 與 cash）、`summary["strategy_position_policy"]`（規則、rules
hash、資金情境、desired vs realized 差異統計、每種 exit reason 的次數／持有期／
realized return）。**這些是流程與可稽核性的改善，不是任何策略有效的證據**——本次
產生的回測數字不得用來宣稱 edge，也沒有任何策略因此通過 clean OOS／forward。

## 已移除的舊腳本（2026-08-16）

下列腳本沒有任何程式或測試 import，且都是**一次性任務或已被 registry 判定
`rejected` 的舊策略**。它們的結論都已經以報告形式進版控，所以刪掉的是腳本、
不是結論。要看當時怎麼算的請翻 git log。

| 已刪除 | 行數 | 結論存放處 |
|---|---:|---|
| `winner_dna.py` | 383 | S12 `rejected`；`outputs/winner_dna_report.md` |
| `defensive_rs.py` | 322 | S05 `rejected`；`outputs/DEFENSIVE_RS_REPORT.md` |
| `factor_audit.py` | 322 | `outputs/FACTOR_AUDIT_REPORT.md` |
| `sector_scan.py` | 309 | `outputs/sector_scan_report.md` |
| `factor_scan.py` | 278 | `outputs/FACTOR_SCAN_REPORT.md` |
| `disposition_event_study.py` | 205 | `outputs/DISPOSITION_EVENT_STUDY.md` |
| `experiment_weights.py` | 136 | `outputs/WEIGHT_FIX_REPORT.md` |
| `evaluate_dynamic_universe.py` | 102 | `outputs/DYNAMIC_UNIVERSE_REPORT.md` |
| `migrate_cache_stamp.py` | 53 | 快取遷移,已完成 |
| `policy_research_run.py` | 32 | 純 shim；實作在 `research/golden_path.py` |

同時刪除四份**已執行完畢**的 GOAL 文件(make_signals golden path、single holdout、
golden path remediation、screener completion)與過期的 `HANDOFF_2026-08-01.md`。
它們開頭都寫著「狀態:待執行」,留著會讓下一個讀的人以為還有事沒做;結論已經落進
程式碼註解、契約測試與 `STRATEGY_REGISTRY.md`。

**刪除的判準是「有沒有人 import」加上「文件有沒有把它當現行指令」。** 第二個條件
救回了 `data/prefetch.py` 與 `universe_bias_audit.py` —— 它們同樣零 import,但
`RESEARCH_OPERATING_PROTOCOL.md` 把它們列為換資料與宣稱前稽核的實際步驟,
刪掉等於拿掉一個documented 的能力。

## 工程閘門（與研究正確性同層）

| 閘門 | 內容 |
|---|---|
| `.github/workflows/ci.yml` | Python 3.11、`pip install -r requirements.txt`、`compileall` 語法 smoke、`preflight.py`、`unittest discover`。**市場資料測試離線**：不設 `FINMIND_TOKEN`，所以任何誤走真實資料路徑的測試會 fail-closed；checkout 與依賴安裝本身仍需網路 |
| `preflight.py` | 對 **git 追蹤中**的檔案檢查密鑰檔名／私鑰內容、`_cache`／`outputs` 資料產物誤追蹤、必要公開文件、`.gitignore` 覆蓋、`.env.example` 空值。命中只印規則與行號，不印內容 |
| `tests/` | `unittest`（非 pytest）、離線、HTTP 全 mock。修完 bug 要留回歸測試並在 docstring 說明原 bug |

公共框架採 [PolyForm Noncommercial License 1.0.0](./LICENSE) 的 source-available
非商業授權；[ADDITIONAL_PERMISSION.md](./ADDITIONAL_PERMISSION.md)另行允許個人以
本人自有資金自主交易。商業權利由 Project Owner 保留，外部貢獻必須接受
[CLA.md](./CLA.md)，確保公共修正仍可被納入未來商業版本。AI 分析師階段是獨立的私人
研究層，不因量化框架公開而自動落入本授權。

## 後續搬遷順序

1. 驗證並快取官方逐日 `reference_price / limit_up / limit_down`，取代一般日的
   `derived_prev_close`；新上市與轉板例外要接 PIT lifecycle。
2. 將其餘資料來源與快照搬到 `market_data/`；月頻 PIT provider 已先搬到
   `universes/`，舊的抓取／解析函式暫留 `universes/pit_snapshots.py` 作相容層。搬遷時
   `data.CacheScope` 是快取檔名的唯一推導點——研究腳本要路徑請用
   `data.cache_scope()` / `data.cache_glob()`，視窗自己指定的全市場表用
   `data.window_cache_scope()` / `data.parse_window_scope()`，不要自己拼字串
   （自己拼就是不變式 7 的下一次破口，處置快取已經因此重演過一次；舊快取加
   範圍戳用 `data/migrate_cache_range.py --apply`，處置／注意快取不在遷移範圍內，
   舊檔一律視為 miss，請重跑 `data/twse_disposition.py` / `data/tpex_disposition.py`）。
   仍不帶範圍維度但**結構上安全**的兩處：`price_adjust.divresult__{sid}__{snap}`
   （查詢窗固定 `2000-01-01`~snapshot，snapshot 已在 key 裡）與
   `pit_universe.pitsnap__{YYYYMMDD}`（檔名本身就是那一天）。
3. 把 `backtest/event_backtest.py` 拆成 `backtesting/engine.py`、`portfolio/` 與 `execution/`。
   在成交紀錄 parity 測試通過前，根目錄引擎仍是唯一正式入口。
4. 最後才搬研究腳本。已證偽與 blocked 策略仍保留在策略揭露紀錄，不因整理資料夾而
   消失或改名成已驗證策略。
5. 公共 repo 只維持可凍結、可稽核的候選輸出契約。Project Owner 的私人 AI 專案可
   消費該輸出並獨立保存研究結果；仍以純量化 A 組對照「量化＋AI 篩選」B 組，未經
   untouched OOS／forward 證明前不得覆寫量化核心。
