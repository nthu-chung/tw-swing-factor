# Public repo 與研究有效性審計（2026-08-14，工程包 2026-08-15 補記）

## 結論

目前程式已具備可重跑的 long-only 台股事件驅動回測骨架，IS/OS 日期邊界、API
失敗、密鑰處理與多個「靜默降級」問題已改為 fail-closed。但 repo **還不能把既有
策略稱為 clean OOS 已驗證或 production-ready**：正式 `backtest/ic --full` 雖已改用
月頻 PIT 候選成員，完整下市股價格覆蓋與歷史產業分類仍未證明，且既有 OS 已被研究過。
下一份可升級的正式證據必須來自合格資料上的重跑，以及規則凍結後的 forward-only。

## 本次已修正

| 嚴重度 | 問題 | 修正與驗證 |
|---|---|---|
| Critical | 多個腳本各自切 60/40、70/30、日曆日近似或事件分位數 | 新增 `evaluation_split.py`；ratio 以排除 embargo 後的可用交易日維持真正 7:3，另支援固定 IS/OS 週數；所有 IS/OS 腳本改走單一入口 |
| Critical | 外部 picks 的 IS 權益曲線可能跑進 OS | 引擎預設截到最後訊號日，明確 `end_date` 仍是硬上界；核心入口逐相位檢查實際 `eval_window` 不得越界 |
| Critical | 核心 `main.py backtest` 仍只報全期單一路徑 | 改為 IS／embargo／OS，兩段都跑滿所有等價再平衡相位，輸出中位數與最小值 |
| Critical | 指定 top-N／全市場失敗時靜默改跑 14 檔 sample | `universe.py` 改為 raise，不再替換研究問題 |
| Critical | FinMind 連線、額度或認證失敗回空表 | 有界重試；401/402/403 等直接報錯；重試耗盡 raise，禁止把故障當「無資料」 |
| Critical | 自建還原失敗會退回原始價 | 改為 raise；除權息請求走 Authorization header，不把 token 放在 URL |
| Critical | 快照日期格式錯誤時退回 today | 改為 raise，避免環境變數 typo 讀到未來資料 |
| Important | 長停牌／下市用最後收盤假裝成交 | 預設 fail-closed；只有明確設定 `SWING_DELIST_RECOVERY=0~1` 才能做敏感度測試，假設會寫入 summary |
| Important | 處置模型開啟但缺市場快取時 no-op／半套放行 | 開啟後缺 TWSE 或 TPEx 任一側即 raise |
| Security | 跨 repo 自動讀取其他專案 `.env` | 移除；只接受 `FINMIND_TOKEN` 環境變數 |
| Security | TWSE／TPEx 使用 `verify=False` | 全部恢復 TLS 憑證驗證，不再關閉安全警告 |
| Security | `.gitignore` 密鑰涵蓋不足 | 加入 `.env.*`、私鑰／憑證、credentials/secrets JSON；提供無密鑰 `.env.example` |

## 切割設定

```bash
# 真正的 IS:OS = 7:3；embargo 另外排除，不會偷吃 OS 比例
export SWING_EVAL_SPLIT_MODE=ratio
export SWING_IS_RATIO=0.70
export SWING_EMBARGO_DAYS=20

# 或固定日曆週數：尾端 26 週 OS，往前留交易日 embargo，再取 52 週 IS
export SWING_EVAL_SPLIT_MODE=weeks
export SWING_IS_WEEKS=52
export SWING_OS_WEEKS=26
```

帶未來標籤的研究會要求 embargo 至少等於標籤 horizon；不足時直接拒跑。固定週數
模式只使用指定的尾端視窗，更早資料不會偷偷混入 IS。

## 尚未解除的研究阻擋

1. **正式多因子入口的候選成員已 PIT，但整體仍不是 survivorship-free。**
   `main.py backtest/ic --full` 走 `universes/monthly_pit.py`，M 月只使用完整 M-1
   曆月快照建立候選池；`--static-universe` 僅保留作 legacy 對照。尚未解除的是下市股
   完整還原價格覆蓋，因此 metadata 仍誠實標記 `survivorship_free=False`。
2. **既有 OS 已被看過。** 曾用受污染 IS/OS 選過權重與參數，所以舊 OS 只能叫
   pseudo-OOS。下一個 clean 證據只能是 `freeze_manifest.py` 之後累積的新資料。
3. **產業分類不是 PIT。** 目前歷史日期套用當前 FinMind 產業標籤；族群策略必須保留
   `industry_pit=False` 限制，取得歷史分類後才能升級。
4. **處置與下市資料仍不完整。** TWSE 歷史處置是由注意名單推導的 proxy；下市現金
   清算／換股條件尚無正式資料。程式現在會阻擋或要求顯式 recovery 假設，不再猜。
5. **歷史漲跌停／成交容量仍是近似。** 一字鎖停以 OHLC 判斷；沒有逐日委託簿、完整
   撮合量與個別股票特殊漲跌幅資料。結果仍需滑價與容量敏感度。

（原第 6 項「公開授權尚未決定」已移到本文件最後的 owner decision 一節。）

## 2026-08-15 補記：公開工程包（第一階段）

這一輪只做**工程包與文件一致性**，沒有改任何策略、因子、universe、回測或價格計算
語意，也沒有重抓資料或重跑績效。

| 項目 | 內容 |
|---|---|
| 離線 CI | `.github/workflows/ci.yml`：Python 3.11 → `pip install -r requirements.txt` → `compileall` 語法 smoke → `preflight.py` → `unittest discover -s tests -p 'test_*.py'` |
| 市場資料離線保證 | 測試步驟不設 `FINMIND_TOKEN`；`_cache/` 依設計不進版控，CI 上本來就沒有資料，任何誤走真實抓取的測試會 fail-closed 報錯而非靜默通過。GitHub Actions 與依賴安裝本身仍需連 GitHub／Python 套件站 |
| Release preflight | `preflight.py`（離線、唯讀）＋`tests/test_preflight.py` |
| 文件一致性 | README／HANDOFF／ARCHITECTURE 的架構描述對齊實際程式：事件驅動執行層、每月 PIT 候選池（只用完整 M-1 月）、daily dynamic universe、稠密 panel 算因子、data_fields／operators 分界、IS／embargo／OS、跑滿所有等價相位、價格完整性 fail-closed |
| 失效績效標示 | README 舊回測表與 IC 表標為 **HISTORICAL / INVALID** 並說明失效原因；HANDOFF 第 2 節加同等標示。**負面研究結論一律保留未改寫** |

### `preflight.py` 檢查的內容

1. Git 追蹤中的**檔名**是否命中密鑰型態（`.env`、`.env.*`、`*.pem`、`*.key`、
   `*.p12`、`id_rsa*`、`credentials*.json`、`secrets*.json`…；`.env.example` 白名單）。
2. Git 追蹤中的**文字檔內容**是否含私鑰 PEM／PGP／PuTTY 標頭、已填值的 token 指派、
   AWS key id、GitHub PAT、Slack token。
3. `_cache/` 一律不得追蹤；`outputs/` 只允許 `*.md`、`universe_top*.json`、
   `FROZEN_MANIFEST_*.json`；資料產物副檔名（csv/pkl/parquet/log…）不得追蹤。
4. 必要公開文件存在（不存在＝fail；存在但尚未 `git add`＝warn，因為那是 commit 前的
   正常中間狀態）、`.gitignore` 涵蓋必要規則、`.env.example` 的密鑰欄位為空值。

**設計鐵則：命中時只輸出「規則 + 檔案 + 行號」，不輸出比對到的內容。**
把疑似 token 印進 CI log 等於再洩一次；`tests/test_preflight.py` 有一條測試專門
釘住這件事，若有人把 match 內容加進訊息就會紅。

`preflight.py` 不取代託管平台的 secret scanning，也不是對任意自訂 token 格式的
數學保證——它擋的是**已知型態與誤提交路徑**。

## Owner decision（審計者不代為決定）

- [ ] **公開授權條款（LICENSE）。** repo 目前沒有 `LICENSE`，因此**預設為保留所有
      權利**，外部使用者沒有明示授權可用。公開前需 owner 選定（常見選項：MIT ——
      最寬鬆；Apache-2.0 —— 含專利授權條款；或不開放授權僅公開閱覽）。
      `preflight.py` 把這件事列為 `owner_decision`、**不擋 CI**，因為授權是法律與
      意願問題，不是稽核腳本能替代的判斷。
- [ ] **公開前旋轉曾經外流風險的 token。** 任何曾出現在終端輸出、截圖或未追蹤檔案裡的
      `FINMIND_TOKEN` 都應旋轉；掃描沒命中不等於沒外流過。
- [ ] **在託管平台開啟 secret scanning 與 push protection**，作為 `preflight.py` 之外的
      第二道防線。
- [ ] **決定 `outputs/` 舊研究報告是否隨 repo 公開。** 它們是 append-only 歷史紀錄
      （含已撤回結論），目前判定為有保留價值；若不希望公開失敗過程，需 owner 決定移除。

## Public repo 安全檢查

- 目前工作樹以密鑰規則與 entropy scanner 掃描，未找到實際 credential；唯一命中是
  工具快取的固定簽章，已排除工具快取目錄。
- Git 歷史以常見雲端、GitHub、Slack、私鑰與 `FINMIND_TOKEN=` 型態掃描，未命中。
  這降低風險，但不是對任意自訂 token 格式的數學保證；公開前仍應在託管平台再跑
  secret scanning，並旋轉任何曾在終端／截圖／未追蹤檔分享過的 token。
- 目前 `.venv` 的依賴稽核在升級 `setuptools` 後回報 `No known vulnerabilities found`。
  這只是 2026-08-14 當下的 advisory 結果，應在 CI 定期重跑。
- `_cache/`、非 Markdown outputs、`.env*`、私鑰與憑證均不進版控；追蹤中的歷史
  Markdown 報告必須視為研究紀錄，不是目前有效績效。

## 驗證範圍

- `PYTHONPATH=. .venv/bin/python -m unittest discover -s tests -p 'test_*.py'`
- `PYTHONPATH=. .venv/bin/python preflight.py`
- Python 全專案語法編譯
- `git diff --check`
- 修改檔的致命 Ruff 規則（語法、未定義名稱等）
- 依賴 advisory audit、目前檔案與 Git 歷史密鑰型態掃描

沒有在本次審計中重抓全市場或重跑昂貴策略績效；因此本文件確認的是架構與離線
回歸閘門，不是宣稱任何策略已重新通過 IS/OS。

**這一點不會因為工程包完成而改變：CI 綠燈只代表「程式沒壞、沒外洩、文件齊全」，
不代表任何策略有效。** 研究證據等級一律以 `STRATEGY_REGISTRY.md` 為準。
