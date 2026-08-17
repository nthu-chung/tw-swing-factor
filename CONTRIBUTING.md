# 貢獻指南

謝謝你願意改善台股回測的正確性。這個 repo 最在意的不是「程式能跑」，而是避免
look-ahead、survivorship bias、價格污染與成交假設製造出假績效。

## 開始前

請依序閱讀：

1. [README.md](./README.md)
2. [AGENTS.md](./AGENTS.md)
3. [ARCHITECTURE.md](./ARCHITECTURE.md)
4. [STRATEGY_REGISTRY.md](./STRATEGY_REGISTRY.md)
5. [RESEARCH_OPERATING_PROTOCOL.md](./RESEARCH_OPERATING_PROTOCOL.md)

大型重構、新資料源或會改變回測語意的修改，請先開 issue 說明問題、偏誤風險與預期
驗證方式。單純文件、測試或明確 bug fix 可以直接送 pull request。

## Pull request 最低要求

- 使用繁體中文寫文件與「為什麼」的註解，程式識別字維持英文。
- 不提交 token、`.env`、市場資料快取或可重新產生的研究產物。
- 修 bug 必須附離線回歸測試，docstring 說明原本如何產生錯誤結果。
- 新增 `ts_` operator 必須加入因果性測試，確保附加未來資料不改變過去值。
- 新策略必須登記證據等級、prove/kill 條件、已知偏誤與失敗結果。
- 不以單一相位、受污染 OS、未還原價或非 PIT universe 宣稱策略有效。
- 不引入測試時的真實 HTTP 請求；FinMind／TWSE／TPEx 一律 mock。

提交前執行：

```bash
PYTHONPATH=. .venv/bin/python preflight.py
PYTHONPATH=. .venv/bin/python -m unittest discover -s tests -p 'test_*.py'
```

## 授權與 CLA

公共版本採 [PolyForm Noncommercial License 1.0.0](./LICENSE)，不是 MIT／Apache，也
不是 OSI-approved open source。貢獻合併後會在相同的 source-available 非商業條款下
提供給社群。

為了讓 Project Owner 未來仍能提供商業授權或移轉整體專案，每位貢獻者必須接受
[CLA.md](./CLA.md)。你保留原始貢獻的著作權，同時授予 Project Owner 使用、修改、
再授權與商業散布該貢獻的權利。無法接受這個安排時，請不要送出程式碼；仍然很歡迎
透過 issue 提供問題描述與重現案例。
