"""results/surge_study.md: the 5 core coins plus coins whose turnover suddenly jumped, traded by the same rules.

    PYTHONPATH=. python scripts/collect_data.py      # once: the wide data set
    PYTHONPATH=. python scripts/surge_study.py
"""
import dataclasses
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, "scripts")
import data_study as ds  # noqa: E402
import edge_study as es  # noqa: E402

from tradebot import backtest, optimize  # noqa: E402


def surge(mult: float, window: int) -> pd.DataFrame:
    """Daily eligibility: flagged (turnover >= mult x its 30-day average and top 20 that day) on any of the
    previous `window` completed days."""
    v = pd.DataFrame({p.stem: pd.read_csv(p, index_col=0, parse_dates=True)["value"]
                      for p in Path("data/daily").glob("KRW-*.csv") if p.stem.split("-")[1] not in ds.STABLE})
    v.index = pd.to_datetime(v.index, utc=True)
    v = v.sort_index()
    flag = (v >= mult * v.rolling(30, min_periods=20).mean().shift(1)) & (v.rank(axis=1, ascending=False) <= 20)
    flag[[m for m in ds.BASE if m in flag]] = False
    return flag.astype(float).rolling(window, min_periods=1).max().shift(1).fillna(0).astype(bool)


def variant(args):
    mult, window, slip = args
    if mult is None:
        fr, gate, costs = ds.frames(ds.BASE), None, ds.CFG.costs
    else:
        elig = surge(mult, window)
        have = {p.name.split("_")[0] for p in Path("data").glob("KRW-*_240m.csv")}
        fr = ds.frames(ds.BASE + [m for m in elig if elig[m].any() and m in have])
        idx = fr["KRW-BTC"].index
        gate = {m: ds.by_bar(elig[m], idx).fillna(False).astype(bool) for m in fr if m not in ds.BASE}
        costs = dataclasses.replace(ds.CFG.costs, per_market={m: slip for m in gate})
    risk = dataclasses.replace(ds.CFG.risk, alloc_per_market=1 / len(ds.BASE))
    btc = fr["KRW-BTC"]["close"]
    orig, by_id = optimize.prepare, {id(df): gate[m] for m, df in fr.items() if gate and m in gate}

    def prep(n, df, p, **kw):  # no entries outside the window; weights only across coins eligible now
        out = orig(n, df, p, **kw)
        g = by_id.get(id(df))
        if g is not None:
            bad = ~g.to_numpy()
            out = es.mask(out, bad)
            out.loc[bad, "vol"] = np.nan
        return out

    optimize.prepare = prep
    try:
        rets, trades = {}, []
        for name in ds.NAMES:
            wf = optimize.walk_forward(fr, name, risk, costs, ds.CFG.train_days, ds.CFG.test_days, btc=btc)
            rets[name] = wf["oos_returns"]
            for w, (_, b, c) in zip(wf["windows"], optimize.windows(fr["KRW-BTC"].index, ds.CFG.train_days, ds.CFG.test_days)):
                if w["params"] is not None:  # the test windows' trades, to see what the surge coins added
                    res = backtest.run({m: prep(name, df, w["params"], btc=btc) for m, df in fr.items()}, risk, costs, start=b, end=c)
                    trades.append(res.trades.assign(strategy=name))
    finally:
        optimize.prepare = orig
    t = pd.concat(trades)
    extra = t[~t["market"].isin(ds.BASE) & (t["exit_time"] < ds.DEV_END)]
    return args, backtest.portfolio(rets), {"trades": len(extra), "coins": extra["market"].nunique(),
                                           "win": float((extra["pnl"] > 0).mean()) if len(extra) else 0.0,
                                           "mean": float(extra["pnl"].mean()) if len(extra) else 0.0}


if __name__ == "__main__":
    jobs = [(None, None, None), (5, 7, 0.003), (5, 7, 0.006), (5, 3, 0.003), (3, 7, 0.003)]
    labels = ["**baseline: 5 coins (production)**", "**S: 5x turnover, top 20, 7 days, 0.3% slippage**",
              "cost check: S with 0.6% slippage", "robustness: 3-day window", "robustness: 3x turnover"]
    with ProcessPoolExecutor(4) as ex:
        out = {a: (r, x) for a, r, x in ex.map(variant, jobs)}
    base = es.stats(out[jobs[0]][0])
    lines = ["| variant | dev total | dev CAGR | CAGR halves | dev MDD | Sharpe | lockbox | surge-coin trades (dev) | verdict |",
             "|---|---|---|---|---|---|---|---|---|"]
    for a, label in zip(jobs, labels):
        r, x = out[a]
        s = es.stats(r)
        v = "" if a == jobs[0] else es.verdict(s, base)
        lines.append(f"| {label} | {s['total']:+.1%} | {s['cagr']:+.1%} | {s['c1']:+.1%} / {s['c2']:+.1%} | {s['mdd']:.1%} "
                     f"| {s['sharpe']:.2f} | {s['lock']:+.1%} | {x['trades']} in {x['coins']} coins, {x['win']:.0%} won, "
                     f"mean {x['mean']:+.1%} | {v} |")
    print("\n".join(lines))
    pd.concat({str(k): v[0] for k, v in out.items()}, axis=1).to_csv("data/ext/surge_returns.csv")
