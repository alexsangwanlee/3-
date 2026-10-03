import numpy as np
import pandas as pd
import pytest


def make_bars(n=24 * 60, seed=0, start="2024-01-01", drift=0.0, vol=0.01):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(drift, vol, n)))
    open_ = np.r_[100.0, close[:-1]]
    high = np.maximum(open_, close) * (1 + rng.uniform(0, vol, n))
    low = np.minimum(open_, close) * (1 - rng.uniform(0, vol, n))
    idx = pd.date_range(start, periods=n, freq="60min", tz="UTC")
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": 1.0}, index=idx)


def flat_bars(prices, start="2024-01-01"):
    """One bar per price with open=high=low=close (easy to reason about fills)."""
    idx = pd.date_range(start, periods=len(prices), freq="60min", tz="UTC")
    p = np.asarray(prices, dtype=float)
    return pd.DataFrame({"open": p, "high": p, "low": p, "close": p, "volume": 1.0}, index=idx)


@pytest.fixture
def bars():
    return make_bars()
