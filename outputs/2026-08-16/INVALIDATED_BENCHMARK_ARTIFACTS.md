# 2026-08-16 首批假說 artifacts 作廢說明

下列已存在的 artifacts 使用舊 benchmark `equal_weight_hold`，其母體錯誤包含了稠密
panel 中當日不可買的股票：

- `protocol.json`
- `hypothesis_leaderboard.csv`
- `runs/is-h1_volume_breakout__*/`
- `runs/is-h2_inst_persistence__*/`
- `runs/is-h3_short_reversal__*/`

這些檔案保留是為了不可改寫的研究歷史，**不得當作目前有效的 benchmark、超額報酬或
candidate 判定來源**。尤其 H2 原始 `summary.json` 的 `excess_vs_benchmark=+0.0729`
已作廢；以修正後「當日 eligible 母體每日等權」重算為約 `-0.1427`，因此 H2 狀態為
`rejected`。

修正後的 runner 使用 `daily_equal_weight_rebalanced_eligible`。任何新研究必須建立新
run directory，不得覆寫這批歷史檔；locked OS 仍未揭露。
