# -*- coding: utf-8 -*-
"""
因子算子庫（operators）— 台股日頻 panel 版,對齊 WorldQuant 語意
================================================================
把散落各處(factors._scale、sector_rotation/rotation_research 的 rank、
factor_audit 的 industry-neutral、market_flow_monitor 的 cross-section zscore)
的算子收斂成一組**可組合、且因果/PIT 安全**的算子,規格參考
`WorldQuant-main/operators.json`(移植 Cross-Sectional / Group / Time-Series /
Arithmetic 的核心子集;Vector/Reduce/Special 等多值/order-book 算子不移植)。

三種語意(對應 WQ 的三類 grouping)：
  - **時序 ts_***(by 個股,只看過去 d 天含當日)：ts_delay/delta/mean/std/zscore/
    rank/scale/sum/min/max/median/returns/ir/decay_linear/arg_max/arg_min/corr。
  - **橫斷面 cs_***(當日跨股)：cs_rank/zscore/winsorize/scale/normalize/quantile/
    demean/one_side。
  - **族群 group_***(當日 × 產業等分組)：group_rank/zscore/neutralize/mean/scale。
  - **elementwise**：signed_power/s_log_1p/sigmoid/log_diff/clamp/if_else/bucket。

因果保證：ts_* 一律 groupby(個股) 後在「日期排序」序列上 rolling/shift(min_periods=d),
只用當日與過去;cs_*/group_* 一律 groupby(當日[/×產業]) 只用同一橫斷面 → 不看未來、
不跨未來洩漏。panel index 需唯一(_prepare_panel 已 reset_index)。

稠密度保證：ts_* 只算「20 列」而不是「20 交易日」,所以在只留動態 universe 成員日的
panel 上一律 **fail-closed raise**(見 `panel_density`);cs_*/group_* 只看當日橫斷面,
不受影響、照常放行。因子要在稠密 panel 上算,成員過濾留到選股階段。

排名母體(`ranking_mask`)：稠密 panel 為了 ts_ 而保留非成員列,但**橫斷面算子不該
把那些列算進母體**。同一份稠密 panel 上,`cs_rank` 的母體可以是全 panel、當月候選池
或當日可買成員 —— 三者給出的分數不同,而差異在單一 cs_ 算子下看不出來(同日單調
轉換),只在**兩個以上 cs_ 加權組合**時才會不對稱地扭曲順序。所以母體必須是呼叫端
的顯式決定,而不是「panel 剛好有哪些列」:

    ops = PanelOps(panel["date"], panel["stock_id"],
                   ranking_mask=panel["in_candidate_pool"],   # 當月候選池
                   ranking_universe="pool")
    ops.cs_rank(x)      # 只在遮罩內排名;遮罩外一律 NaN
    ops.ts_ir(ret, 20)  # 不受遮罩影響,仍看完整稠密序列

遮罩只作用在 cs_* / group_* / regression_* / bucket(見 `_cross_sectional` 裝飾器,
`tests/test_operators_ranking_universe.py` 會擋住漏標的新算子);ts_* 一律看完整
序列,否則就退化成稀疏 panel 的失真問題。不傳 `ranking_mask` = 維持舊行為(全 panel)。

用法：
    ops = PanelOps(panel["date"], panel["stock_id"])
    z   = ops.cs_zscore(panel["mom_ret"])           # 當日跨股 z-score
    r   = ops.ts_rank(panel["close"], 20)           # 個股近20日時序 rank
    neu = ops.group_neutralize(panel["inst_6d"], group=industry_series)
"""
from __future__ import annotations

import functools
from typing import Callable, Optional

import numpy as np
import pandas as pd

from . import panel_density

try:
    from scipy.stats import norm as _norm
    _HAVE_SCIPY = True
except Exception:                       # scipy 缺就讓 cs_quantile 退化
    _HAVE_SCIPY = False


# ── elementwise（不需分組；純函數,對齊 WQ Arithmetic）──────────────────────
def sign(x: pd.Series) -> pd.Series:
    return np.sign(x)


def signed_power(x: pd.Series, a: float) -> pd.Series:
    """x^a 但保留 x 的正負號(WQ signed_power)。a=0.5 常用來壓尾、保方向。"""
    return np.sign(x) * (x.abs() ** a)


def s_log_1p(x: pd.Series) -> pd.Series:
    """sign(x)*log1p(|x|):壓縮量級、保方向、保 0(WQ s_log_1p)。"""
    return np.sign(x) * np.log1p(x.abs())


def log_diff(x: pd.Series) -> pd.Series:
    """log(x) - log(x_prev) 需搭配 ts_delay;此處提供對數(WQ log_diff 的元件)。"""
    return np.log(x.where(x > 0))


def sigmoid(x: pd.Series) -> pd.Series:
    return 1.0 / (1.0 + np.exp(-x))


def clamp(x: pd.Series, lower: float = 0.0, upper: float = 0.0) -> pd.Series:
    """把 x 夾在 [lower, upper](WQ clamp,inverse 模式未移植)。"""
    return x.clip(lower=lower, upper=upper)


def if_else(cond: pd.Series, a, b) -> pd.Series:
    """cond 為真取 a 否則取 b(WQ if_else);a/b 可為 Series 或純量。"""
    return pd.Series(np.where(cond.fillna(False).to_numpy(), a, b), index=cond.index)


# ── 橫斷面算子的排名母體裝飾器 ─────────────────────────────────────────────
def _cross_sectional(fn: Callable) -> Callable:
    """標記一個方法是**橫斷面**算子,並讓它自動套用 `PanelOps` 的排名母體。

    為什麼用裝飾器而不是在每個方法裡自己遮:漏掉一個就是一個安靜錯掉的母體,
    而母體錯了不會 crash,只會讓分數變成另一套。標記本身也讓
    `tests/test_operators_ranking_universe.py` 能反過來掃「有沒有算子忘了標」——
    新增算子時忘記處理母體會被測試擋下來,而不是等到寫報告才發現。

    語意:遮罩外的列在**輸入**就被轉成 NaN(所以不進 mean/std/rank 的母體),
    輸出也一律 NaN(所以不會有「不在母體卻拿得到分數」的列流到下游)。
    """
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        if self._cs_mask is None:
            return fn(self, *args, **kwargs)
        name = fn.__name__
        args = tuple(self._cs_scope_in(a, name) for a in args)
        kwargs = {k: self._cs_scope_in(v, name) for k, v in kwargs.items()}
        return self._cs_scope_out(fn(self, *args, **kwargs))

    wrapper._is_cross_sectional = True
    return wrapper


# ── PanelOps:綁定 (date, stock) 鍵,提供因果/橫斷面/族群算子 ────────────────
class PanelOps:
    """綁定一個 long panel 的 date/stock 鍵,方法回傳與 panel index 對齊的 Series。

    `ranking_mask` 決定橫斷面算子的母體(見模組 docstring);不傳 = 全 panel。
    `ranking_universe` 只是那個母體的名字,會被呼叫端寫進 provenance,讓事後看得出
    當時的分數是在哪個母體上算的 —— 母體不留痕跡等於數字無法重現。
    """

    def __init__(self, date: pd.Series, stock: pd.Series, *,
                 ranking_mask: Optional[pd.Series] = None,
                 ranking_universe: str = "panel"):
        if not date.index.equals(stock.index):
            raise ValueError("date 與 stock 的 index 必須一致")
        if date.index.duplicated().any():
            raise ValueError("panel index 需唯一(先 reset_index(drop=True))")
        # 稠密度標籤要在 pd.to_datetime 之前抓:轉型會產生新 Series、attrs 就沒了。
        # 取自 panel 的欄位(attrs 會從 DataFrame 傳播到單欄),未標記 = 未知 = 放行。
        self._panel_density = (panel_density.density_of(date)
                               or panel_density.density_of(stock))
        self.date = pd.to_datetime(date)
        self.stock = stock.astype(str)
        self._orig_index = date.index
        # 供 ts_* 用:依 (個股, 日期) 排序後的 index 順序
        order = pd.DataFrame({"s": self.stock, "d": self.date}, index=self._orig_index)
        self._sorted_index = order.sort_values(["s", "d"], kind="stable").index
        self._stock_sorted = self.stock.loc[self._sorted_index]
        self.ranking_universe = str(ranking_universe)
        self._cs_mask = self._validate_ranking_mask(ranking_mask)

    # ---- 內部:橫斷面排名母體 ----
    def _validate_ranking_mask(self, mask) -> Optional[pd.Series]:
        """把排名母體正規化成對齊 panel 的 bool Series;不合法一律 fail-closed。"""
        if mask is None:
            return None
        if not isinstance(mask, pd.Series):
            raise TypeError("ranking_mask 必須是與 panel 對齊的 bool Series")
        if not mask.index.equals(self._orig_index):
            raise ValueError(
                "[fail-closed] ranking_mask 的 index 與 panel 不一致。"
                "錯位的遮罩會把 A 股票的成員資格套到 B 股票身上,而結果看起來完全正常")
        out = mask.fillna(False).astype(bool)
        if not out.any():
            raise ValueError(
                "[fail-closed] ranking_mask 全為 False:橫斷面母體是空的,"
                "所有 cs_ 算子都會回 NaN。這通常代表傳錯欄位,不是真的沒有成員")
        return out

    def _cs_scope_in(self, value, op_name: str):
        """把輸入限縮到排名母體:母體外轉 NaN,才不會進 rank/mean/std 的計算。"""
        if isinstance(value, (list, tuple)):
            return type(value)(self._cs_scope_in(v, op_name) for v in value)
        if not isinstance(value, pd.Series):
            return value                      # 純量參數(視窗、side、rettype…)
        if not value.index.equals(self._orig_index):
            raise ValueError(
                f"[fail-closed] PanelOps.{op_name}:輸入 Series 的 index 與 panel "
                "不一致,無法套用排名母體。請先對齊到同一個 panel index")
        return value.where(self._cs_mask)

    def _cs_scope_out(self, result):
        """母體外的列不得帶著分數離開:它們當天本來就不在這個橫斷面裡。"""
        if not isinstance(result, pd.Series):
            result = pd.Series(result, index=self._orig_index)
        return result.where(self._cs_mask)

    # ---- 內部:稀疏 panel 上禁止時序運算（不變式 3 的第二道防線）----
    def _require_dense(self, op_name: str) -> None:
        """ts_ 類算子在「只留成員日」的 panel 上一律 fail-closed。

        為什麼要擋在算子這一層:因子失真不會 crash,只會讓 Sharpe 變好看,所以
        必須在計算發生的那一刻擋住,而不是靠呼叫端記得傳 keep_non_members=True。
        panel 沒有稠密度標籤(例如手寫的測試 panel)時放行 —— 見 panel_density。
        """
        panel_density.require_dense_density(
            self._panel_density,
            who=f"PanelOps.{op_name}", what=f"時序算子 {op_name}",
        )

    # ---- 內部:時序 rolling（因果,只看過去 d 含當日）----
    def _ts(self, x: pd.Series, d: int, func: Callable, min_periods: Optional[int] = None,
            raw: bool = False, use_apply: bool = False) -> pd.Series:
        self._require_dense("ts_*")
        if d < 1:
            raise ValueError("d 必須 >= 1")
        mp = d if min_periods is None else min_periods
        xs = x.loc[self._sorted_index]
        g = xs.groupby(self._stock_sorted, sort=False)

        def _roll(v: pd.Series) -> pd.Series:
            r = v.rolling(d, min_periods=mp)
            return r.apply(func, raw=raw) if use_apply else getattr(r, func)()

        res = g.transform(_roll)
        return res.loc[self._orig_index]

    def ts_delay(self, x: pd.Series, d: int) -> pd.Series:
        self._require_dense("ts_delay")
        xs = x.loc[self._sorted_index]
        res = xs.groupby(self._stock_sorted, sort=False).shift(d)
        return res.loc[self._orig_index]

    def ts_delta(self, x: pd.Series, d: int) -> pd.Series:
        return x - self.ts_delay(x, d)

    def ts_mean(self, x, d): return self._ts(x, d, "mean")
    def ts_std_dev(self, x, d): return self._ts(x, d, "std")   # 樣本 std(ddof=1)
    def ts_sum(self, x, d): return self._ts(x, d, "sum")
    def ts_min(self, x, d): return self._ts(x, d, "min")
    def ts_max(self, x, d): return self._ts(x, d, "max")
    def ts_median(self, x, d): return self._ts(x, d, "median")

    def ts_zscore(self, x, d):
        return (x - self.ts_mean(x, d)) / self.ts_std_dev(x, d).replace(0, np.nan)

    def ts_ir(self, x, d):
        return self.ts_mean(x, d) / self.ts_std_dev(x, d).replace(0, np.nan)

    def ts_returns(self, x, d):
        return x / self.ts_delay(x, d) - 1.0

    def ts_scale(self, x, d):
        lo, hi = self.ts_min(x, d), self.ts_max(x, d)
        return (x - lo) / (hi - lo).replace(0, np.nan)

    def ts_rank(self, x, d):
        """當前值在過去 d 天的時序 rank,正規化到 [0,1](WQ ts_rank)。"""
        def _r(a):
            n = len(a)
            if n < 2:
                return np.nan
            return (a.argsort().argsort()[-1]) / (n - 1)
        return self._ts(x, d, _r, raw=True, use_apply=True)

    def ts_arg_max(self, x, d):
        """過去 d 天內最大值的相對位置(0=當日即最大;WQ ts_arg_max)。"""
        return self._ts(x, d, lambda a: (len(a) - 1) - int(np.argmax(a)), raw=True, use_apply=True)

    def ts_arg_min(self, x, d):
        return self._ts(x, d, lambda a: (len(a) - 1) - int(np.argmin(a)), raw=True, use_apply=True)

    def ts_decay_linear(self, x, d):
        """過去 d 天線性加權(越近權重越大;WQ ts_decay_linear,sparse=NaN 視為 0)。"""
        w = np.arange(1, d + 1, dtype=float)
        w /= w.sum()

        def _dw(a):
            a = np.where(np.isnan(a), 0.0, a)
            return float(np.dot(a, w))
        return self._ts(x, d, _dw, raw=True, use_apply=True, min_periods=d)

    # 用動差公式算成對統計(全部走已驗證的 ts_mean transform,避開 groupby.apply
    # 在單股/index 對齊上的雷)。母體矩(ddof=0);OLS 斜率 b=cov/var 時 ddof 相消。
    def ts_cov(self, x, y, d):
        """個股 x,y 過去 d 天滾動共變異(因果,母體矩)。"""
        return self.ts_mean(x * y, d) - self.ts_mean(x, d) * self.ts_mean(y, d)

    def ts_var(self, x, d):
        """個股 x 過去 d 天滾動變異數(因果,母體矩;供迴歸斜率分母)。"""
        return self.ts_mean(x * x, d) - self.ts_mean(x, d) ** 2

    def ts_corr(self, x, y, d):
        """個股 x,y 過去 d 天滾動相關(因果)。"""
        denom = np.sqrt(self.ts_var(x, d) * self.ts_var(y, d))
        return self.ts_cov(x, y, d) / denom.replace(0, np.nan)

    # ── 差分族(WQ ts_*_diff;GA 常用,全部可由既有算子組出,這裡給名字)──
    def ts_av_diff(self, x, d):
        """x - ts_mean(x,d):偏離自身均值多少(WQ ts_av_diff)。NaN 不參與均值。"""
        return x - self.ts_mean(x, d)

    def ts_max_diff(self, x, d):
        return x - self.ts_max(x, d)

    def ts_min_diff(self, x, d):
        return x - self.ts_min(x, d)

    def ts_min_max_diff(self, x, d, f: float = 0.5):
        """x - f*(ts_min + ts_max):相對於視窗中軸的位置(WQ ts_min_max_diff)。"""
        return x - f * (self.ts_min(x, d) + self.ts_max(x, d))

    def ts_min_max_cps(self, x, d, f: float = 2.0):
        """(ts_min + ts_max) - f*x(WQ ts_min_max_cps)。"""
        return (self.ts_min(x, d) + self.ts_max(x, d)) - f * x

    # ── 分布形狀 ────────────────────────────────────────────────────────
    def ts_product(self, x, d):
        return self._ts(x, d, lambda a: float(np.prod(a)), raw=True, use_apply=True)

    def ts_skewness(self, x, d):
        return self._ts(x, d, "skew")

    def ts_kurtosis(self, x, d):
        return self._ts(x, d, "kurt")

    def ts_quantile(self, x, d, q: float = 0.5):
        """視窗內的第 q 分位(WQ ts_quantile 的 driver='uniform' 近似)。"""
        if not 0.0 <= q <= 1.0:
            raise ValueError("q 必須介於 0 與 1")
        return self._ts(x, d, lambda a: float(np.nanquantile(a, q)),
                        raw=True, use_apply=True)

    def ts_entropy(self, x, d, buckets: int = 10):
        """視窗內數值分布的 Shannon 熵(WQ ts_entropy)。分布越均勻值越大。"""
        def _e(a):
            a = a[np.isfinite(a)]
            if len(a) < 2:
                return np.nan
            lo, hi = float(np.min(a)), float(np.max(a))
            if hi == lo:
                return 0.0
            cnt, _ = np.histogram(a, bins=buckets, range=(lo, hi))
            p = cnt[cnt > 0] / cnt.sum()
            return float(-(p * np.log(p)).sum())
        return self._ts(x, d, _e, raw=True, use_apply=True)

    def ts_count_nans(self, x, d):
        return self._ts(x.isna().astype(float), d, "sum")

    # ── 補值 / 衰減 / 抑制周轉 ──────────────────────────────────────────
    def ts_backfill(self, x, d):
        """用過去 d 天內最後一個非 NaN 值補當前 NaN(WQ ts_backfill)。

        只往**過去**取,不會用到未來 —— 這點必須守住,否則等於前視補值。
        """
        self._require_dense("ts_backfill")
        xs = x.loc[self._sorted_index]
        filled = xs.groupby(self._stock_sorted, sort=False).ffill(limit=max(0, d - 1))
        return filled.loc[self._orig_index]

    def ts_decay_exp(self, x, d, factor: float = 0.5):
        """指數衰減加權(越近權重越大;WQ ts_decay_exp_window)。"""
        if not 0 < factor <= 1:
            raise ValueError("factor 必須介於 0(不含)與 1")
        w = factor ** np.arange(d - 1, -1, -1, dtype=float)
        w /= w.sum()

        def _dw(a):
            a = np.where(np.isnan(a), 0.0, a)
            return float(np.dot(a, w))
        return self._ts(x, d, _dw, raw=True, use_apply=True, min_periods=d)

    def last_diff_value(self, x, d):
        """過去 d 天內,最後一個「與當前值不同」的值(WQ last_diff_value)。"""
        def _ldv(a):
            cur = a[-1]
            if not np.isfinite(cur):
                return np.nan
            for v in a[-2::-1]:
                if np.isfinite(v) and v != cur:
                    return float(v)
            return np.nan
        return self._ts(x, d, _ldv, raw=True, use_apply=True, min_periods=1)

    def hump(self, x, threshold: float = 0.01):
        """限制每日變動幅度以抑制周轉(WQ hump)。

        y_t = y_{t-1} + clip(x_t - y_{t-1}, -threshold, +threshold)

        這是**路徑相依**的遞迴,必須逐股循序算。實測顯示周轉率是弱訊號策略的
        主導因素(見 chip_momentum_strategy 的說明),所以這個算子值得有。
        """
        self._require_dense("hump")
        xs = x.loc[self._sorted_index]
        out = np.full(len(xs), np.nan)
        vals = xs.to_numpy(dtype=float)
        codes = self._stock_sorted.to_numpy()
        prev, prev_sid = np.nan, None
        for i in range(len(vals)):
            sid, v = codes[i], vals[i]
            if sid != prev_sid:
                prev, prev_sid = np.nan, sid
            if not np.isfinite(v):
                out[i] = prev
                continue
            if not np.isfinite(prev):
                prev = v
            else:
                prev = prev + np.clip(v - prev, -threshold, threshold)
            out[i] = prev
        return pd.Series(out, index=xs.index).loc[self._orig_index]

    # ── 技術指標(有視窗 → 算子;無視窗的部分放 attach_fields)────────────
    def ts_rsi(self, x, d: int = 14):
        """RSI/100,回傳 [0,1](WQ 沒有內建,但可由 primitive 組出)。

        RSI = 上漲幅度總和 / 全部變動幅度總和。這裡直接給名字方便閱讀,
        GA 仍可用 ts_sum/ts_delta/elem_max 自行組出變體(那才是重點)。
        """
        delta = self.ts_delta(x, 1)
        up = self.ts_sum(delta.clip(lower=0.0), d)
        total = self.ts_sum(delta.abs(), d)
        return up / total.replace(0, np.nan)

    def ts_atr(self, true_range: pd.Series, d: int = 14):
        """ATR = true_range 的 d 日均值。true_range 由 attach_fields 提供。

        獨立成一個名字只是為了可讀;數學上就是 ts_mean(true_range, d)。
        """
        return self.ts_mean(true_range, d)

    def ts_bollinger_pos(self, x, d: int = 20, k: float = 2.0):
        """布林位階:(x - 中軌) / (k*std),0=中軌、+1=上軌、-1=下軌。"""
        mid = self.ts_mean(x, d)
        sd = self._ts(x, d, lambda a: float(np.nanstd(a)), raw=True, use_apply=True)
        return (x - mid) / (k * sd).replace(0, np.nan)

    def ts_vwap_dev(self, close: pd.Series, vwap: pd.Series, d: int = 20):
        """收盤相對於近 d 日均 VWAP 的偏離度。>0 = 買方持續推高於均價成本。"""
        return close / self.ts_mean(vwap, d).replace(0, np.nan) - 1.0

    def ts_regression(self, y: pd.Series, x: pd.Series, d: int,
                      lag: int = 0, rettype="beta") -> pd.Series:
        """個股層時序 OLS:過去 d 天把 y 迴歸到 x(y = a + b·x),全因果(WQ ts_regression)。

        lag:把 x 先延遲 lag 天再迴歸(用過去的 x 預測今天的 y)。
        rettype(可用字串或 WQ 數字碼):
          'beta'/'slope'/2  斜率 b       | 'alpha'/'intercept'/1  截距 a
          'pred'/'predicted'/3 當前擬合 ŷ | 'resid'/'residual'/0  當前殘差 y-ŷ
          'r2'/'rsquared'/4  判定係數 R²
        """
        xl = self.ts_delay(x, lag) if lag else x
        var = self.ts_var(xl, d)
        cov = self.ts_cov(xl, y, d)
        b = cov / var.replace(0, np.nan)
        a = self.ts_mean(y, d) - b * self.ts_mean(xl, d)
        if rettype in ("beta", "slope", 2):
            return b
        if rettype in ("alpha", "intercept", 1):
            return a
        pred = a + b * xl
        if rettype in ("pred", "predicted", 3):
            return pred
        if rettype in ("resid", "residual", 0):
            return y - pred
        if rettype in ("r2", "rsquared", 4):
            r = self.ts_corr(xl, y, d)
            return r * r
        raise ValueError(f"未知 rettype: {rettype}")

    # ---- 橫斷面 cs_*（當日跨股;不看未來;全向量化,避免自訂函數 transform）----
    def _by_date(self, x):
        return x.groupby(self.date, sort=False)

    @_cross_sectional
    def cs_rank(self, x):
        """當日跨股 rank → [0,1](WQ rank)。"""
        return self._by_date(x).rank(pct=True)

    @_cross_sectional
    def cs_zscore(self, x):
        """當日跨股 z-score,母體 std(ddof=0,對齊 WQ / market_flow_monitor)。"""
        g = self._by_date(x)
        m = g.transform("mean")
        sd = g.transform(lambda s: s.std(ddof=0))       # 純量 broadcast,安全
        return (x - m) / sd.replace(0, np.nan)

    @_cross_sectional
    def cs_demean(self, x):
        """當日減去橫斷面均值(WQ normalize,useStd=false)。"""
        return x - self._by_date(x).transform("mean")

    @_cross_sectional
    def cs_normalize(self, x, use_std: bool = False):
        return self.cs_zscore(x) if use_std else self.cs_demean(x)

    @_cross_sectional
    def cs_winsorize(self, x, std: float = 4.0):
        """當日把離群夾到 mean ± std*sd(WQ winsorize)。"""
        g = self._by_date(x)
        m = g.transform("mean")
        sd = g.transform(lambda s: s.std(ddof=0))
        return x.clip(m - std * sd, m + std * sd)

    @_cross_sectional
    def cs_scale(self, x, a: float = 1.0):
        """當日縮放到 sum(|x|)=a(WQ scale to booksize)。"""
        tot = x.abs().groupby(self.date, sort=False).transform("sum")
        return x * a / tot.replace(0, np.nan)

    @_cross_sectional
    def cs_one_side(self, x, side: str = "long"):
        """平移成 long-only(減當日最小)或 short-only(減最大)(WQ one_side)。"""
        agg = "min" if side == "long" else "max"
        return x - self._by_date(x).transform(agg)

    @_cross_sectional
    def cs_quantile(self, x):
        """當日 rank 後套高斯反 CDF(WQ quantile gaussian);無 scipy 則退回置中 rank。"""
        r = self._by_date(x).rank(pct=True).clip(1e-6, 1 - 1e-6)
        if _HAVE_SCIPY:
            return pd.Series(_norm.ppf(r.to_numpy()), index=r.index)
        return r - 0.5

    @_cross_sectional
    def bucket(self, x, n: int = 10):
        """當日 rank 後切成 n 個桶(0..n-1),可當 group 值(WQ bucket)。"""
        r = self._by_date(x).rank(pct=True, method="first")
        return np.floor(r * n).clip(0, n - 1)

    # ---- 族群 group_*（當日 × 分組,如產業;不看未來;向量化）----
    def _by_group(self, x, group):
        if isinstance(group, str):
            raise ValueError("group 請傳入與 panel 對齊的 Series(產業標籤),非欄名")
        return x.groupby([self.date, group.astype(str)], sort=False)

    @_cross_sectional
    def group_rank(self, x, group):
        return self._by_group(x, group).rank(pct=True)

    @_cross_sectional
    def group_mean(self, x, group):
        return self._by_group(x, group).transform("mean")

    @_cross_sectional
    def group_zscore(self, x, group):
        g = self._by_group(x, group)
        m = g.transform("mean")
        sd = g.transform(lambda s: s.std(ddof=0))
        return (x - m) / sd.replace(0, np.nan)

    @_cross_sectional
    def group_neutralize(self, x, group):
        """對每個(當日×族群)去均值(WQ group_neutralize;= factor_audit 的產業中性化)。"""
        return x - self._by_group(x, group).transform("mean")

    @_cross_sectional
    def group_std_dev(self, x, group):
        """當日族群內標準差(WQ group_std_dev);母體 std 對齊 group_zscore。"""
        return self._by_group(x, group).transform(lambda s: s.std(ddof=0))

    @_cross_sectional
    def group_median(self, x, group):
        return self._by_group(x, group).transform("median")

    @_cross_sectional
    def group_sum(self, x, group):
        return self._by_group(x, group).transform("sum")

    @_cross_sectional
    def group_count(self, x, group):
        """族群成員數;中性化前用來擋掉單一成員組(那種組中性化後恆為 0)。"""
        return self._by_group(x, group).transform("count")

    @_cross_sectional
    def group_scale(self, x, group):
        """(x - gmin)/(gmax - gmin),當日族群內縮到 [0,1](WQ group_scale)。"""
        g = self._by_group(x, group)
        lo = g.transform("min"); hi = g.transform("max")
        return (x - lo) / (hi - lo).replace(0, np.nan)

    # ---- 橫斷面單因子迴歸（當日跨股,y = a + b·x;不看未來）----
    def _cs_ab(self, y, x):
        """當日跨股 OLS 的 (a, b),向量化(cov/var 用母體矩)。回傳 (a_series, b_series)。"""
        by = self.date
        mx = x.groupby(by, sort=False).transform("mean")
        my = y.groupby(by, sort=False).transform("mean")
        mxy = (x * y).groupby(by, sort=False).transform("mean")
        mxx = (x * x).groupby(by, sort=False).transform("mean")
        b = (mxy - mx * my) / (mxx - mx * mx).replace(0, np.nan)
        a = my - b * mx
        return a, b

    @_cross_sectional
    def regression_proj(self, y, x):
        """當日跨股把 y 迴歸到 x,回傳擬合值 ŷ = a + b·x(WQ regression_proj)。"""
        a, b = self._cs_ab(y, x)
        return a + b * x

    @_cross_sectional
    def regression_neut(self, y, x):
        """當日跨股把 y 對 x 迴歸後的**殘差**(y 中性化掉 x 的成分;WQ regression_neut)。"""
        a, b = self._cs_ab(y, x)
        return y - (a + b * x)

    @_cross_sectional
    def multi_regression(self, y, xs, rettype="resid"):
        """當日跨股多因子 OLS(y ~ 1 + x1 + x2 + …),回傳殘差或擬合(WQ multi_regression)。

        xs:自變數 Series 清單。rettype='resid'(中性化 y)或 'pred'(擬合)。
        每個交易日獨立最小平方解;樣本不足(< 因子數+1)的當日回 NaN。
        """
        design = pd.concat([pd.Series(1.0, index=y.index, name="_const"), *xs], axis=1)
        out = pd.Series(np.nan, index=y.index, dtype=float)
        k = design.shape[1]
        for _, idx in y.groupby(self.date, sort=False).groups.items():
            yy = y.loc[idx]
            XX = design.loc[idx]
            m = yy.notna() & XX.notna().all(axis=1)
            if int(m.sum()) < k + 1:
                continue
            Xm = XX.loc[m].to_numpy(float)
            ym = yy.loc[m].to_numpy(float)
            beta, *_ = np.linalg.lstsq(Xm, ym, rcond=None)
            pred = XX.to_numpy(float) @ beta
            vals = pred if rettype == "pred" else (yy.to_numpy(float) - pred)
            out.loc[idx] = vals
        return out


# 對齊 factors._scale 的固定區間線性縮放(保留;與 cs_/ts_ 併用)
def scale_fixed(x: pd.Series, lo: float, hi: float) -> pd.Series:
    if hi == lo:
        return x * 0.0
    return ((x - lo) / (hi - lo)).clip(0.0, 1.0)


# ══════════════════════════════════════════════════════════════════════════
# 元素級輔助(給 GA 組表達式用;命名對齊 WQ)
# ══════════════════════════════════════════════════════════════════════════
def elem_max(a: pd.Series, b) -> pd.Series:
    """逐元素取大。b 可為 Series 或純量。"""
    return a.combine(b, max) if isinstance(b, pd.Series) else a.clip(lower=b)


def elem_min(a: pd.Series, b) -> pd.Series:
    return a.combine(b, min) if isinstance(b, pd.Series) else a.clip(upper=b)


def abs_(x: pd.Series) -> pd.Series:
    return x.abs()


def reverse(x: pd.Series) -> pd.Series:
    return -x
