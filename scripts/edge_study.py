"""The pre-registered study in results/edge_study.md: costs, maximum profit, confirmed entries, a shifted clock.

    PYTHONPATH=. python scripts/edge_study.py
"""
import dataclasses
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "scripts")
import data_study as ds  # noqa: E402

from tradebot import backtest, data, optimize  # noqa: E402
from tradebot.data import regularize  # noqa: E402
from tradebot.strategies import atr, ema  # noqa: E402

CFG, NAMES = ds.CFG, ds.NAMES
ROUND_TRIP = 2 * (CFG.costs.fee + CFG.costs.slippage)


def wf(fr, name, transform=None, risk=CFG.risk):
    """walk_forward where transform(prepared, market) may change every prepared frame (selection and test alike)."""
    if transform is None:
        return optimize.walk_forward(fr, name, risk, CFG.costs, CFG.train_days, CFG.test_days)
    orig, market_of = optimize.prepare, {id(df): m for m, df in fr.items()}
    optimize.prepare = lambda n, df, p, **kw: transform(orig(n, df, p, **kw), market_of.get(id(df)))
    try:
        return optimize.walk_forward(fr, name, risk, CFG.costs, CFG.train_days, CFG.test_days)
    finally:
        optimize.prepare = orig


def mask(prep, bad):
    prep = prep.copy()
    bad = np.asarray(bad, dtype=bool)
    prep["enter"] &= ~bad
    prep.loc[bad, "entry_stop"] = np.nan
    return prep


def stats(rets):
    dev, lock = rets[rets.index < ds.DEV_END], rets[rets.index >= ds.DEV_END]
    h = len(dev) // 2
    m = backtest.metrics(dev)
    return {"cagr": m["cagr"], "total": m["total_return"], "sharpe": m["sharpe"], "mdd": m["max_drawdown"],
            "c1": backtest.metrics(dev.iloc[:h])["cagr"], "c2": backtest.metrics(dev.iloc[h:])["cagr"],
            "lock": (1 + lock).prod() - 1}


def verdict(s, base, non_inferiority=False):
    if non_inferiority:
        rules = {"CAGR ≥ base - 1pt": s["cagr"] >= base["cagr"] - 0.01, "MDD ≤ 3pt worse": s["mdd"] >= base["mdd"] - 0.03,
                 "halves within 1.5pt": abs(s["c1"] - base["c1"]) <= 0.015 and abs(s["c2"] - base["c2"]) <= 0.015}
    else:
        rules = {"higher CAGR": s["cagr"] > base["cagr"], "both halves": s["c1"] >= base["c1"] and s["c2"] >= base["c2"],
                 "MDD ≥ -30%": s["mdd"] >= -0.30}
    failed = [k for k, ok in rules.items() if not ok]
    if failed:
        return "reject (" + ", ".join(failed) + ")"
    return "PASS; lockbox " + ("ok" if s["lock"] >= base["lock"] - 0.02 else "FAILS")


def row(label, s, note=""):
    return (f"| {label} | {s['total']:+.1%} | {s['cagr']:+.1%} | {s['c1']:+.1%} / {s['c2']:+.1%} | {s['sharpe']:.2f} "
            f"| {s['mdd']:.1%} | {s['lock']:+.1%} | {note} |")


def run(fr, transform=None, risk=CFG.risk):
    return stats(backtest.portfolio({n: wf(fr, n, transform, risk)["oos_returns"] for n in NAMES}))


def confluence(fr):
    """E2: at least 3 of 4 independent confirmations at the previous bar's close."""
    btc = fr["KRW-BTC"]["close"]
    btc_up = (btc > ema(btc, 200)).shift(1)
    out = {}
    for m, df in fr.items():
        c, d = df["close"], 1440 // CFG.timeframe
        votes = pd.DataFrame({
            "trend": c > ema(c, 200),
            "volume": df["volume"].rolling(d).sum() >= df["volume"].rolling(30 * d).sum() / 30,
            "not_chasing": c <= ema(c, 50) + 3 * atr(df),
        }).shift(1)
        votes["btc"] = btc_up.reindex(df.index)
        out[m] = votes.fillna(False).astype(int).sum(axis=1) >= 3
    return out


def shifted_frames(offset_h: int):
    """4h bars rebuilt from 1h candles, closing `offset_h` hours later than Upbit's own 4h candles."""
    out = {}
    for m in ds.BASE:
        h = data.load(m, 60, CFG.history_days, offline=True)
        b = h.resample("4h", offset=f"{offset_h}h", label="left", closed="left").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna(subset=["close"])
        out[m] = regularize(b, CFG.timeframe)
    idx = out["KRW-BTC"].index
    return {m: ds.pad(df, idx) for m, df in out.items()}


def main():
    fr = ds.frames(ds.BASE)
    head = ["| variant | dev total | dev CAGR | CAGR halves | Sharpe | dev MDD | lockbox | verdict |",
            "|---|---|---|---|---|---|---|---|"]
    base = run(fr)
    lines = head + [row("baseline", base)]
    print(lines[-1], flush=True)
    results = {}

    def report(key, label, s, non_inf=False):
        results[key] = s
        lines.append(row(label, s, verdict(s, base, non_inf)))
        print(lines[-1], flush=True)

    e1 = lambda p, m: mask(p, (p["stop_dist"] / p["open"] < ROUND_TRIP / 0.10).to_numpy())  # noqa: E731
    report("E1", "E1 skip trades fees would eat", run(fr, e1))
    conf = confluence(fr)
    e2 = lambda p, m: mask(p, ~conf[m].to_numpy())  # noqa: E731
    report("E2", "E2 confirmed entries (3 of 4)", run(fr, e2))
    orig_score = optimize.score
    optimize.score = lambda res, min_trades: (backtest.metrics(res.daily_returns())["cagr"]
                                              if len(res.trades) >= min_trades else -np.inf)
    try:
        report("E3", "E3 choose parameters by CAGR", run(fr))
    finally:
        optimize.score = orig_score
    report("E4", "E4 risk 2% per trade", run(fr, risk=dataclasses.replace(CFG.risk, risk_per_trade=0.02)))
    ref = run(shifted_frames(0))
    lines.append(row("(reference: 4h rebuilt from 1h, same clock)", ref))
    print(lines[-1], flush=True)
    report("E5", "E5 clock shifted by 2 hours", run(shifted_frames(2)), non_inf=True)

    print("\n".join(lines))


if __name__ == "__main__":
    main()
