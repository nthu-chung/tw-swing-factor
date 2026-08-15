# 價格尺度契約(Price Scale Contract)

> 狀態:**契約定義,實作尚未完成**。本文件先把用語與欄位語意定死,程式再照它搬。
> 訂於 2026-08-15,依據是本 repo 對 FinMind 付費資料集的實測,以及 CRSP / Compustat /
> Bloomberg / LSEG / FactSet 官方文件的儲存模型。

## 0. 為什麼需要這份契約

`data.fetch_price()` 目前回傳一個裸 `DataFrame`,裡面的 `close` **可能**是原始價、
**可能**是自建還原、之後**可能**是 FinMind 的 `TaiwanStockPriceAdj`。呼叫端無從分辨。

於是同一份資料被三個地方用三種互不相容的假設消費:

- `execution/taiwan_rules.py` 假設它是**當時真正的成交價**(tick 價格帶、±10% 漲跌停)
- `execution/costs.py` 假設它是**當時真正的成交價**(1000 股整張、20 元最低手續費)
- `factor_engine/data_fields.py` 假設 `close` 與 `turnover / volume` **同一個尺度**

而沒有任何一層能發現假設被違反。實測後果(2327 國巨,2024-06-24):

| 量 | 原始價空間 | 還原價空間 | 差 |
|---|---:|---:|---:|
| 一張成本 | 759,000 元 | 147,245 元 | **5.15 倍** |
| tick 升降單位 | 1.00(500–1000 帶) | 0.50(100–500 帶) | 帶別錯 |
| vwap(`turnover/volume`)vs `close` | 一致 | 546.50 vs 135.53 | **4.03 倍** |

這與 `factor_engine/panel_density.py` 要解的是同一類問題(呼叫端無法得知 panel 的
稠密度),所以解法也一樣:**欄位契約 + 標籤 + fail-closed**。

## 1. 用語:先定死,因為中文圈的慣例與直覺相反

**中文圈標準用法以 Tushare 官方文件為準,與「字面直覺」相反,務必照抄不要自己推:**

| 中文 | 錨點 | 最新一根 bar 的值 | 新事件發生時 | 英文 |
|---|---|---|---|---|
| **前復權(qfq)** | 最新交易日 | **等於原始價** | **整段歷史被重寫** | back-adjusted |
| **後復權(hfq)** | 序列起點 | 大於原始價 | 只新增未來的值,歷史不變 | forward-adjusted |

**FinMind 的 `TaiwanStockPriceAdj` 是前復權(qfq)。** 實測依據:2026-08-14 當日
2330 與 2327 的 `adj close` 與 `raw close` 完全相等、OHLC 調整倍數全部 `1.000000`;
且同一 `start_date` 用不同 `end_date` 抓,共同交易日 576 筆逐值相同 —— 錨是資料集的
最新一根 bar,`end_date` 不影響錨點。

> 錨在最新交易日**不是 FinMind 的缺陷**,而是機構常態:CRSP 官方定義 base date
> 「usually chosen to be the last available day of trading」,Compustat 明文
> 「發生分割或股票股利時所有期間的累積因子都會被改」。差別在於機構庫存的是
> **原始價 + 帶版本的因子表**,還原是查詢時的 view;FinMind 只給還原值,
> 沒有因子表、沒有 as-of 參數,所以無法重建任何一個 vintage。

**本 repo 一律不用中文簡稱溝通程式行為**,程式與 provenance 欄位只用明確的錨點名稱:
`adjustment_anchor ∈ {latest_bar, series_start, fixed_date}`。中文簡稱只在對外文件出現,
且必須同時標註錨點。

## 2. 欄位契約

價格 frame 的欄位語意固定如下,**不因任何設定而改變**:

| 欄位 | 語意 | 是否隨設定改變 |
|---|---|---|
| `open` / `high` / `low` / `close` | **當時真正成交的價格(as-traded)** | **否,永遠是原始價** |
| `open_adj` / `high_adj` / `low_adj` / `close_adj` | 還原價,語意由 `adjustment_mode` + `adjustment_anchor` 決定 | 是 |
| `adj_factor_price` | 價格累積因子 `C_p(t)`,`close_adj = close / adj_factor_price` | 是 |
| `adj_factor_share` | 股數/成交量累積因子 `C_s(t)`,`volume_adj = volume * adj_factor_share` | 是 |
| `volume` | 成交股數,**原始** | 否 |
| `turnover` | 成交金額,**原始** | 否(見 §3) |

**鐵則一:`close` 永遠是原始價。** 現行「還原後覆寫 `close`」的做法是本契約要消滅的東西。
要還原值就讀 `close_adj`,讀不到就是資料層沒給,不是預設值。

**鐵則二:價格因子與股數因子必須是兩欄,不得共用一欄。**
依據 CRSP:`CFACPR` 與 `CFACSHR` 在 spin-off、rights、非最終清算分配、增資、限額收購時
**不相等**。且兩者的套用方向相反 —— 價格用**除**、股數與成交量用**乘**
(CRSP:`A(t) = P(t)/C(t)` vs `A(t) = P(t)*C(t)`;Compustat:per-share 除、shares 乘)。

**鐵則三:成交金額(`turnover`)是尺度不變量,永不調整。**
它是當天真正換手的錢,不隨任何還原基準改變。

## 3. 誰該讀哪一欄

| 用途 | 讀哪一欄 | 理由 |
|---|---|---|
| tick 升降單位、±10% 漲跌停、一字鎖停判定 | `close` / `open`(raw) | 價格帶是絕對價位規則 |
| 整張 1000 股成本、零股、最低手續費 20 元 | `close` / `open`(raw) | 資金可負擔性是絕對金額 |
| 事件引擎的成交價與部位成本 | `open` / `close`(raw) | 與實盤語意一致 |
| 報酬、動能、MA、停損比較、權益曲線 | `*_adj` | 需要跨公司行動連續 |
| `vwap` | `turnover / volume`(兩者皆 raw) | 兩個原始量相除 = 原始 vwap |
| `vwap` 要與 `close_adj` 比較時 | `vwap / adj_factor_price` | **不可直接比,實測差 4.03 倍** |
| `amihud` = 非流動性 | `abs(ret_adj) / turnover` | 分子用還原報酬、分母用原始金額 |
| `dollar_volume`、ADV20、universe 排名 | `turnover`(raw) | 尺度不變量,不需轉換 |

> 業界佐證:LSEG Datastream 官方教學明寫 `UP`(unadjusted price)的用途就是
> 「implement stock price restrictions」;LSEG Historical Pricing 把「只調價不調量」
> 與「價量同調」做成兩個具名模式(`RPO` / `RTS`)。**FinMind 的 Adj 在這個分類上就是
> `RPO`** —— 所以任何用到 `volume` / `turnover` 的因子在還原檔裡是未定義行為。

## 4. 現金股利要不要調成交量:採 CRSP / Zipline 慣例

業界有兩派:LEAN 的 `Normalize()` 對 Adjusted 模式一律 `volume × (1/factor)`(含現金股利);
Zipline 原始碼與 CRSP 明訂**股數與成交量只吃分割與股票股利,現金股利不調量**。

**本 repo 採 CRSP / Zipline 慣例**,理由是台股現金股利頻繁而分割罕見,若把現金股利也
折進量,絕大多數個股的歷史成交量都會被無謂地改寫:

```
adj_factor_share 只在以下事件改變:股票分割、股票股利(配股)、減資、面額變更
adj_factor_price 在以上事件 + 現金股利 + spin-off / rights 時改變
```

因此 `adj_factor_price != adj_factor_share` 是常態,不是異常。

## 5. Provenance:每份結果都要能自證它是什麼尺度

`summary["data"]` 至少新增:

| 欄位 | 值域 | 意義 |
|---|---|---|
| `adjustment_mode` | `none` / `price_only` / `price_and_volume` / `total_return` | 這份資料被調整到什麼程度 |
| `adjustment_anchor` | `latest_bar` / `series_start` / `fixed_date` | 錨在哪裡(決定可不可重現) |
| `adjustment_source` | `vendor_adj` / `self_built` / `none` | 因子從哪來 |
| `events_vintage` | 日期 | 公司行動事件表的版本 |
| `events_sha256` | 雜湊 | 事件表內容指紋 |
| `price_space_execution` | 必須是 `raw` | 執行層讀了哪一條序列 |
| `return_convention` | 個股與基準各自 `with_dividends` / `price_only` | 見 §6 |

`adjustment_anchor == "latest_bar"` 時,**該結果不得標為 formal-evidence-eligible** ——
因為它在下一次公司行動後不可重現。這一條讓「可重現性」變成機器可判定的欄位,
而不是靠人記得。

## 6. 基準口徑必須與個股一致

個股序列若含息(還原價把現金股利折回價格),基準就必須用**含息報酬指數**
(`TaiwanStockTotalReturnIndex`);個股若不含息,基準用價格指數(TAIEX)。

實測不一致的代價:**每年約 2.86 個百分點的假超額、Sharpe 差 0.113**。
口徑不一致的比較比不比更糟,因此這一條是 fail-closed,不是警告。

## 7. 這份契約要求的 fail-closed 閘門

| 閘門 | 條件 | 處置 |
|---|---|---|
| 尺度宣告 | frame 缺 `adjustment_mode` / `adjustment_anchor` | 不得產出正式績效 |
| 執行層純度 | 執行層收到只有 `*_adj` 而無 raw 欄位的 frame | raise |
| 量價一致性 | `turnover` 與 `close × volume` 的比值偏離 1 超過門檻 | raise(代表尺度被混用) |
| 因子只在事件日變動 | `adj_factor_*` 在非公司行動日改變 | raise(代表因子表或價格有誤) |
| 因子幅度相符 | 因子跳幅與事件內容(配息/配股/減資比例)推算不符 | raise(代表有未記錄的事件) |
| 報酬上界 | 非事件日的日報酬絕對值超過當日法定漲跌幅 | raise(比固定 0.11 門檻精確) |
| 基準口徑 | 個股與基準的 `return_convention` 不一致 | raise |

> 最後一條取代現行 `PRICE_INTEGRITY_RETURN_THRESHOLD=0.11` 的固定門檻。
> 實測依據:全市場 2015–2026 共 532 筆減資,其中 **139 筆(26.1%)的價格跳幅
> 小於 0.11** —— 固定門檻對四分之一的減資結構上是盲的。改用「非事件日 + 法定漲跌幅」
> 同時解決誤殺(事件日的合法跳空)與漏抓(小幅減資)。

## 8. 尚未決定的事

以下需要在建 `market_data/` 之前拍板,列在這裡避免被默默決定:

1. **錨要不要從 `latest_bar` 換成 `series_start`**(即 qfq → hfq)。換了之後歷史值
   永不改變、凍結績效可重現;代價是還原價會愈往後偏離現實,所以執行層**只能**讀 raw
   ——這正好是本契約 §3 已經要求的,所以代價其實已經被吸收。
2. **因子從哪裡來**:從 FinMind `adj / raw` 相除反推(涵蓋所有事件但不可解釋),
   還是自建事件表推導(可解釋但要接四個來源、且會漏抓)。
3. ~~FinMind 隱含比值 4.0286 與 TWSE 官方 4.0000 的 0.7% 落差成因未明~~
   **已解(2026-08-15 實測)**:那不是誤差。2327 的 `close / close_adj` 在分割前是
   `4.028701`、分割後是 `1.007175`,**兩者相除正好 `4.000000`**,等於 TWSE TWTB8U 官方
   的 `546.00 / 136.50`。也就是:

   > **因子的絕對值 = 該日之後所有事件的累積;因子的「跳幅」才是那一次事件的比例。**

   殘留的 `1.007175` 是分割後到錨點之間累積的現金股利。這條規則讓 §7 的
   「因子跳幅要與事件內容相符」成為可實作的檢查:比對的對象是**相鄰兩日因子的比值**,
   不是因子本身。
4. **興櫃、創新板、DR 的排除**已在另一條線處理(證券別白名單),但它們的公司行動
   語意是否與普通股相同,尚未查證。
