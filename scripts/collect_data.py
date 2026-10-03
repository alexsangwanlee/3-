"""Download the wide data set for scripts/data_study.py (research only; the bot does not need it).

    PYTHONPATH=. python scripts/collect_data.py

- Upbit daily candles for every KRW market (liquidity ranking, cross-sectional features)
- Upbit 4h candles for every coin that was ever among the 30 most traded (the wider trading universe)
- Fear & Greed index (alternative.me, daily)
- Binance USDT-perp funding rates per coin (data.binance.vision monthly dumps)
- Binance BTCUSDT open interest (data.binance.vision daily metrics)
- Kimchi premium: Upbit KRW-BTC vs Binance BTCUSDT x USD/KRW (ECB rate via frankfurter.dev)

Everything lands in data/ (gitignored). Re-running only downloads what is missing.
"""
import io
import json
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd
import requests

from tradebot import data
from tradebot.upbit import UpbitClient

DAYS = 1825
TOP = 30
EXT = Path("data/ext")
START = pd.Timestamp.now(tz="UTC").floor("D") - pd.Timedelta(days=DAYS)
http = requests.Session()


def get(url, **kw):
    for attempt in range(4):
        try:
            r = http.get(url, timeout=30, **kw)
            if r.status_code in (404, 451):
                return None
            r.raise_for_status()
            return r
        except requests.RequestException:
            time.sleep(2 ** attempt)
    return None


def upbit_daily(client: UpbitClient, market: str) -> pd.DataFrame:
    path = Path("data/daily") / f"{market}.csv"
    if path.exists():
        return pd.read_csv(path, index_col=0, parse_dates=True)
    rows, cursor = [], None
    while True:
        page = client._request("GET", "/candles/days", {"market": market, "count": 200, **({"to": cursor} if cursor else {})})
        if not page:
            break
        rows += page
        if len(page) < 200 or pd.Timestamp(page[-1]["candle_date_time_utc"], tz="UTC") <= START:
            break
        cursor = page[-1]["candle_date_time_utc"] + "Z"
        time.sleep(0.12)
    df = data.to_frame(rows)
    value = {pd.Timestamp(r["candle_date_time_utc"], tz="UTC"): float(r["candle_acc_trade_price"]) for r in rows}
    df["value"] = pd.Series(value).reindex(df.index)
    path.parent.mkdir(parents=True, exist_ok=True)
    df[df.index >= START].to_csv(path)
    return df


def universe(daily: dict[str, pd.DataFrame]) -> list[str]:
    """Every coin that was among the TOP most traded (30-day KRW value) on some day."""
    value = pd.DataFrame({m: d["value"] for m, d in daily.items()}).rolling(30, min_periods=20).sum()
    rank = value.rank(axis=1, ascending=False)
    return sorted(m for m in value if (rank[m] <= TOP).any())


def funding(coin: str) -> pd.Series:
    path = EXT / f"funding_{coin}.csv"
    if path.exists():
        return pd.read_csv(path, index_col=0, parse_dates=True).iloc[:, 0]
    out = []
    for month in pd.period_range(START.tz_localize(None), pd.Timestamp.now(), freq="M")[:-1]:
        r = get(f"https://data.binance.vision/data/futures/um/monthly/fundingRate/{coin}USDT/{coin}USDT-fundingRate-{month}.zip")
        if r is None:
            continue
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            df = pd.read_csv(z.open(z.namelist()[0]))
        out.append(pd.Series(df["last_funding_rate"].values, index=pd.to_datetime(df["calc_time"], unit="ms", utc=True)))
    s = pd.concat(out).sort_index() if out else pd.Series(dtype=float)
    s.rename("funding").to_csv(path)
    return s


def open_interest() -> pd.Series:
    path = EXT / "oi_BTC.csv"
    if path.exists():
        return pd.read_csv(path, index_col=0, parse_dates=True).iloc[:, 0]

    def day(d):
        r = get(f"https://data.binance.vision/data/futures/um/daily/metrics/BTCUSDT/BTCUSDT-metrics-{d:%Y-%m-%d}.zip")
        if r is None:
            return None
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            df = pd.read_csv(z.open(z.namelist()[0]))
        return d, float(df["sum_open_interest_value"].mean())

    days = pd.date_range(START.tz_localize(None), pd.Timestamp.now().floor("D") - pd.Timedelta(days=2))
    with ThreadPoolExecutor(16) as pool:
        got = [x for x in pool.map(day, days) if x]
    s = pd.Series({d: v for d, v in got}).sort_index()
    s.index = s.index.tz_localize("UTC")
    s.rename("oi").to_csv(path)
    return s


def fear_greed() -> pd.Series:
    rows = get("https://api.alternative.me/fng/?limit=0&format=json").json()["data"]
    s = pd.Series({pd.Timestamp(int(r["timestamp"]), unit="s", tz="UTC"): float(r["value"]) for r in rows}).sort_index()
    s.rename("fng").to_csv(EXT / "fng.csv")
    return s


def kimchi(upbit_btc: pd.DataFrame) -> pd.Series:
    rows, start = [], int(START.timestamp() * 1000)
    while True:
        page = get("https://data-api.binance.vision/api/v3/klines",
                   params={"symbol": "BTCUSDT", "interval": "1d", "startTime": start, "limit": 1000}).json()
        rows += page
        if len(page) < 1000:
            break
        start = page[-1][0] + 1
    usdt = pd.Series({pd.Timestamp(r[0], unit="ms", tz="UTC"): float(r[4]) for r in rows})
    fx = get(f"https://api.frankfurter.dev/v1/{START:%Y-%m-%d}..?from=USD&to=KRW").json()["rates"]
    fx = pd.Series({pd.Timestamp(d, tz="UTC"): v["KRW"] for d, v in fx.items()}).sort_index()
    fx = fx.reindex(usdt.index.union(fx.index)).ffill().reindex(usdt.index)
    s = (upbit_btc["close"].reindex(usdt.index) / (usdt * fx) - 1).dropna()
    s.rename("kimchi").to_csv(EXT / "kimchi.csv")
    return s


def main():
    EXT.mkdir(parents=True, exist_ok=True)
    client = UpbitClient()
    markets = sorted(r["market"] for r in client._request("GET", "/market/all") if r["market"].startswith("KRW-"))
    daily = {}
    for i, m in enumerate(markets):
        daily[m] = upbit_daily(client, m)
        if i % 25 == 0:
            print(f"daily {i}/{len(markets)}", flush=True)
    uni = universe(daily)
    (EXT / "universe.json").write_text(json.dumps(uni))
    print(f"universe: {len(uni)} coins ever in the top {TOP}", flush=True)
    for i, m in enumerate(uni):
        data.load(m, 240, DAYS, client=client)
        print(f"4h {i + 1}/{len(uni)} {m}", flush=True)
    print("fear & greed", len(fear_greed()), flush=True)
    print("kimchi premium", len(kimchi(daily["KRW-BTC"])), flush=True)
    with ThreadPoolExecutor(8) as pool:
        n = list(pool.map(lambda m: len(funding(m.split("-")[1])), uni))
    print(f"funding: {sum(1 for x in n if x)} of {len(uni)} coins have Binance perps", flush=True)
    print("open interest", len(open_interest()), flush=True)


if __name__ == "__main__":
    main()
