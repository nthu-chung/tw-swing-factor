# Quant Research 設計與協議骨架

這個資料夾只保存「研究 runner 的契約與設計邊界」,不放策略結果、資料快取或
campaign 產物。

## 現有程式責任

```text
backtest/
├── __init__.py             只有說明,不轉出任何東西(兩個引擎都必須指名)
└── event_backtest.py       事件引擎:T+1／漲跌停／處置／整股／成本;唯一可作正式證據

strategies/                 **純策略** —— 這個資料夾裡每個 .py 都是一支策略
├── h1_volume_breakout.py   h3_short_reversal.py
├── h5..h9_*.py             流量／低波動／融資融券等族群的假說
├── s19_reference.py        無績效宣稱的管線驗收策略
└── s19_chip_momentum.py    legacy 端到端模組(會改全域 config,不適合平行 GA)

strategy_kit/               機器 —— 策略要用的東西,但它們不是策略
├── signal_builder.py       分數 → 合格 SignalFrame 的翻譯層
├── contracts.py            DataRequirements / SignalContext
├── registry.py             allowlisted strategy registry(逐檔顯式註冊)
├── spec.py                 凍結用的 StrategySpec
└── position_policy.py      分數 → 想要的部位

research/
├── contracts.py            CandidateSpec / EvaluationProtocol / BacktestRequest
├── signal_validation.py    repo/external SignalFrame 共用 validator
├── fixtures.py             synthetic 與 local frozen-data fixture
├── golden_path.py          strategy → 事件引擎 → artifacts 的 orchestration
├── screening.py            人類可讀候選清單(signal artifact 的薄視圖)
├── artifacts.py            immutable run artifacts
├── holdout.py              single-holdout 資料閘門
├── protocols/              single-holdout protocol 樣板／凍結協議
└── docs/                   設計與邊界

evaluation/
├── splits.py               現有 IS / embargo / OS 唯一切割實作
├── phases.py               共用 phase sweep(唯一實作,AST 守衛禁止第二份)
└── holdout.py              append-only holdout reveal ledger
```

`research/` 不得建立第二套回測引擎,也不得重寫 `evaluation/splits.py` 或
`evaluation/holdout.py`。新的 runner 應組合既有能力並補上不可繞過的區段級資料
存取邊界。

## 文件

- [EVALUATION_DATA_BOUNDARY_SPEC.md](./EVALUATION_DATA_BOUNDARY_SPEC.md):V1 單次
  IS／embargo／locked OS、warmup、一次揭露與明確延後項目。
- [`../protocols/`](../protocols/):可進版控的 evaluation protocol 樣板;真正執行前
  必須填入固定日期與 snapshot identity。

先前四份逐步交接用的 GOAL 文件(make_signals golden path、single holdout、
golden path remediation、screener completion)都已執行完畢並刪除 —— 它們的結論
已經落進程式碼註解、契約測試與 `STRATEGY_REGISTRY.md`。要查當時的規格請翻 git log;
留著只會讓下一個讀的人以為還有事沒做。

## 目前狀態

Golden Path 已可驗證 Python `make_signals()` 到事件引擎、artifacts 與人類可讀候選
清單的機械鏈路;single-holdout 邊界也已就位(一般研究只讀 IS,策略凍結且 owner
授權後才可揭露 OS)。**這不代表任何策略已有 edge,也不代表搜尋／GA 已能安全使用。**

V1 不做 rolling／walk-forward,也不做逐決策日資料沙盒。策略可在獲准的 IS 或 OS
區段內向量化計算;IS 內部因果性由 operators 契約、prefix-invariance 測試與 code
review 負責。這是明確的速度／隔離強度取捨,不可描述成已具備任意 Python 沙盒。

執行產物應寫入 `outputs/research_runs/<run_id>/` 或呼叫端指定的暫存目錄;不得放進
本資料夾,也不得加入 git。
