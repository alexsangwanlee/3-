"""What can a bot learn from 워뇨띠's published BitMEX record (account aoa, 2018-03..2021-12)?

Source: his DCInside 차트 갤러리 post of 2026-09-22 (Google Drive archive, sha256 below).
His terms: analysis and your own programs are fine; do not sell bots or products built on it.
The raw files go to data/wonyotti/ (gitignored) and are never committed.

    pip install scikit-learn && PYTHONPATH=. python scripts/wonyotti_study.py

Steps: fills -> hourly position -> (1) style profile, (2) "mirror" his hourly positions,
(3) clone his long decisions from market features (train 2018-2020, validate 2021),
(4) trade the clone on Upbit 2022-04..2026-03 with the tradebot engine.
"""
import glob
import hashlib
import subprocess
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from tradebot import backtest, data
from tradebot.risk import Costs, Risk
from tradebot.strategies import _finish

WORK = Path("data/wonyotti")
DRIVE_ID = "1XDwxbriz_kOq44iH-mHcjsYTBklMnMW3"
SHA256 = "b6f1dc7aadf8209bf6c99fd516a06c0cabdc77c5f16f9fc8df92fdf1d8d01b9a"


def download():
    z = WORK / "aoa.zip"
    if not z.exists():
        WORK.mkdir(parents=True, exist_ok=True)
        url = f"https://drive.usercontent.google.com/download?id={DRIVE_ID}&export=download&confirm=t"
        z.write_bytes(requests.get(url, headers={"user-agent": "Mozilla/5.0"}, timeout=600).content)
    assert hashlib.sha256(z.read_bytes()).hexdigest() == SHA256, "archive differs from the published one"
    if not glob.glob(str(WORK / "aoa-execution-*.csv")):
        subprocess.run(["unzip", "-o", "-q", "-O", "CP949", str(z), "-d", str(WORK)], check=True)


def binance_1h(symbol, start="2017-10-01", end="2022-01-01"):
    path = WORK / f"{symbol}_1h.pkl"
    if path.exists():
        return pd.read_pickle(path)
    rows, t, stop = [], int(pd.Timestamp(start, tz="UTC").timestamp() * 1e3), pd.Timestamp(end, tz="UTC")
    while t < stop.timestamp() * 1e3:
        r = requests.get("https://data-api.binance.vision/api/v3/klines", timeout=20,
                         params={"symbol": symbol, "interval": "1h", "startTime": t, "limit": 1000}).json()
        if not r:
            break
        rows, t = rows + r, r[-1][0] + 3_600_000
        time.sleep(0.05)
    df = pd.DataFrame(rows).iloc[:, :6].astype(float)
    df.columns = ["t", "open", "high", "low", "close", "volume"]
    df.index = pd.to_datetime(df.pop("t"), unit="ms", utc=True)
    df = df[df.index < stop]
    df.to_pickle(path)
    return df


def bet(hours):
    """His BTC bet each hour: XBTUSD contracts / account value in USD (he counted profit in BTC)."""
    cols = ["symbol", "side", "lastqty", "lastpx", "exectype", "lastliquidityind", "timestamp"]
    f = pd.concat(pd.read_csv(p, usecols=cols, encoding="utf-8-sig") for p in sorted(glob.glob(str(WORK / "aoa-execution-*.csv"))))
    f = f[(f["exectype"] == "Trade") & (f["symbol"] == "XBTUSD")]
    f.index = pd.to_datetime(f["timestamp"], utc=True, format="mixed")
    contracts = f["lastqty"].where(f["side"] == "Buy", -f["lastqty"]).resample("h").sum().cumsum().reindex(hours).ffill().fillna(0)
    px = f["lastpx"].resample("h").last().reindex(hours).ffill()
    w = pd.read_csv(WORK / "aoa-wallet-2018-03-01-2021-12-31.csv", encoding="utf-8-sig", usecols=["date", "walletbalance"])
    wallet = w.groupby("date")["walletbalance"].last() / 1e8
    wallet.index = pd.to_datetime(wallet.index, utc=True)
    wallet = pd.Series(wallet.reindex(hours.floor("D")).ffill().shift(24).to_numpy(), index=hours)  # yesterday's balance
    usd = wallet * px
    return (contracts / usd).where(usd > 1000)


def features(df):
    """Scale-free features known at the close of each 1h bar."""
    c, v = df["close"], df["volume"]
    f = pd.DataFrame(index=df.index)
    for n in (1, 4, 24, 72, 168):
        f[f"r{n}"] = np.log(c / c.shift(n))
    for n in (24, 168):
        f[f"hi{n}"] = c / df["high"].rolling(n).max() - 1
        f[f"lo{n}"] = c / df["low"].rolling(n).min() - 1
        f[f"ema{n}"] = c / c.ewm(span=n, adjust=False).mean() - 1
    r = np.log(c / c.shift(1))
    f["vol24"] = r.rolling(24).std()
    f["volratio"] = f["vol24"] / r.rolling(168).std()
    f["volz"] = np.log((v + 1) / (v.rolling(168).mean() + 1))
    d = c.diff()
    f["rsi14"] = 100 - 100 / (1 + d.clip(lower=0).ewm(alpha=1 / 14).mean() / (-d.clip(upper=0)).ewm(alpha=1 / 14).mean())
    return f


def main():
    download()
    hours = pd.date_range("2018-03-05", "2021-12-31 23:00", freq="h", tz="UTC")
    lev = bet(hours)
    state = np.sign(lev.round(3))
    btc = binance_1h("BTCUSDT")
    F = features(btc).reindex(hours)
    print("hours long / flat / short:", [f"{(state == s).mean():.0%}" for s in (1, 0, -1)])

    q = pd.qcut(F["r24"], 5, labels=["Q1 dump", "Q2", "Q3", "Q4", "Q5 pump"])
    print("\nlong / short share by past-24h BTC move:\n",
          pd.DataFrame({"long": state == 1, "short": state == -1, "q": q}).groupby("q", observed=True).mean().round(2).to_string())

    fwd = np.log(btc["close"].shift(-1) / btc["close"]).reindex(hours)
    print("\nmirror on BTC 2018-03..2021-12 (positions 1h late, 0.1% per side):")
    for label, pos in [("buy & hold", pd.Series(1.0, index=hours)), ("copy his long bets", (state == 1).astype(float)),
                       ("copy long + short bets", state.fillna(0))]:
        pos = pos.shift(1).fillna(0)
        ret = (pos * fwd - pos.diff().abs().fillna(0) * 0.001).dropna()
        day = ret.groupby(ret.index.floor("D")).sum()
        print(f"  {label:24s} {np.exp(day.sum()) - 1:+8.1%}  Sharpe {day.mean() / day.std() * np.sqrt(365):.2f}")

    y = (state.shift(-1) == 1)
    ok = F.notna().all(axis=1) & state.shift(-1).notna()
    X, y = F[ok], y[ok].astype(int)
    train = X.index < "2021-01-01"
    model = make_pipeline(StandardScaler(), LogisticRegression(max_iter=1000)).fit(X[train], y[train])
    print(f"\nclone AUC: train 2018-2020 {roc_auc_score(y[train], model.predict_proba(X[train])[:, 1]):.2f}, "
          f"validation 2021 {roc_auc_score(y[~train], model.predict_proba(X[~train])[:, 1]):.2f} (0.5 = coin flip)")

    base_rate = y[train].mean()
    prepared = {}
    for m in ("KRW-BTC", "KRW-ETH"):
        df = data.load(m, 60, 1825, offline=True)
        df = df[df.index < pd.Timestamp("2026-04-01", tz="UTC")]
        Fm = features(df)
        p = pd.Series(np.nan, index=df.index)
        good = Fm.notna().all(axis=1)
        p[good] = model.predict_proba(Fm[good])[:, 1]
        long_ = p > base_rate
        prepared[m] = _finish(df, enter=long_.shift(1), exit=(~long_).shift(1))
    res = backtest.run(prepared, Risk(), Costs(), start=pd.Timestamp("2022-04-02", tz="UTC"))
    m = res.metrics()
    print(f"clone traded on Upbit BTC+ETH 1h, 2022-04..2026-03: total {m['total_return']:+.1%}, "
          f"Sharpe {m['sharpe']:.2f}, MDD {m['max_drawdown']:.1%}, {m['trades']} trades")


if __name__ == "__main__":
    main()
