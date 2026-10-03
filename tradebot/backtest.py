"""Bar-by-bar portfolio backtester with fees, slippage and the shared risk rules.

Fill model per bar: scheduled exits at the open -> entries (open or buy-stop)
-> stop-losses on the low (also for positions opened in this bar, which is the
conservative assumption) -> daily guard on the close.
"""
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .risk import Costs, DailyGuard, Risk, order_value

@dataclass
class Result:
    equity: pd.Series   # equity at each bar close
    trades: pd.DataFrame
    capital: float

    def daily_returns(self) -> pd.Series:
        daily = self.equity.resample("D").last().dropna()
        rets = daily.pct_change()
        rets.iloc[0] = daily.iloc[0] / self.capital - 1
        return rets

    def metrics(self) -> dict:
        return metrics(self.daily_returns(), self.trades)


def metrics(rets: pd.Series, trades: pd.DataFrame | None = None) -> dict:
    rets = rets.dropna()
    curve = (1 + rets).cumprod()
    days = max(len(rets), 1)
    total = curve.iloc[-1] - 1 if len(rets) else 0.0
    std = rets.std()
    out = {
        "days": len(rets),
        "total_return": total,
        "cagr": (1 + total) ** (365 / days) - 1 if total > -1 else -1.0,
        "avg_daily": rets.mean(),
        "median_daily": rets.median(),
        "sharpe": rets.mean() / std * np.sqrt(365) if std > 0 else 0.0,
        "max_drawdown": (curve / curve.cummax().clip(lower=1) - 1).min() if len(rets) else 0.0,
        "best_day": rets.max(),
        "worst_day": rets.min(),
        "days_ge_3pct": (rets >= 0.03).mean(),
        "days_le_-3pct": (rets <= -0.03).mean(),
    }
    if trades is not None:
        out["trades"] = len(trades)
        out["win_rate"] = (trades["pnl"] > 0).mean() if len(trades) else 0.0
    return out


def run(frames: dict[str, pd.DataFrame], risk: Risk = Risk(), costs: Costs = Costs(),
        capital: float = 1.0, start=None, end=None) -> Result:
    """`frames` are strategy-prepared frames per market (see strategies.py)."""
    markets = list(frames)
    idx = frames[markets[0]].index
    for m in markets[1:]:
        idx = idx.intersection(frames[m].index)
    if start is not None:
        idx = idx[idx >= pd.Timestamp(start)]
    if end is not None:
        idx = idx[idx < pd.Timestamp(end)]

    def col(name, dtype=float):
        return np.vstack([frames[m][name].reindex(idx).to_numpy(dtype=dtype) for m in markets])

    o, h, lo, c = col("open"), col("high"), col("low"), col("close")
    enter, exit_, es, sd, size = col("enter", bool), col("exit", bool), col("entry_stop"), col("stop_dist"), col("size")
    days = idx.floor("D").asi8  # day keys; works for any datetime resolution
    fee, slip = costs.fee, costs.slippage
    alloc = risk.alloc_per_market or 1.0 / len(markets)

    n, T = len(markets), len(idx)
    cash = capital
    qty = np.zeros(n)
    entry_px = np.zeros(n)
    stop = np.full(n, np.nan)
    entry_i = np.zeros(n, dtype=int)
    last_entry_day = np.full(n, -1)
    guard = DailyGuard.from_risk(risk)
    equity = np.empty(T)
    trades = []

    def sell(j, px, i, reason):
        nonlocal cash
        fill = px * (1 - slip)
        cash += qty[j] * fill * (1 - fee)
        trades.append((markets[j], idx[entry_i[j]], idx[i], entry_px[j], fill,
                       fill * (1 - fee) / (entry_px[j] * (1 + fee)) - 1, reason))
        qty[j] = 0.0
        stop[j] = np.nan

    for i in range(T):
        guard.on_day(days[i], equity[i - 1] if i else capital)
        for j in range(n):
            if qty[j] > 0:
                if exit_[j, i]:
                    sell(j, o[j, i], i, "exit")
                elif o[j, i] <= stop[j]:
                    sell(j, o[j, i], i, "stop")

        if guard.can_trade:
            eq_open = cash + float(qty @ o[:, i])
            for j in range(n):
                if qty[j] > 0:
                    continue
                if enter[j, i]:
                    px = o[j, i]
                elif es[j, i] <= h[j, i] and last_entry_day[j] != days[i]:  # NaN compares False
                    px = max(o[j, i], es[j, i])
                else:
                    continue
                fill = px * (1 + slip)
                value = order_value(eq_open, cash, alloc, size[j, i], sd[j, i], fill, risk.risk_per_trade, fee)
                if value <= capital * 1e-6:
                    continue
                qty[j] = value / fill
                cash -= value * (1 + fee)
                entry_px[j], entry_i[j], last_entry_day[j] = fill, i, days[i]
                stop[j] = fill - sd[j, i]  # NaN when the strategy has no stop

        for j in range(n):
            if qty[j] > 0 and lo[j, i] <= stop[j]:
                sell(j, min(o[j, i], stop[j]), i, "stop")

        eq = cash + float(qty @ c[:, i])
        reason = guard.check(eq)
        if reason:
            for j in range(n):
                if qty[j] > 0:
                    sell(j, c[j, i], i, reason)
            eq = cash
        equity[i] = eq

    trades_df = pd.DataFrame(trades, columns=["market", "entry_time", "exit_time", "entry", "exit", "pnl", "reason"])
    return Result(pd.Series(equity, index=idx), trades_df, capital)
