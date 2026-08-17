# Evaluation Protocols

這裡只放可進版控的 evaluation protocol 樣板或經 owner 凍結的研究協議，不放市場
資料、訊號、回測結果或 holdout ledger。

- `single_holdout.example.json` 是欄位骨架，不可直接當正式協議。
- 真正執行前必須填入固定交易日、snapshot identity 與內容 fingerprint。
- protocol 一旦用於選策略或揭露 OS，不得覆寫原檔；建立新版本並產生新
  `evaluation_run_hash`。
- 策略可調參數不放在這裡；它們屬於 `CandidateSpec`／strategy rule hash。
- 執行產物仍寫到 `outputs/research_runs/<run_id>/`，holdout 揭露由既有
  `outputs/holdout_ledger.jsonl` 記錄。
