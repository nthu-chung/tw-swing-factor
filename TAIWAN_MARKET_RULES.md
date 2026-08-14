# 台股普通股回測規則

核對日期：2026-08-14。這份規格只適用上市／上櫃普通股 long-only 現股；ETF、ETN、
權證、債券與信用交易不得直接套用。

## 已確立並寫入程式的規則

| 規則 | 系統行為 | 官方依據 |
|---|---|---|
| 普通交易單位 | `regular_lot` 每次只能買 1,000 股或其倍數 | TWSE／TPEx 交易制度 |
| 零股單位 | `odd_lot_proxy` 只能產生整數股，最小 1 股 | TWSE／TPEx 零股制度 |
| 股票升降單位 | `<10:0.01`、`10~50:0.05`、`50~100:0.1`、`100~500:0.5`、`500~1000:1`、`>=1000:5` | TWSE 營業細則第 62 條 |
| 每日漲跌幅 | 以開盤競價基準上下 10%，再按合法 tick 向範圍內調整 | TWSE 營業細則第 63 條 |
| 小於一個 tick | 仍准許上下移動一個最小升降單位 | TWSE 營業細則第 63 條 |
| 新上市櫃普通股 | 符合資格者首五個交易日無漲跌幅限制；轉板例外由 PIT lifecycle 判斷 | TWSE／TPEx 交易制度 |
| 手續費 | 費率與最低收費皆可調；不把最低 20 元冒充交易所規則 | TWSE 投資指南 |
| 股票交易稅 | 賣出價金 0.3%；本系統不模擬當沖優惠 | TWSE 投資指南 |

官方公開範例已釘成測試：開盤競價基準 40.60 元時，漲停為 44.65 元、跌停為
36.55 元，而不是把 10% 後的數值任意四捨五入。

官方來源：

- [TWSE 交易制度](https://www.twse.com.tw/zh/products/system/trading.html?hl=zh-TW)
- [TWSE 營業細則第 62 條](https://twse-regulation.twse.com.tw/TW/law/DOC01_print.aspx?FLCODE=FL007304&FLNO=62)
- [TWSE 營業細則第 63 條](https://twse-regulation.twse.com.tw/TW/law/DOC01_print.aspx?FLCODE=FL007304&FLNO=63)
- [TWSE 投資指南手續費、交易稅與 T+2](https://www.twse.com.tw/zh/about/company/guide.html)
- [TPEx 上櫃交易制度](https://www.tpex.org.tw/zh-tw/mainboard/trading/rules/system.html)

## 三種股數模式

```text
research_fractional  純 alpha、相位與參數比較；允許小數股，不是可下單績效
regular_lot          1,000 股整張；需指定實際初始資金，供可部署性檢查
odd_lot_proxy        1 股整數；成交價仍借用普通交易 open，只能做敏感度測試
```

設定方式：

```bash
export SWING_ORDER_SIZE_MODE=regular_lot
export SWING_INITIAL_CAPITAL=1000000
export SWING_MIN_COMMISSION=20       # 請按自己的券商修改；0 表示不設最低費用
```

每次回測的 `summary.execution` 都會留下模式、初始資金、費率、最低手續費、稅率、
因交易單位而跳過的候選數，以及價格限制資料來源。

## 精確漲跌停仍需要的資料

判斷順序為：

1. 官方逐日 `limit_up / limit_down`。
2. 官方逐日 `reference_price` 搭配本 repo 的法規計算器。
3. 暫時以昨日收盤推導，並標記 `derived_prev_close`。

第 3 種在一般交易日通常成立，但除權息、減資、恢復交易與新上市日不能視為官方
精確值。因此只有 `regular_lot + official` 才會標成 `price_and_lot_realistic=true`。
由於日線仍無法重建下列盤中與帳務規則，整體 `execution_realistic` 目前固定為 false。

FinMind 公開 client 顯示 `TaiwanStockPriceLimit` 具有 `date / stock_id /
reference_price / limit_up / limit_down` 欄位；目前本機沒有設定 token，尚未完成真實
回應、權限與歷史覆蓋測試，所以這輪不把它預設成已驗證資料源。

## 日線不能精確重建的規則

以下不可因為程式有欄位就宣稱已精確模擬：

- 盤中零股 9:10 後獨立撮合價格。
- 盤中瞬間價格穩定措施與排隊順位。
- 處置股票每 5／20 分鐘集合競價的實際成交機率。
- 全額交割與預收款券的券商帳務流程。
- T+2 應收應付帳本。

目前處置期間採禁止新倉的保守模型；其餘會在取得足夠資料後逐項加入，不能用普通
日線 OHLC 假裝還原盤中路徑。
