"""Long-only strategies for spot crypto, collected from well-known public playbooks.

Every strategy returns the input frame plus these columns, all known at the
*open* of each bar (no look-ahead — see tests/test_strategies.py):

    enter       bool   buy at this bar's open
    exit        bool   sell at this bar's open (only positions opened in earlier bars)
    entry_stop  float  buy-stop level active during this bar; NaN = none (max 1 fill/day)
    stop_dist   float  stop-loss distance below the fill price for a new position; NaN = none
    size        float  fraction (0..1) of the per-market allocation to use
    vol         float  std of the last 20 completed daily returns (for inverse-vol weighting)

Days are UTC days, which matches Upbit's daily candle (09:00 KST).
"""
import itertools

import numpy as np
import pandas as pd

# -- indicators -------------------------------------------------------------


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    down = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / down)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def daily(df: pd.DataFrame) -> tuple[pd.DatetimeIndex, pd.DataFrame]:
    day = df.index.floor("D")
    g = df.groupby(day)
    d = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(),
                      "low": g["low"].min(), "close": g["close"].last()})
    return day, d


def _finish(df, enter=None, exit=None, entry_stop=None, size=None, stop_atr=0.0) -> pd.DataFrame:
    def flag(s):
        return pd.Series(False, index=df.index) if s is None else s.astype("boolean").fillna(False).astype(bool)

    out = df.copy()
    out["enter"] = flag(enter)
    out["exit"] = flag(exit)
    out["entry_stop"] = np.nan if entry_stop is None else entry_stop
    out["stop_dist"] = stop_atr * atr(df).shift(1) if stop_atr > 0 else np.nan
    out["size"] = 1.0 if size is None else size.clip(0, 1).fillna(0.0)
    day, d = daily(df)
    out["vol"] = d["close"].pct_change().rolling(20).std().shift(1).reindex(day).to_numpy()
    return out


# -- strategies -------------------------------------------------------------


def vbo(df, k=0.5, ma=5, target_vol=0.0, stop_atr=0.0):
    """Larry Williams volatility breakout (the classic Korean pyupbit bot).

    Buy when price crosses today's open + k * yesterday's range; sell at the next
    day's open. k <= 0 uses the 20-day average noise ratio as k. `ma` only trades
    when today's open is above the N-day close average. `target_vol` scales size
    down on volatile days (size = target_vol / yesterday's range %).
    """
    day, d = daily(df)
    rng = (d["high"] - d["low"]).shift(1)
    if k > 0:
        kk = k
    else:
        noise = 1 - (d["close"] - d["open"]).abs() / (d["high"] - d["low"])
        kk = noise.rolling(20).mean().shift(1)
    target = d["open"] + kk * rng
    ok = target.notna()
    if ma:
        ok &= d["open"] > d["close"].rolling(ma).mean().shift(1)
    size_d = pd.Series(1.0, index=d.index)
    if target_vol:
        size_d = target_vol / (rng / d["close"].shift(1))
    per_bar = lambda s: pd.Series(s.reindex(day).to_numpy(), index=df.index)  # noqa: E731
    first_bar = pd.Series(day, index=df.index).diff().ne(pd.Timedelta(0))
    return _finish(df, exit=first_bar, entry_stop=per_bar(target.where(ok)),
                   size=per_bar(size_d), stop_atr=stop_atr)


def donchian(df, n=48, m=24, stop_atr=2.0):
    """Turtle-style channel breakout: buy a close above the n-bar high, sell a close below the m-bar low."""
    upper = df["high"].rolling(n).max().shift(1)
    lower = df["low"].rolling(m).min().shift(1)
    return _finish(df, enter=(df["close"] > upper).shift(1), exit=(df["close"] < lower).shift(1), stop_atr=stop_atr)


def rsi_mr(df, n=2, lo=10, hi=70, trend=200, stop_atr=2.0):
    """Connors RSI-2 mean reversion: buy oversold dips inside an uptrend, sell the bounce."""
    r = rsi(df["close"], n)
    cond = r < lo
    if trend:
        cond &= df["close"] > sma(df["close"], trend)
    return _finish(df, enter=cond.shift(1), exit=(r > hi).shift(1), stop_atr=stop_atr)


def ema_cross(df, fast=24, slow=120, stop_atr=2.0):
    """Moving-average trend following: buy the golden cross, sell when fast < slow."""
    above = ema(df["close"], fast) > ema(df["close"], slow)
    cross_up = above & ~above.shift(1, fill_value=False)
    return _finish(df, enter=cross_up.shift(1), exit=(~above).shift(1), stop_atr=stop_atr)


def hold(df):
    """Benchmark: always long."""
    return _finish(df, enter=pd.Series(True, index=df.index))


STRATEGIES = {"vbo": vbo, "donchian": donchian, "rsi_mr": rsi_mr, "ema_cross": ema_cross, "hold": hold}

GRIDS = {
    "vbo": {"k": [0, 0.4, 0.5, 0.6], "ma": [0, 5, 10], "target_vol": [0, 0.03]},
    "donchian": {"n": [24, 48, 96, 168], "m": [12, 24, 48], "stop_atr": [2, 3]},
    "rsi_mr": {"n": [2, 4], "lo": [5, 10, 20], "hi": [60, 75], "stop_atr": [2, 3]},
    "ema_cross": {"fast": [12, 24, 48], "slow": [72, 120, 240], "stop_atr": [2, 3]},
}


def combos(name: str) -> list[dict]:
    grid = GRIDS.get(name, {})
    keys = list(grid)
    return [dict(zip(keys, vals)) for vals in itertools.product(*(grid[k] for k in keys))]


def confirmed(df: pd.DataFrame, btc_close: pd.Series) -> pd.Series:
    """Entry confirmation, adopted in results/edge_study.md: at least 3 of these 4 hold at the previous bar's close.
    The coin is above its 200-bar EMA; BTC is above its 200-bar EMA; the last day's volume is at least the 30-day
    daily average; the coin is no more than 3 ATR above its 50-bar EMA (not chasing)."""
    c, btc = df["close"], btc_close.reindex(df.index).ffill()
    d = int(pd.Timedelta(days=1) / (df.index[1] - df.index[0]))  # bars per day
    votes = pd.DataFrame({"trend": c > ema(c, 200), "btc": btc > ema(btc, 200),
                          "volume": df["volume"].rolling(d).sum() >= df["volume"].rolling(30 * d).sum() / 30,
                          "not_chasing": c <= ema(c, 50) + 3 * atr(df)}).shift(1)
    return votes.fillna(False).astype(int).sum(axis=1) >= 3


def prepare(name: str, df: pd.DataFrame, params: dict | None = None, btc: pd.Series | None = None) -> pd.DataFrame:
    """Strategy columns for `df`. With `btc` (BTC closes), entries also need the 3-of-4 confirmation."""
    out = STRATEGIES[name](df, **(params or {}))
    if btc is not None and name != "hold":
        bad = ~confirmed(df, btc).to_numpy()
        out["enter"] &= ~bad
        out.loc[bad, "entry_stop"] = np.nan
    return out
