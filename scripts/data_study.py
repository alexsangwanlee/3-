"""The pre-registered comparison in results/data_study.md.

    PYTHONPATH=. python scripts/collect_data.py              # once: the wide data set
    PYTHONPATH=. python scripts/data_study.py candidates     # bars where an entry could fire (for Kronos)
    python scripts/kronos_forecasts.py --kronos <clone dir>  # V4 forecasts (torch, slow)
    PYTHONPATH=. python scripts/data_study.py                # all variants -> table

Research only: needs lightgbm (and torch for Kronos), which the bot itself does not.
"""
import dataclasses
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from tradebot import backtest, config, data, optimize
from tradebot.optimize import walk_forward, windows
from tradebot.strategies import atr, ema, prepare

CFG = config.Config()  # production defaults = the baseline
NAMES = CFG.strategies
BASE = CFG.markets
D = 1440 // CFG.timeframe
DEV_END = pd.Timestamp("2026-04-01", tz="UTC")
EXT = Path("data/ext")
STABLE = {"USDT", "USDC", "USDS", "USDE", "DAI", "TUSD", "PYUSD", "FDUSD", "USD1"}
HORIZON = 5 * D  # V3 label: next 5 days
Q = 0.30


# -- data -------------------------------------------------------------------

def pad(df: pd.DataFrame, idx: pd.DatetimeIndex) -> pd.DataFrame:
    """Same index for every coin: bars before listing are flat at the first open with no volume."""
    out = df.reindex(idx)
    first = df["open"].iloc[0]
    for c in ("open", "high", "low", "close"):
        out[c] = out[c].fillna(first) if c != "close" else out[c].ffill().fillna(first)
    out["volume"] = out["volume"].fillna(0.0)
    return out


def frames(markets) -> dict[str, pd.DataFrame]:
    raw = {m: data.load(m, CFG.timeframe, CFG.history_days, offline=True) for m in markets}
    idx = raw["KRW-BTC"].index
    return {m: pad(df, idx) for m, df in raw.items()}


def daily_external() -> pd.DataFrame:
    """Previous day's value of every market-wide series, indexed by UTC day."""
    def read(name):
        s = pd.read_csv(EXT / f"{name}.csv", index_col=0, parse_dates=True).iloc[:, 0]
        s.index = pd.to_datetime(s.index, utc=True, format="ISO8601")
        return s

    fund = read("funding_BTC").resample("D").mean()
    ext = pd.DataFrame({"fng": read("fng").resample("D").last(), "kimchi": read("kimchi").resample("D").last(),
                        "btc_funding7": fund.rolling(7, min_periods=3).mean(),
                        "oi30": read("oi_BTC").resample("D").last().pct_change(30, fill_method=None)})
    return ext.ffill().shift(1)


def turnover_rank() -> pd.DataFrame:
    """Rank of trailing 30-day KRW turnover through the previous day, among all non-stable KRW markets."""
    vals = {}
    for p in Path("data/daily").glob("KRW-*.csv"):
        if p.stem.split("-")[1] in STABLE:
            continue
        df = pd.read_csv(p, index_col=0, parse_dates=True)
        df.index = pd.to_datetime(df.index, utc=True)
        vals[p.stem] = df["value"]
    value = pd.DataFrame(vals).sort_index().rolling(30, min_periods=20).sum().shift(1)
    return value.rank(axis=1, ascending=False)


def by_bar(daily: pd.Series | pd.DataFrame, idx: pd.DatetimeIndex):
    return daily.reindex(idx.floor("D")).set_axis(idx)


# -- evaluation ---------------------------------------------------------------

def gated_wf(fr, name, gate=None, risk=CFG.risk):
    """walk_forward with entries allowed only where gate[market] is True (selection and test alike)."""
    if gate is None:
        return walk_forward(fr, name, risk, CFG.costs, CFG.train_days, CFG.test_days)
    orig, by_id = optimize.prepare, {id(df): gate[m] for m, df in fr.items()}

    def prep(n, df, p):
        out = orig(n, df, p)
        g = by_id.get(id(df))
        if g is not None:
            out["enter"] &= g.to_numpy()
            out.loc[~g.to_numpy(), "entry_stop"] = np.nan
        return out

    optimize.prepare = prep
    try:
        return walk_forward(fr, name, risk, CFG.costs, CFG.train_days, CFG.test_days)
    finally:
        optimize.prepare = orig


def stats(rets: pd.Series) -> dict:
    dev, lock = rets[rets.index < DEV_END], rets[rets.index >= DEV_END]
    m, h = backtest.metrics(dev), len(dev) // 2
    return {"total": m["total_return"], "sharpe": m["sharpe"], "s1": backtest.metrics(dev.iloc[:h])["sharpe"],
            "s2": backtest.metrics(dev.iloc[h:])["sharpe"], "mdd": m["max_drawdown"],
            "lock": (1 + lock).prod() - 1, "lock_sharpe": backtest.metrics(lock)["sharpe"]}


def verdict(s: dict, base: dict, auc: float | None = None) -> str:
    rules = [s["sharpe"] >= base["sharpe"] + 0.10, s["s1"] >= base["s1"] and s["s2"] >= base["s2"],
             s["mdd"] >= base["mdd"] - 0.03] + ([auc is not None and auc > 0.55] if auc is not None else [])
    if not all(rules):
        return "reject (" + ", ".join(n for n, ok in zip(["Sharpe +0.10", "both halves", "MDD", "AUC"], rules) if not ok) + ")"
    return "PASS dev; lockbox " + ("ok" if s["lock"] >= base["lock"] - 0.02 else "FAILS")


def row(label, s, note=""):
    return (f"| {label} | {s['total']:+.1%} | {s['sharpe']:.2f} | {s['s1']:.2f} / {s['s2']:.2f} | {s['mdd']:.1%} "
            f"| {s['lock']:+.1%} | {note} |")


def auc(score, y) -> float:
    score, y = np.asarray(score, float), np.asarray(y, float)
    ok = ~np.isnan(score)
    score, y = score[ok], y[ok]
    r, n1 = pd.Series(score).rank().to_numpy(), y.sum()
    return float((r[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * (len(y) - n1))) if 0 < n1 < len(y) else float("nan")


def masked_oos(fr, wf_by_name, mask_for):
    """Re-run every test window with each window's chosen parameters and entries masked by mask_for(wi, name, prep)."""
    wins = list(windows(next(iter(fr.values())).index, CFG.train_days, CFG.test_days))
    parts, trades = {n: [] for n in NAMES}, {n: [] for n in NAMES}
    for wi, (a, b, c) in enumerate(wins):
        for n in NAMES:
            p = wf_by_name[n]["windows"][wi]["params"]
            if p is None:
                parts[n].append(pd.Series(0.0, index=pd.date_range(b, c - pd.Timedelta(hours=1), freq="D")))
                continue
            prep = {m: prepare(n, df, p) for m, df in fr.items()}
            mask = mask_for(wi, b, c, n, prep)
            for m, bad in (mask or {}).items():
                prep[m]["enter"] &= ~bad
                prep[m].loc[bad, "entry_stop"] = np.nan
            res = backtest.run(prep, CFG.risk, CFG.costs, start=b, end=c)
            parts[n].append(res.daily_returns())
            trades[n].append(res.trades)
    return backtest.portfolio({n: pd.concat(parts[n]) for n in NAMES}), pd.concat(sum(trades.values(), []))


# -- V3: LightGBM on many features --------------------------------------------

def features(fr: dict[str, pd.DataFrame], ext: pd.DataFrame, rank: pd.DataFrame) -> dict[str, pd.DataFrame]:
    idx = fr["KRW-BTC"].index
    btc = fr["KRW-BTC"]["close"]
    e = by_bar(ext, idx)
    r = {m: df["close"].pct_change(fill_method=None) for m, df in fr.items()}
    r7 = pd.DataFrame({m: df["close"].pct_change(7 * D, fill_method=None) for m, df in fr.items()})
    r30 = pd.DataFrame({m: df["close"].pct_change(30 * D, fill_method=None) for m, df in fr.items()})
    out = {}
    for m, df in fr.items():
        c, coin = df["close"], m.split("-")[1]
        fpath = EXT / f"funding_{coin}.csv"
        fund = (pd.read_csv(fpath, index_col=0, parse_dates=True).iloc[:, 0] if fpath.exists() else pd.Series(dtype=float))
        fund.index = pd.to_datetime(fund.index, utc=True, format="ISO8601")
        fund7 = fund.resample("D").mean().rolling(7, min_periods=3).mean().shift(1) if len(fund) else None
        f = {f"r{k}d": c.pct_change(k * D, fill_method=None) for k in (1, 3, 7, 14, 30, 60)}
        f.update({"vol7": r[m].rolling(7 * D).std(), "vol30": r[m].rolling(30 * D).std(), "atr": atr(df) / c,
                  "ema50": c / ema(c, 50) - 1, "ema200": c / ema(c, 200) - 1,
                  "range30": (c - c.rolling(30 * D).min()) / (c.rolling(30 * D).max() - c.rolling(30 * D).min()),
                  "volu": df["volume"].rolling(D).sum() / df["volume"].rolling(30 * D).mean() / D,
                  "xs_r7": r7.rank(axis=1, pct=True)[m], "xs_r30": r30.rank(axis=1, pct=True)[m],
                  "btc_r7": btc.pct_change(7 * D), "btc_ema200": btc / ema(btc, 200) - 1,
                  "corr_btc": r[m].rolling(30 * D).corr(r["KRW-BTC"])})
        f = pd.DataFrame(f).replace([np.inf, -np.inf], np.nan).shift(1)  # through the previous bar's close
        f["turnover_rank"] = by_bar(rank[m], idx) if m in rank else np.nan  # already through the previous day
        for col in ext:
            f[col] = e[col]
        f["funding7"] = by_bar(fund7, idx) if fund7 is not None else np.nan
        out[m] = f
    return out


def lgbm_scores(fr, feats, rank, wins):
    """Per test window: model trained on every eligible bar whose 5-day label was known before the window."""
    import lightgbm as lgb

    idx = fr["KRW-BTC"].index
    X = pd.concat({m: f for m, f in feats.items()}, names=["market", "time"])
    fwd = pd.concat({m: df["close"].shift(-HORIZON) / df["open"] - 1 for m, df in fr.items()}, names=["market", "time"])
    elig = pd.concat({m: by_bar(rank[m], idx) <= 30 if m in rank else pd.Series(False, idx) for m in fr},
                     names=["market", "time"]).fillna(False)
    t = X.index.get_level_values("time")
    params = {"objective": "binary", "learning_rate": 0.05, "num_leaves": 15, "min_data_in_leaf": 200,
              "feature_fraction": 0.8, "bagging_fraction": 0.8, "bagging_freq": 1, "verbose": -1, "seed": 0}
    scores, thresholds = {}, {}
    for wi, (a, b, c) in enumerate(wins):
        train = elig.to_numpy() & (t < b - pd.Timedelta(hours=4 * HORIZON)) & fwd.notna().to_numpy()
        if train.sum() < 20_000:
            continue
        model = lgb.train(params, lgb.Dataset(X[train], (fwd[train] > 0).astype(int)), num_boost_round=300)
        thresholds[wi] = float(np.quantile(model.predict(X[train]), Q))
        test = (t >= b) & (t < c)
        scores[wi] = pd.Series(model.predict(X[test]), index=X.index[test])
        print(f"  lgbm window {wi}: {train.sum():,} training bars", flush=True)
    return scores, thresholds, fwd


# -- runs -------------------------------------------------------------------

def candidates(fr, wf_by_name):
    """Bars in test windows where either strategy could enter: Kronos forecasts these."""
    wins = list(windows(fr["KRW-BTC"].index, CFG.train_days, CFG.test_days))
    rows = set()
    for wi, (a, b, c) in enumerate(wins):
        for n in NAMES:
            p = wf_by_name[n]["windows"][wi]["params"]
            if p is None:
                continue
            for m, df in fr.items():
                prep = prepare(n, df, p)
                w = prep[(prep.index >= b) & (prep.index < c)]
                hit = w["enter"] | (w["high"] >= w["entry_stop"])
                rows |= {(m, str(t)) for t in w.index[hit.to_numpy()]}
    out = pd.DataFrame(sorted(rows), columns=["market", "time"])
    out.to_csv(EXT / "kronos_candidates.csv", index=False)
    print(f"{len(out)} candidate bars -> {EXT / 'kronos_candidates.csv'}")


def main(which):
    fr = frames(BASE)
    print("baseline walk-forward", flush=True)
    base_wf = {n: gated_wf(fr, n) for n in NAMES}
    if which == "candidates":
        return candidates(fr, base_wf)
    base_rets = backtest.portfolio({n: base_wf[n]["oos_returns"] for n in NAMES})
    base = stats(base_rets)
    lines = ["| variant | dev total | dev Sharpe | halves | dev MDD | lockbox | verdict |", "|---|---|---|---|---|---|---|",
             row("baseline: 5 coins", base, "")]
    ext, rank = daily_external(), turnover_rank()
    idx = fr["KRW-BTC"].index
    wins = list(windows(idx, CFG.train_days, CFG.test_days))

    # V2: market-wide gates on the baseline coins
    for label, ok in (("V2a Fear & Greed < 80", ext["fng"] < 80), ("V2b BTC funding ≤ 0.03%", ext["btc_funding7"] <= 0.0003),
                      ("V2c kimchi premium ≤ 5%", ext["kimchi"] <= 0.05)):
        g = by_bar(ok.astype("boolean"), idx).fillna(True).astype(bool)  # no data -> no gate
        s = stats(backtest.portfolio({n: gated_wf(fr, n, {m: g for m in fr})["oos_returns"] for n in NAMES}))
        lines.append(row(label, s, verdict(s, base)))
        print(lines[-1], flush=True)

    # V1: wider universe ranked by turnover
    uni = [m for m in json.loads((EXT / "universe.json").read_text()) if m.split("-")[1] not in STABLE]
    wide = frames(uni)
    for n_top in (10, 20):
        gate = {m: (by_bar(rank[m], idx) <= n_top).fillna(False).to_numpy() for m in wide}
        gate = {m: pd.Series(g, idx) for m, g in gate.items()}
        risk = dataclasses.replace(CFG.risk, alloc_per_market=1 / n_top)
        s = stats(backtest.portfolio({n: gated_wf(wide, n, gate, risk)["oos_returns"] for n in NAMES}))
        lines.append(row(f"V1{'ab'[n_top == 20]} top {n_top} of {len(uni)} coins", s, verdict(s, base)))
        print(lines[-1], flush=True)

    # V3: LightGBM, trained on every bar of the wide universe, filters the baseline's entries
    feats = features(wide, ext, rank)
    scores, thr, fwd = lgbm_scores(wide, feats, rank, wins)

    def lgbm_mask(wi, b, c, n, prep):
        if wi not in scores:
            return None
        sc = scores[wi]
        return {m: (sc.xs(m, level="market").reindex(prep[m].index) < thr[wi]).fillna(False).to_numpy() for m in prep}

    rets3, _ = masked_oos(fr, base_wf, lgbm_mask)
    all_sc = pd.concat(scores.values())
    trade_rows = [(m, t, pnl) for n in NAMES for m, t, pnl in _trades(fr, base_wf, n)]
    tr_auc = auc([all_sc.get((m, t), np.nan) for m, t, _ in trade_rows], [pnl > 0 for *_, pnl in trade_rows])
    bar_y = fwd.reindex(all_sc.index) > 0
    s = stats(rets3)
    lines.append(row("V3 LightGBM filter", s, verdict(s, base, tr_auc) + f"; AUC trades {tr_auc:.3f}, bars "
                     f"{auc(all_sc.to_numpy(), bar_y.to_numpy()):.3f}"))
    print(lines[-1], flush=True)

    # V4: Kronos forecasts (from scripts/kronos_forecasts.py)
    kp = EXT / "kronos_preds.csv"
    if kp.exists():
        k = pd.read_csv(kp, parse_dates=["time"]).set_index(["market", "time"])["pred"]
        k.index = k.index.set_levels(pd.to_datetime(k.index.levels[1], utc=True), level=1)

        def kronos_mask(wi, b, c, n, prep):
            return {m: (k.xs(m, level="market").reindex(prep[m].index) < 0).fillna(False).to_numpy() for m in prep}

        rets4, _ = masked_oos(fr, base_wf, kronos_mask)
        real = pd.Series([fr[m]["close"].shift(-(D - 1)).get(t, np.nan) / fr[m]["close"].shift(1).get(t, np.nan) - 1
                          for m, t in k.index], index=k.index)
        tr_auc = auc([k.get((m, t), np.nan) for m, t, _ in trade_rows], [pnl > 0 for *_, pnl in trade_rows])
        s = stats(rets4)
        lines.append(row(f"V4 Kronos-small filter ({len(k)} forecasts)", s, verdict(s, base, tr_auc)
                         + f"; AUC trades {tr_auc:.3f}, 24h {auc(k.to_numpy(), (real > 0).to_numpy()):.3f}"))
        print(lines[-1], flush=True)
    Path("results/data_study_table.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


def _trades(fr, wf_by_name, n):
    """(market, entry bar, pnl) of the baseline's out-of-sample trades."""
    wins = list(windows(fr["KRW-BTC"].index, CFG.train_days, CFG.test_days))
    for wi, (a, b, c) in enumerate(wins):
        p = wf_by_name[n]["windows"][wi]["params"]
        if p is None:
            continue
        res = backtest.run({m: prepare(n, df, p) for m, df in fr.items()}, CFG.risk, CFG.costs, start=b, end=c)
        yield from zip(res.trades["market"], pd.to_datetime(res.trades["entry_time"], utc=True), res.trades["pnl"])


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "all")
