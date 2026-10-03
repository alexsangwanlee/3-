import numpy as np
import pytest

from tradebot.strategies import GRIDS, STRATEGIES, combos, prepare

SIGNALS = ["enter", "exit", "entry_stop", "stop_dist", "size", "vol"]
CASES = [(name, {}) for name in STRATEGIES] + [(name, combos(name)[-1]) for name in GRIDS]


@pytest.mark.parametrize("name,params", CASES)
def test_signals_do_not_look_ahead(bars, name, params):
    """A bar's signals may only use earlier bars plus its own open."""
    full = prepare(name, bars, params)
    o = bars.columns.get_loc("open")
    for cut in range(24 * 30 + 12, 24 * 32 + 12):  # spans two day boundaries
        for move in (1.3, 0.7):  # pump / dump the last bar, keeping its open
            partial = bars.iloc[:cut].copy()
            px = partial.iloc[-1, o] * move
            partial.iloc[-1, partial.columns.get_indexer(["high", "low", "close"])] = [
                max(px, partial.iloc[-1, o]), min(px, partial.iloc[-1, o]), px]
            got = prepare(name, partial, params).iloc[-1]
            want = full.iloc[cut - 1]
            for col in SIGNALS:
                assert np.isclose(float(got[col]), float(want[col]), equal_nan=True), (name, cut, move, col)


def test_vbo_level_is_open_plus_k_range(bars):
    out = prepare("vbo", bars, {"k": 0.5, "ma": 0})
    day2 = bars.loc["2024-01-02"]
    prev = bars.loc["2024-01-01"]
    level = day2["open"].iloc[0] + 0.5 * (prev["high"].max() - prev["low"].min())
    assert np.allclose(out.loc["2024-01-02", "entry_stop"], level)
    assert out.loc["2024-01-02", "exit"].iloc[0] and not out.loc["2024-01-02", "exit"].iloc[1:].any()


@pytest.mark.parametrize("name", ["donchian", "ema_cross"])
def test_entry_confirmation_does_not_look_ahead(bars, name):
    """The 3-of-4 confirmation (results/edge_study.md) only uses bars before the current one, BTC's included."""
    from conftest import make_bars
    btc = make_bars(seed=7)
    full = prepare(name, bars, {}, btc=btc["close"])
    for cut in range(24 * 40, 24 * 40 + 30):
        for move in (1.3, 0.7):
            partial, b = bars.iloc[:cut].copy(), btc["close"].iloc[:cut].copy()
            partial.iloc[-1, partial.columns.get_indexer(["high", "low", "close"])] *= move
            partial.iloc[-1, partial.columns.get_loc("volume")] *= 20  # a volume spike on the bar itself
            b.iloc[-1] *= move
            got = prepare(name, partial, {}, btc=b).iloc[-1]
            assert bool(got["enter"]) == bool(full["enter"].iloc[cut - 1]), (name, cut, move)
            assert np.isclose(float(got["entry_stop"]), float(full["entry_stop"].iloc[cut - 1]), equal_nan=True)


def test_no_entries_without_three_confirmations():
    from conftest import make_bars
    falling = make_bars(drift=-0.003, seed=3)  # below its 200-bar EMA, and so is "BTC"
    out = prepare("donchian", falling, {}, btc=falling["close"])
    late = out.iloc[300:]
    assert not late["enter"].any() and late["entry_stop"].isna().all()
    rising = make_bars(drift=0.002, seed=3)
    assert prepare("donchian", rising, {}, btc=rising["close"])["enter"].iloc[24 * 31:].any()  # the rule lets trends in
