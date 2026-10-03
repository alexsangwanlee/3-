"""V1 of results/data_study.md: trade the top 10 / 20 coins by 30-day KRW turnover instead of 5 fixed ones.

    PYTHONPATH=. python scripts/collect_data.py      # once: the wide data set
    PYTHONPATH=. python scripts/universe_study.py
"""
import dataclasses
import json
import sys
from concurrent.futures import ProcessPoolExecutor

import pandas as pd

sys.path.insert(0, "scripts")
import data_study as ds  # noqa: E402
import edge_study as es  # noqa: E402

from tradebot import backtest, optimize  # noqa: E402


def wf(fr, name, gate, risk, btc):
    """walk_forward with entries only where gate[market] is True; btc=None reproduces the bot before confirmation."""
    orig, by_id = optimize.prepare, {id(df): gate[m] for m, df in fr.items()} if gate else {}

    def prep(n, df, p, **kw):
        out = orig(n, df, p, **kw)
        g = by_id.get(id(df))
        if g is not None:
            out = es.mask(out, ~g.to_numpy())
        return out

    optimize.prepare = prep
    try:
        return optimize.walk_forward(fr, name, risk, ds.CFG.costs, ds.CFG.train_days, ds.CFG.test_days, btc=btc)
    finally:
        optimize.prepare = orig


def variant(args):
    top, confirm = args
    rank = ds.turnover_rank()
    if top is None:
        fr = ds.frames(ds.BASE)
        gate, risk = None, ds.CFG.risk
    else:
        uni = [m for m in json.loads((ds.EXT / "universe.json").read_text()) if m.split("-")[1] not in ds.STABLE
               and m in rank and (rank[m] <= top).any()]
        fr = ds.frames(uni)
        idx = fr["KRW-BTC"].index
        gate = {m: ds.by_bar(rank[m] <= top, idx).fillna(False).astype(bool) for m in fr}
        risk = dataclasses.replace(ds.CFG.risk, alloc_per_market=1 / top)
    btc = fr["KRW-BTC"]["close"] if confirm else None
    rets = backtest.portfolio({n: wf(fr, n, gate, risk, btc)["oos_returns"] for n in ds.NAMES})
    return args, rets


if __name__ == "__main__":
    jobs = [(None, False), (None, True), (10, False), (20, False), (10, True), (20, True)]
    with ProcessPoolExecutor(4) as ex:
        out = dict(ex.map(variant, jobs))
    lines = ["| variant | dev total | Sharpe | halves (Sharpe) | dev CAGR | CAGR halves | dev MDD | lockbox | verdict |",
             "|---|---|---|---|---|---|---|---|---|"]
    for confirm in (False, True):
        base_r = out[(None, confirm)]
        b_sh, b_cg = ds.stats(base_r), es.stats(base_r)
        tag = "today's rules (confirmation)" if confirm else "as registered (no confirmation)"
        lines.append(f"| **baseline 5 coins, {tag}** | {b_sh['total']:+.1%} | {b_sh['sharpe']:.2f} | {b_sh['s1']:.2f} / {b_sh['s2']:.2f} "
                     f"| {b_cg['cagr']:+.1%} | {b_cg['c1']:+.1%} / {b_cg['c2']:+.1%} | {b_sh['mdd']:.1%} | {b_sh['lock']:+.1%} | |")
        for top in (10, 20):
            r = out[(top, confirm)]
            sh, cg = ds.stats(r), es.stats(r)
            verdict = es.verdict(cg, b_cg) if confirm else ds.verdict(sh, b_sh)
            lines.append(f"| top {top} by turnover, {tag} | {sh['total']:+.1%} | {sh['sharpe']:.2f} | {sh['s1']:.2f} / {sh['s2']:.2f} "
                         f"| {cg['cagr']:+.1%} | {cg['c1']:+.1%} / {cg['c2']:+.1%} | {sh['mdd']:.1%} | {sh['lock']:+.1%} | {verdict} |")
    print("\n".join(lines))
    pd.concat({str(k): v for k, v in out.items()}, axis=1).to_csv("data/ext/universe_returns.csv")
