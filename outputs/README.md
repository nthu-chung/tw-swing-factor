# 歷史研究輸出

此目錄中的 Markdown 是 append-only 研究紀錄，包含已撤回、被偏誤污染或只屬
pseudo-OOS 的舊結果。請勿因檔名含 `REPORT`、`OOS` 或 Sharpe 數字就視為目前有效。

判讀順序：

1. 先看 repo 根目錄的 `PUBLIC_REPO_AUDIT.md`、`STRATEGY_REGISTRY.md` 與
   `RESEARCH_OPERATING_PROTOCOL.md`。
2. 只有 metadata 顯示 PIT／價格完整性未 bypass、IS/OS 邊界可稽核、相位跑滿，且
   規則在 OS 之前已凍結的結果，才可進一步討論。
3. 目前已被看過或曾參與參數選擇的 OS 一律降級為 pseudo-OOS；clean 證據要走
   `freeze_manifest.py` → `forward_test.py`。
