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
