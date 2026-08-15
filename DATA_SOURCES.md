# 免費資料源實測盤點

> 更新 2026-08-03。**這份是「實測」而非「文件宣稱」** —— 每一項都在本 repo 打過、
> 記下實際回傳與踩到的坑。要找資料先看這裡,不要重新試錯。
>
> 想買資料前先讀最後一節「付費才有的東西」。目前結論:**還不需要付費**。
> 規劃性質的比較(TEJ / FinMind 方案)見 `DATA_SOURCE_RESEARCH.md`。
>
> ⚠️ **這一頁的端點沒有一個會在測試或 CI 裡被呼叫。** `tests/` 一律離線、mock 掉
> HTTP,`.github/workflows/ci.yml` 也不設 `FINMIND_TOKEN`。要動這些端點就是要動
> 真實額度與真實資料,只在本機手動執行。

## 1. FinMind(免費層)

**額度:600 次/小時**,每小時重置。查目前用量:

```bash
請從 FinMind 帳號頁查看當前用量。不要把 token 放進 URL、shell history 或共享終端的
process list；本 repo 的資料請求只用 `Authorization: Bearer ...` header。
# → {"level":1,"level_title":"Free","api_request_limit_hour":600,"user_count":<已用>}
```

超額回 `HTTP 402 Payment Required`。**注意 `/api/v4/user_info` 是 404,要用
`api.web.finmindtrade.com/v2/user_info`。**

### 可用(已實測)

| dataset | 內容 | 本 repo 用在哪 |
|---|---|---|
| `TaiwanStockPrice` | 日 OHLCV + 成交金額(**未還原**) | `data.fetch_price` |
| `TaiwanStockPrice` (`data_id=TAIEX`) | 大盤指數 | `data.fetch_market_index` |
| `TaiwanStockInfo` | 代號/名稱/產業/市場別 | `data.fetch_stock_info` |
| `TaiwanStockInstitutionalInvestorsBuySell` | 外資/投信/自營淨買 | `data.fetch_institutional` |
| `TaiwanStockMarginPurchaseShortSale` | 融資融券餘額 | `data.fetch_margin` |
| `TaiwanStockSecuritiesLending` | 借券 | `data.fetch_lending` |
| `TaiwanStockShareholding` | 外資持股比例 | `data.fetch_foreign_holding` |
| **`TaiwanStockDividendResult`** | **除權息前後參考價** | **`price_adjust.py` 自建還原價的關鍵** |
| `TaiwanStockDividend` | 股利政策(含除息交易日) | 未用 |
| `TaiwanStockDelisting` | 下市清單(2024-01~2026-07 共 32 筆) | 未用(PIT 池方案更完整) |
| `TaiwanStockBalanceSheet` | 資產負債表,含 `CapitalStock` | **可推市值**,見下 |
| `TaiwanStockFinancialStatements` | 損益表(EPS/毛利等) | 未用 |
| `TaiwanStockMonthRevenue` | 月營收 | 未用 |
| `TaiwanStockPER` | 本益比/淨值比 | 未用 |

### 已確認 client schema、尚未實測權限與覆蓋

| dataset | 公開 client 欄位 | 狀態 |
|---|---|---|
| `TaiwanStockPriceLimit` | `date / stock_id / reference_price / limit_up / limit_down` | 已接 `data.fetch_price_limits`；本機未設定 token，尚未驗證真實回應、免費層權限、新上市空值語意與歷史覆蓋，不列入「可用(已實測)」 |

### 被鎖(回 `status=400 Your level is register`)

| dataset | 內容 | 免費替代 |
|---|---|---|
| `TaiwanStockPriceAdj` | 官方還原價 | ✅ `price_adjust.py` 自建(僅除權息,不含分割/減資) |
| `TaiwanStockMarketValue` | 市值 | ✅ 用 `CapitalStock`,見下 |
| `TaiwanStockHoldingSharesPer` | 集保戶數分級 | ❌ TDCC 有公開但需另爬 |
| `TaiwanStockTradingDailyReport` | 分點進出 | ❌ 難自建,TWSE 需逐檔爬 |

### 免費算市值的方法(實測)

`TaiwanStockMarketValue` 被鎖,但資產負債表的股本免費:

```
TaiwanStockBalanceSheet → type="CapitalStock"
台積電 2026-03-31: 259,323,701,000 元
÷ 面額 10 元 = 25.93 億股        ← 對得上實際股數
× 收盤價 = 市值
```

注意股本是**季頻**且有公告落後,PIT 使用要對齊公告日而非財報期末日。

## 2. TWSE 證交所(免費,免 token)

### 逐日全市場行情 —— **含當時在交易、後來下市的股票**

```
https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX
  ?date=YYYYMMDD&type=ALLBUT0999&response=json
```

- 回 **10 張表**;要的是欄位含「證券代號」+「成交金額」那張(不要寫死 index,
  `pit_universe._col` 用欄位名找)。
- 實測:2026-07-31 共 1373 列,其中 4 碼普通股 1085 檔;2024-08-05 為 1020 檔。
  **檔數會變 = 上市/下市**,這正是 survivorship-free 的來源。
- 非交易日 `stat != "OK"`。
- 欄位:證券代號/名稱/成交股數/成交筆數/成交金額/開/高/低/收/漲跌/…

用於 `pit_universe.py` 建 point-in-time 候選池。

### 注意/處置

```
歷史注意:https://www.twse.com.tw/rwd/zh/announcement/notice
          ?startDate=YYYYMMDD&endDate=YYYYMMDD&response=json   ← 日期參數有效
當前處置:https://openapi.twse.com.tw/v1/announcement/punish     ← 只回當前,無歷史
```

TWSE **沒有歷史處置端點**,所以 `twse_disposition.py` 用「連續3日注意→次日起處置
10日」規則推導(proxy,偏寬,`source="derived"`)。

### 其他

```
當日全市場:https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL
```

## 3. TPEx 櫃買中心(免費,免 token)

### 逐日全市場行情

```
https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes
  ?date=YYYY/MM/DD&response=json      ← 日期格式是 YYYY/MM/DD,不是 YYYYMMDD
```

- 實測 2026-07-31 回 10218 列,其中 4 碼普通股僅 **888 檔**(其餘是權證等)。
- ⚠️ **非交易日會回「上一個交易日」的資料**,不是空表。必須用回傳的 `date` 欄位
  驗證是否等於查詢日,不符就當非交易日(`pit_universe.fetch_tpex_day` 有做)。
- 端點名很容易試錯:`afterTrading/otc` 回正確欄位但 **0 列**,要用 `dailyQuotes`。

### 注意/處置 —— **品質優於 TWSE**

```
歷史處置:https://www.tpex.org.tw/www/zh-tw/bulletin/disposal
歷史注意:https://www.tpex.org.tw/www/zh-tw/bulletin/attention
         ?startDate=YYYY/MM/DD&endDate=YYYY/MM/DD&response=json
```

處置端點**直接給真實「處置起訖時間」**,不需要像 TWSE 那樣推導。
所以上櫃是 `actual`、上市是 `derived`,`source` 欄位據實標示(見 `tpex_disposition.py`)。

實測 2023-01~2026-06:978 段真實處置 / 370 檔;注意事件 12117 筆。
PIT 檢查 325 筆零違規 —— 公布日一律早於處置起始日。

### 端點探索

```
https://www.tpex.org.tw/openapi/swagger.json     ← 列出所有 openapi 端點
```

openapi 系列(`/openapi/v1/...`)只給**近期滾動視窗**:處置約 2 週、注意僅當日。
要歷史一律走上面的 `bulletin/*`。

## 4. 通用踩坑清單

| 坑 | 症狀 | 處理 |
|---|---|---|
| **分段抓取邊界重複** | 期間跨查詢邊界時兩段都回傳同一筆 | 必須去重。實測 TPEx 2024 全年 415 筆 = H1∪H2,交集 14 筆 |
| **瞬斷靜默變空** | `ChunkedEncodingError` 後回空表 | 必須重試,耗盡後 **raise 而非回空** —— 空表會被當成「該期間無資料」漏掉整年 |
| **欄位夾帶相對連結** | `"合晶(../../mainboard/...)"`、`"(./attention.html)"` | 正則剝除 `\(\.{1,2}/[^)]*\)` |
| **代號混雜** | 5 碼可轉債(24552)、6 碼權證、00 開頭 ETF | 代號**形狀**前篩(4 碼數字非 00)只擋這幾種,**不是**證券別判定 —— 一律走 `security_type.is_plausible_equity_code` |
| **FinMind 外資持股對不上** | `foreign_ratio` merge 後覆蓋率 0% | 週頻資料,需 `merge_asof` 而非 `merge` |
| **興櫃／DR／創新板洩漏** | `universe._is_normal_stock` 收了 `market_type` 卻沒用 | **已修(2026-08-15)**:改用 `security_type` 的證券別白名單,見下節 |

### 證券別白名單(2026-08-15 已修)

`universe._is_normal_stock(stock_id, market_type)` 收了 `market_type` 卻**完全沒用
它**,實際只檢查「4 碼數字且不以 00 開頭」。實測(repo 快取,`data.fetch_stock_info`
的去重規則):

| 證券別來源 | 舊規則通過 | 白名單擋掉 | 明細 |
|---|---|---|---|
| `info__ALL__2026-06-22`(凍結快照) | 2509 | 408 | 興櫃 369 / 創新板 28 / DR 11 |
| `info__ALL__2026-08-06` | 2527 | 421 | 興櫃 381 / 創新板 29 / DR 11 |
| PIT 逐日快照(<= 2026-06-22) | 1988 | 33 | 創新板 28 / DR 4 / 興櫃 1 |
| `outputs/universe_top100.json` | 100 | 1 | 創新板 7610 聯友金屬-創 |

**為什麼是「假 Sharpe」等級的缺陷**:興櫃沒有 ±10% 漲跌停。2026-05 單日
|ret| > 10.5% 的比例為上市 0.034%、上櫃 0.042%、興櫃 **3.872%**(約 100 倍),
最大單日 +57.17%(6775 穎台科技 2026-05-12)、最小 -24.90%;而動能因子找的正是
那種標的 —— 偏誤方向是系統性灌高 Sharpe。流動性也擋不住:2026-05 有 339 檔興櫃
真的有成交(合計日均成交值 136.8 億),最大一檔(3595 山太士)日均成交值 14.75 億、
全市場 ADV 排名 **#188**,直接落在 `DYNAMIC_UNIVERSE_CANDIDATE_POOL=300` 之內。

判定欄位(不用代號規則猜):

- `type`(→ `market_type`):只放行 `twse` / `tpex`,`emerging` 明確排除。
- `industry_category`(→ `industry`):非普通股清單(ETF / ETN / 指數 / 大盤 /
  所有證券 / 受益證券 / 存託憑證 / 創新板股票)+ 已知普通股白名單;沒見過的分類
  fail-closed。
- `stock_name`(→ `name`):後綴 `-創`(創新板)與 `-DR`。**這條不可省** ——
  FinMind 的 `industry_category` 對創新板不可靠:29 檔簡稱帶「-創」的股票只有 3 檔
  被標成 `創新板股票`(7835 永悅健康-創 標成「數位雲端」),而且同一檔會在不同快照
  被改分類(4590 富田-創:`創新板股票` → `電機機械`)。

已知限制:TaiwanStockInfo 的證券別是**當下狀態**,不是 PIT。「當時興櫃、現在上市」
的歷史列擋不住(PIT 池的保護來自資料源本身:TWSE/TPEx 日行情端點不含興櫃)。

## 5. 付費才有的東西(以及該不該買)

**目前結論:不需要。** 兩個曾經的阻擋項都已用免費資料解決:

- 還原價 → `price_adjust.py`(缺口:分割/減資,由 `price_integrity` 殘留掃描擋)
- 市值 → `CapitalStock / 10 × close`

真正只有付費才有的,依價值排序:

1. **分點進出**(`TaiwanStockTradingDailyReport`)—— 台股特有的主力券商籌碼維度,
   現有因子完全沒有,且很難自建。從 S19 的因子掃描看,籌碼面(法人流)是有效的
   那一半,往這個方向加深最有機會找到**低相關**的第二個 portfolio 成分。
2. **速率限制** —— 600/hr 是開發效率的實際瓶頸(本 repo 曾為此寫額度感知的過夜腳本)。
3. 集保戶數分級 —— TDCC 有公開,可自爬。
4. 官方還原價 —— 補分割/減資缺口。

**買的時機**:等 PIT 候選池上線、策略在無偏基礎上重測完之後。在有偏的基礎上
買資料,只是跑得更快而已。

## 關聯檔案

- 自建還原價:`price_adjust.py`｜PIT 候選池:`pit_universe.py`
- 注意/處置:`twse_disposition.py`(上市)、`tpex_disposition.py`(上櫃)
- 資料層:`data.py`｜候選池建構:`build_universe.py`
- 規劃性質的資料源比較(TEJ 等):`DATA_SOURCE_RESEARCH.md`
