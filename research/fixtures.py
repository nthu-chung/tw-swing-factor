# -*- coding: utf-8 -*-
"""golden path 的兩種資料來源:`synthetic`(離線)與 `local`(凍結資料)。

`synthetic` 存在的理由不是「省事」,而是要有一條**完全離線、可在 CI 跑、但仍然
經過真 validator / 真 policy / 真事件引擎**的路徑。它證明的是管線接通,絕對
不是策略有效 —— 所以它產生的結果一律標 `formal_evidence_ready=false`。

`local` 走 repo 的凍結資料與月頻 PIT 候選池。資料不齊全時**fail-closed 並列出
缺口**,不得自動開 `SWING_ALLOW_UNADJUSTED`、future pool 或任何逃生門。
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Mapping, Optional

import numpy as np
import pandas as pd

import config
import security_type
from factor_engine.panel_density import DENSE, tag


@dataclass
class Fixture:
    """一次 run 的資料來源。"""

    name: str
    panel: pd.DataFrame
    symbols: List[str]
    start_date: str
    end_date: str
    universe_kwargs: Dict[str, Any] = field(default_factory=dict)
    readiness: Dict[str, Any] = field(default_factory=dict)
    offline: bool = True
    # 這個 fixture 實際交給策略的資料窗。segment 邊界必須在**建 panel 時**就
    # 生效(規格 §2.1:「runner 不得先載入 OS 再只裁掉輸出」),所以截斷發生在
    # fixture 內、make_signals 之前;audit 會記錄策略實際收到的 input min/max。
    window: Optional[tuple] = None

    @contextlib.contextmanager
    def engine_context(self) -> Iterator[None]:
        yield


class SyntheticFixture(Fixture):
    """離線 fixture:價格由 seeded RNG 生成,引擎的資料層在 context 內被接管。

    這裡用 `unittest.mock.patch` 只接管**資料抓取**(價格、處置、價格完整性),
    引擎的成交、漲跌停、成本、股數、T+1 與 policy 全部照跑 —— 也就是說,
    被替換掉的只有「資料從哪來」,不是「回測怎麼算」。
    """

    def __init__(self, *, prices: Mapping[str, pd.DataFrame], **kwargs) -> None:
        super().__init__(**kwargs)
        self._prices = {k: v.copy() for k, v in prices.items()}
        self.offline = True

    @contextlib.contextmanager
    def engine_context(self) -> Iterator[None]:
        from unittest import mock

        from backtest import event_backtest

        registry = {sid: ("twse", "半導體業", f"合成{sid}") for sid in self.symbols}
        previous = security_type.registry_snapshot() \
            if hasattr(security_type, "registry_snapshot") else None
        security_type.set_registry(registry)
        try:
            with (
                mock.patch.object(event_backtest, "_assert_price_integrity",
                                  lambda *a, **k: None),
                mock.patch.object(event_backtest, "_load_disposition_days",
                                  lambda *a, **k: {}),
                mock.patch.object(
                    event_backtest.data, "fetch_price",
                    side_effect=lambda sid, *a, **k: self._prices[str(sid)].copy()),
            ):
                yield
        finally:
            if previous is None:
                security_type.reset_registry()
            else:
                security_type.set_registry(previous)


def synthetic_fixture(*, n_symbols: int = 12, n_days: int = 130,
                      seed: int = 20260816) -> SyntheticFixture:
    """建一份確定性的合成 panel + 價格。

    刻意讓標的之間有穩定的強弱差(不是純隨機),否則排名每天亂跳,選股路徑
    退化成雜訊,管線雖然跑得完卻驗不出「訊號真的影響了決策」。
    """
    rng = np.random.default_rng(seed)
    days = pd.bdate_range("2026-01-05", periods=n_days)
    symbols = [f"9{i:03d}" for i in range(1, n_symbols + 1)]

    prices: Dict[str, pd.DataFrame] = {}
    rows: List[Dict[str, Any]] = []
    for j, sid in enumerate(symbols):
        drift = 0.0016 - j * 0.00035          # 由強到弱的穩定梯度
        px = 60.0 + j * 7.0
        closes: List[float] = []
        for _ in days:
            px *= float(1.0 + drift + rng.normal(0.0, 0.011))
            px = max(px, 5.0)
            closes.append(round(px, 2))
        close = np.asarray(closes, dtype=float)
        opens = np.round(close * (1.0 + rng.normal(0.0, 0.002, len(close))), 2)
        highs = np.round(np.maximum(close, opens) * 1.004, 2)
        lows = np.round(np.minimum(close, opens) * 0.996, 2)
        volume = np.round(1_000_000 + rng.normal(0, 40_000, len(close))).clip(1e5)
        prices[sid] = pd.DataFrame({
            "date": days, "open": opens, "high": highs, "low": lows,
            "close": close, "volume": volume, "turnover": close * volume,
        })
        flow = rng.normal((n_symbols - j) * 900.0, 2_500.0, len(close))
        rows.append(pd.DataFrame({
            "date": days, "stock_id": sid, "close": close,
            "volume": volume, "turnover": close * volume,
            "foreign_net": flow, "trust_net": flow * 0.4,
            # 合成資料沒有兩層 universe 的概念:候選池 = 成員 = 全部。欄位仍要在,
            # 否則預設 ranking_universe="pool" 會 fail-closed —— 那是對的行為,
            # 但對合成 fixture 來說「缺欄位」不是缺陷,是本來就沒有那個維度。
            "in_dynamic_universe": True, "in_candidate_pool": True,
            "trend_ok": True,
        }))

    panel = tag(pd.concat(rows, ignore_index=True)
                .sort_values(["stock_id", "date"]).reset_index(drop=True), DENSE)
    return SyntheticFixture(
        prices=prices, name="synthetic", panel=panel, symbols=symbols,
        start_date=str(days[0].date()), end_date=str(days[-1].date()),
        universe_kwargs={"symbols": symbols, "sample": False,
                         "dynamic_enabled": True,
                         "static_universe_comparator": True},
        readiness={"source": "synthetic_seeded_rng", "seed": int(seed),
                   "offline": True,
                   "note": "合成資料只證明管線接通,不是任何績效證據"},
    )


class LocalDataReadinessError(RuntimeError):
    """本地凍結資料不足以跑正式 reference run;附精確缺口清單。"""


def local_fixture(*, candidate_pool_n: Optional[int] = None) -> Fixture:
    """走 repo 凍結資料 + 月頻 PIT 候選池。資料不齊全 fail-closed。"""
    gaps: List[str] = []
    details: Dict[str, Any] = {"source": "local_frozen_data", "offline": False}

    snapshot = str(getattr(config, "SNAPSHOT_END_DATE", "") or "").strip()
    details["snapshot_end_date"] = snapshot
    if not snapshot:
        gaps.append("config.SNAPSHOT_END_DATE 未鎖:回測結果會隨日曆漂移")

    if not getattr(config, "FINMIND_TOKEN", ""):
        gaps.append("FINMIND_TOKEN 未設定:凍結資料若有缺口將無法補抓"
                    "(而且不得用逃生門繞過)")

    for flag, why in (
        ("ALLOW_UNADJUSTED_BACKTEST", "未還原價逃生門"),
        ("ALLOW_FUTURE_POOL", "未來候選池逃生門"),
    ):
        if bool(getattr(config, flag, False)):
            gaps.append(f"{flag} 已開啟({why});reference run 不接受逃生門")

    universe = None
    if not gaps:
        try:
            from universes import historical_pit_universe
            kwargs = {}
            if candidate_pool_n:
                kwargs["candidate_pool_n"] = int(candidate_pool_n)
            universe = historical_pit_universe(**kwargs)
            details["pit_symbols"] = len(universe.symbols)
        except Exception as exc:                      # noqa: BLE001
            gaps.append(f"月頻 PIT 候選池建不起來:{type(exc).__name__}: {exc}")

    if gaps:
        raise LocalDataReadinessError(
            "[fail-closed] 本地凍結資料尚未 ready,不降級、不假造結果。缺口:\n  - "
            + "\n  - ".join(gaps))

    from strategies.s19_reference import S19ReferenceStrategy  # noqa: F401
    import strategies.s19_chip_momentum as s19

    panel, symbols = s19._build_pit_panel()

    # 成員資格必須同時滿足「當月 PIT 候選」與「當日 dynamic universe」。
    # 2026-08-16 實測發現的缺口:`_build_pit_panel()` 的 `in_dynamic_universe`
    # 是在**所有月份候選池的聯集**內排 ADV20,所以一檔股票可能在「它那個月
    # 根本不是候選」的日子被標成成員。引擎的 `_verify_external_picks_are_pit`
    # 會抓到(實測 921 筆 / 271 檔落在當日候選池外並 fail-closed),但那時已經
    # 是回測階段;正確的位置是資料層 —— 排名母體從一開始就不該包含非候選。
    # 這是 §3.1 的不變式在資料層的版本,修在這裡才不會讓每個策略各自處理。
    provider = universe.provider
    candidate = provider.candidate_mask(panel)
    before = int(panel["in_dynamic_universe"].fillna(False).astype(bool).sum())
    panel = panel.copy()
    panel["in_dynamic_universe"] = (
        panel["in_dynamic_universe"].fillna(False).astype(bool) & candidate)
    after = int(panel["in_dynamic_universe"].sum())
    details["membership_rows_before_pit_intersection"] = before
    details["membership_rows_after_pit_intersection"] = after
    details["membership_rows_dropped_not_candidate"] = before - after

    details["panel_rows"] = int(len(panel))
    details["panel_symbols"] = int(panel["stock_id"].nunique())
    return Fixture(
        name="local", panel=tag(panel, DENSE), symbols=list(symbols),
        start_date=str(pd.Timestamp(panel["date"].min()).date()),
        end_date=str(pd.Timestamp(panel["date"].max()).date()),
        universe_kwargs=dict(universe.backtest_kwargs()),
        readiness=details, offline=False,
    )


def apply_window(fixture: Fixture, window: Optional[tuple]) -> Fixture:
    """把 fixture 的 panel 截到 segment 窗內。

    這是 single-holdout 資料閘門的**唯一**截斷點,而且刻意放在 fixture 而不是
    runner 的輸出端:規格 §2.1 要求「不得先載入 OS 再只裁掉輸出」,§8.1 更要求
    能證明 OS 的列「從未進入 strategy」。放在這裡,策略拿到的 DataFrame 物理上
    就不含窗外的列 —— 測試可以塞一個窗外 sentinel 列然後斷言策略沒看到它。

    誠實聲明(對齊規格 §2.2):這是**程序性**閘門,不是物理沙盒。底層快取仍然
    存著整份 frozen snapshot,任意 Python 仍可自己去讀 `data.fetch_price`。
    這一層擋的是「IS 研究流程偷看 locked OS」,不是不受信任的程式碼。
    """
    if window is None:
        return fixture
    start, end = (pd.Timestamp(window[0]), pd.Timestamp(window[1]))
    if start > end:
        raise ValueError(f"[fail-closed] segment 窗顛倒:{start.date()} > {end.date()}")
    panel = fixture.panel
    inside = (panel["date"] >= start) & (panel["date"] <= end)
    dropped = int((~inside).sum())
    bounded = panel.loc[inside].reset_index(drop=True)
    if bounded.empty:
        raise ValueError(
            f"[fail-closed] segment 窗 [{start.date()}, {end.date()}] 內沒有任何資料")
    fixture.panel = tag(bounded, DENSE)
    fixture.window = (str(start.date()), str(end.date()))
    fixture.start_date = str(pd.Timestamp(bounded["date"].min()).date())
    fixture.end_date = str(pd.Timestamp(bounded["date"].max()).date())
    fixture.readiness = dict(fixture.readiness)
    fixture.readiness.update({
        "segment_window": fixture.window,
        "rows_dropped_outside_window": dropped,
        "panel_input_min": fixture.start_date,
        "panel_input_max": fixture.end_date,
    })
    return fixture


#: 合法的 fixture 名稱。抽成常數是為了讓呼叫端能**事前**驗證,而不是等到
#: `build_fixture()` 裡面才 raise —— `reveal_locked_os` 需要在寫揭露紀錄之前
#: 就知道名字對不對(打錯一個字母不該燒掉一次 fresh OOS 宣稱)。
KNOWN_FIXTURES = ("synthetic", "local")


def build_fixture(name: str, *, window: Optional[tuple] = None,
                  **kwargs) -> Fixture:
    if name == "synthetic":
        fixture = synthetic_fixture(**{k: v for k, v in kwargs.items()
                                       if k in ("n_symbols", "n_days", "seed")})
    elif name == "local":
        fixture = local_fixture(**{k: v for k, v in kwargs.items()
                                   if k in ("candidate_pool_n",)})
    else:
        raise ValueError(
            f"[fail-closed] 未知的 fixture={name!r};只接受 synthetic / local")
    return apply_window(fixture, window)
