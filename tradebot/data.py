"""Historical candle download + CSV cache."""
import time
from pathlib import Path

import pandas as pd

from .upbit import UpbitClient

COLS = ["open", "high", "low", "close", "volume"]


def to_frame(rows: list[dict]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=COLS, index=pd.DatetimeIndex([], tz="UTC"))
    df = pd.DataFrame({
        "time": pd.to_datetime([r["candle_date_time_utc"] for r in rows], utc=True),
        "open": [float(r["opening_price"]) for r in rows],
        "high": [float(r["high_price"]) for r in rows],
        "low": [float(r["low_price"]) for r in rows],
        "close": [float(r["trade_price"]) for r in rows],
        "volume": [float(r["candle_acc_trade_volume"]) for r in rows],
    })
    return df.set_index("time").sort_index()


def regularize(df: pd.DataFrame, unit: int) -> pd.DataFrame:
    """Upbit skips candles with no trades; fill them as flat bars."""
    df = df[~df.index.duplicated(keep="last")].sort_index()
    full = pd.date_range(df.index[0], df.index[-1], freq=f"{unit}min")
    df = df.reindex(full)
    df["close"] = df["close"].ffill()
    for col in ("open", "high", "low"):
        df[col] = df[col].fillna(df["close"])
    df["volume"] = df["volume"].fillna(0.0)
    return df


def fetch(client: UpbitClient, market: str, unit: int, since: pd.Timestamp, to: pd.Timestamp | None = None) -> pd.DataFrame:
    """Page backwards from `to` (or now) until `since`."""
    rows, cursor = [], None if to is None else to.strftime("%Y-%m-%dT%H:%M:%SZ")
    while True:
        page = client.candles(market, unit, to=cursor, count=200)
        if not page:
            break
        rows.extend(page)
        oldest = page[-1]["candle_date_time_utc"]
        if pd.Timestamp(oldest, tz="UTC") <= since or len(page) < 200:
            break
        cursor = oldest + "Z"
        time.sleep(0.12)  # stay under the 10 req/s candle limit
    df = to_frame(rows)
    return df[df.index >= since]


def load(market: str, unit: int = 60, days: int = 1095, cache_dir: str = "data",
         client: UpbitClient | None = None, offline: bool = False) -> pd.DataFrame:
    """Cached history for `market`; only the missing recent part is downloaded."""
    path = Path(cache_dir) / f"{market}_{unit}m.csv"
    since = pd.Timestamp.now(tz="UTC").floor("D") - pd.Timedelta(days=days)
    cached = None
    if path.exists():
        cached = pd.read_csv(path, index_col=0, parse_dates=True)
        cached.index = pd.to_datetime(cached.index, utc=True)
    if not offline:
        client = client or UpbitClient()
        if cached is not None and cached.index[0] <= since:
            new = fetch(client, market, unit, since=cached.index[-1])
        else:
            new = fetch(client, market, unit, since=since)
        cached = new if cached is None else pd.concat([cached, new])
        path.parent.mkdir(parents=True, exist_ok=True)
        cached = cached[~cached.index.duplicated(keep="last")].sort_index()
        cached.to_csv(path)
    if cached is None or cached.empty:
        raise FileNotFoundError(f"no data for {market}; run `python -m tradebot fetch` first")
    return regularize(cached[cached.index >= since], unit)
