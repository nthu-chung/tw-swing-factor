# 台股論文級資料來源盤點

> 更新日：2026-07-23  
> 目標：long-only 動能／族群研究所需的長期還原價、每日 point-in-time
> universe、下市股、公司行動與可重現資料快照。
>
> ⚠️ **2026-08-03 更新：這份是「該不該買」的規劃比較，已有部分結論過時。**
> 要查「現在有什麼、怎麼打、有什麼坑」請看 **`DATA_SOURCES.md`**（實測盤點）。
> 本文當時的兩個主要結論已被免費方案取代：
> - 還原價不必付費 → `price_adjust.py` 用免費的 `TaiwanStockDividendResult` 自建
>   （缺口：分割／減資，由 `price_integrity` 殘留掃描擋）。
> - PIT universe／下市股不必付費 → `pit_universe.py` 用交易所逐日全市場快照重建，
>   快照天然含當時在交易、後來下市的股票，且自帶 OHLCV。
> - 另：`TaiwanStockDelisting` 本來就是免費的（2024-01~2026-07 共 32 筆）。

## 結論

### 最務實的下一步：FinMind backer / sponsor

目前程式已使用 FinMind，升級後改動最小：

> 2026-07-23 實測：目前 repo 複用的帳號等級是 `register`；
> `TaiwanStockPriceAdj` 回 HTTP 400 `Please update your user level`，尚無還原價權限。

- `TaiwanStockPriceAdj`：還原股價自 1994-10-01 起，但只限 backer / sponsor。
- `TaiwanStockDelisting`：下市櫃資料自 2001-01-01 起。
- 另有減資參考價、分割後參考價、面額變更等公司行動資料。
- 付費層支援指定日期一次取得全市場日資料，適合建立每日候選池。

限制：

- 必須確認方案授權是否允許論文附帶資料或只能附下載程式／hash。
- 下市表本身不等於完整歷史證券主檔；仍要驗證上市日、轉板、產業分類歷史。
- 免費 token 官方文件列出的限制為每小時 600 次，逐檔抓全市場多年資料效率不足。

官方資料：

- [FinMind 技術面與還原股價](https://finmind.github.io/tutor/TaiwanMarket/Technical/)
- [FinMind 下市櫃與公司行動資料](https://finmind.github.io/tutor/TaiwanMarket/Fundamental/)
- [FinMind API rate limit](https://finmind.github.io/en/quickstart/)

### 論文品質優先：TEJ PIT

TEJ 明確提供：

- 歷史上曾經上市櫃及已下市公司；
- point-in-time 公告時間與歷史版本；
- 歷史價格調整、公司事件、每日指數／ETF 成分；
- 適合直接處理 survivorship bias 與 look-ahead bias。

這是最乾淨的研究來源，但需詢價與確認學術授權/API 方案。

官方資料：

- [TEJ Point-in-Time 與完整歷史樣本](https://www.tejwin.com/news/tej-point-in-time-%E6%8A%95%E8%B3%87%E7%94%A8%E8%B2%A1%E5%8B%99%E8%B3%87%E6%96%99%E5%BA%AB/)
- [TEJ 量化資料庫的 PIT、下市股與歷史成分](https://www.tejwin.com/news/tej%E6%8A%95%E8%B3%87%E7%94%A8%E8%B3%87%E6%96%99%E5%BA%AB_%E9%87%8F%E5%8C%96%E6%8A%95%E8%B3%87%E5%88%86%E6%9E%90%E6%87%89%E7%94%A8/)

### 免費官方資料：TWSE + TPEx 自建

可用於交叉核對及自行建 daily security master：

- TWSE OpenAPI 有當日全市場成交資訊、除權息預告、暫停交易及指數資料。
- TWSE 另提供超過 15 年的全市場歷史交易資訊產品；是否免費、格式與授權需向
  資訊服務窗口確認。
- TPEx 網站可按日期下載每日上櫃行情 CSV。

優點是來源官方；缺點是跨市場格式、公司行動、代碼沿用、下市股與歷史產業分類
都要自行清洗，研究工程量最大。

官方資料：

- [TWSE OpenAPI](https://openapi.twse.com.tw/)
- [TWSE Information Services](https://www.twse.com.tw/en/products/information/information.html)
- [TPEx Daily Stock Quotes](https://www.tpex.org.tw/en-us/mainboard/trading/info/pricing.html)

## 建議採購／驗證順序

### 若目標只是先完成策略原型

1. 先向 FinMind 確認 backer / sponsor 是否同時滿足：
   `TaiwanStockPriceAdj`、全市場按日下載、10 年以上、下市股可查、研究再現授權。
2. 同時確認 `TaiwanStockMonthRevenue` 的歷史 `create_time` 是否完整；
   舊資料若沒有建立時間，不能把月初日期當成當時已知。
3. `TaiwanStockNews` 可作探索，但必須確認新聞時間戳、來源授權、歷史回補與
   文章去重；未確認前不能作論文主因子。

### 若目標是論文／可投資等級

直接向 TEJ 詢價，要求報價單明列以下資料表與 API 權限：

- 全部現存與已下市普通股的歷史證券主檔、上市／下市日；
- 還原總報酬價格、除權息／減資／分割／面額變更；
- 三大法人、外資持股、融資融券、借券；券商分點作選配；
- 月營收數值與**實際公告日／時間**；
- MOPS 每日重大訊息全文與公告時間；
- point-in-time 細產業／產品鏈分類，或可回溯的公司業務標籤；
- TAIEX、產業指數與成分歷史。

TEJ 官方量化資料庫說明 PIT、下市股及還原價是避免前視與生存者偏誤的核心；
月營收產品含公告日、上市與下市公司；2026 年推出的 MOPS 重訊全文資料自
2013 年起含公告時間與全文；券商分點資料則可補「主力集中」類特徵。

官方資料：

- [TEJ 量化投資資料庫](https://www.tejwin.com/en/news/quantitative-investment/)
- [TEJ 月營收資料](https://www.tejwin.com/en/news/monthly-sales/)
- [TEJ MOPS 重訊全文](https://www.tejwin.com/en/news/tw-corporate-announcements/)
- [TEJ 券商分點交易](https://www.tejwin.com/en/news/brokers-trading-taiwan/)
- [TEJ API 提供方式](https://www.tejwin.com/en/service-provide-method/)

4. TWSE / TPEx 免費資料作 spot check，不建議在第一版論文自行重建全部 PIT
   corporate-action pipeline。

TWSE Data E-Shop 也可單獨訂購每日法人交易、除權息與上市／下市資料；MOPS Push
服務含月營收與公司活動。若 TEJ 報價超出預算，可以官方資料分項採購，但需要自行
處理 TWSE/TPEx 合併、代碼沿用、公告時點與歷史分類。

## Repo 接口

目前可用環境變數切換還原價：

```bash
SWING_PRICE_DATASET=TaiwanStockPriceAdj .venv/bin/python main.py backtest \
  --pool 300 --universe-top 100 --top 5
```

動態 universe 每個訊號日使用截至當日的 20 日平均成交值排名，並套用當日可知的
20 日平均成交量門檻；策略仍是 long-only。使用現有 `universe_top300.json` 時，
metadata 會標記 `survivorship_free=False`，因為候選集合仍來自期末快照。
