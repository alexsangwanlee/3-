"""Walk-forward optimisation: tune on the past window, trade the next one, repeat.

Only the stitched out-of-sample (OOS) windows are reported, so the numbers
are what the bot would have earned with parameters it could actually know.
"""
import numpy as np
import pandas as pd

from . import backtest
from .risk import Costs, Risk
from .strategies import combos, prepare


def windows(index: pd.DatetimeIndex, train_days: int, test_days: int):
    start, last = index[0].floor("D"), index[-1]
    t = start + pd.Timedelta(days=train_days)
    while t < last:
        yield t - pd.Timedelta(days=train_days), t, min(t + pd.Timedelta(days=test_days), last + pd.Timedelta(hours=1))
        t += pd.Timedelta(days=test_days)


def score(res: backtest.Result, min_trades: int) -> float:
    if len(res.trades) < min_trades:
        return -np.inf
    return backtest.metrics(res.daily_returns())["sharpe"]


def walk_forward(frames: dict[str, pd.DataFrame], name: str, risk: Risk = Risk(), costs: Costs = Costs(),
                 train_days: int = 180, test_days: int = 60, min_trades: int = 10) -> dict:
    grid = combos(name) or [{}]
    index = next(iter(frames.values())).index
    wins = list(windows(index, train_days, test_days))
    scores = np.full((len(grid), len(wins)), -np.inf)
    for gi, params in enumerate(grid):
        prepared = {m: prepare(name, df, params) for m, df in frames.items()}
        for wi, (a, b, _) in enumerate(wins):
            scores[gi, wi] = score(backtest.run(prepared, risk, costs, start=a, end=b), min_trades)

    parts, chosen, trades = [], [], []
    for wi, (a, b, c) in enumerate(wins):
        gi = int(np.argmax(scores[:, wi]))
        best = scores[gi, wi]
        if best <= 0:  # nothing worked in training -> sit in cash for this window
            days = pd.date_range(b, c - pd.Timedelta(hours=1), freq="D")
            parts.append(pd.Series(0.0, index=days))
            chosen.append({"test_start": str(b.date()), "params": None, "train_sharpe": best})
            continue
        prepared = {m: prepare(name, df, grid[gi]) for m, df in frames.items()}
        res = backtest.run(prepared, risk, costs, start=b, end=c)
        parts.append(res.daily_returns())
        trades.append(res.trades)
        chosen.append({"test_start": str(b.date()), "params": grid[gi], "train_sharpe": round(float(best), 2)})

    rets = pd.concat(parts)
    all_trades = pd.concat(trades) if trades else pd.DataFrame(columns=["pnl"])
    return {"strategy": name, "oos_returns": rets, "metrics": backtest.metrics(rets, all_trades), "windows": chosen}


def best_params(frames: dict[str, pd.DataFrame], name: str, risk: Risk, costs: Costs,
                train_days: int = 180, min_trades: int = 10) -> tuple[dict, float]:
    """Parameters to trade from now on: best Sharpe on the most recent `train_days`."""
    index = next(iter(frames.values())).index
    start = index[-1] - pd.Timedelta(days=train_days)
    best, best_score = {}, -np.inf
    for params in combos(name) or [{}]:
        prepared = {m: prepare(name, df, params) for m, df in frames.items()}
        s = score(backtest.run(prepared, risk, costs, start=start), min_trades)
        if s > best_score:
            best, best_score = params, s
    return best, best_score
