import numpy as np
import pandas as pd
import pytest
from conftest import flat_bars

from tradebot import backtest
from tradebot.risk import Costs, Risk
from tradebot.strategies import _finish, prepare

NO_GUARD = Risk(daily_target=None, daily_loss_limit=None, max_drawdown=None, risk_per_trade=0)
COSTS = Costs(fee=0.001, slippage=0.002)


def test_round_trip_costs_on_flat_price():
    df = prepare("hold", flat_bars([100] * 5))
    res = backtest.run({"X": df}, NO_GUARD, COSTS)
    assert res.equity.iloc[-1] == pytest.approx(1 / (1.001 * 1.002))


@pytest.mark.parametrize("unit", ["ns", "us", "s"])  # CSV round-trips may change the resolution
def test_daily_target_locks_profit_until_next_day(unit):
    prices = [100 * 1.01 ** i for i in range(24)] + [130, 130, 132.6]  # +1%/bar on day 1, +2% day 2
    df = flat_bars(prices)
    df.index = df.index.as_unit(unit)
    res = backtest.run({"X": prepare("hold", df)}, Risk(daily_target=0.03, daily_loss_limit=None,
                                                                      max_drawdown=None, risk_per_trade=0), Costs(0, 0))
    first = res.trades.iloc[0]
    assert first["reason"] == "daily_target"
    assert first["pnl"] == pytest.approx(1.01 ** 3 - 1)  # entered at bar 0 open, locked at bar 3 close
    day1 = res.equity.loc["2024-01-01"]
    assert (day1.iloc[3:] == day1.iloc[3]).all()  # no more trading that day
    assert len(res.trades) == 1  # re-entered on day 2, still open
    assert res.equity.iloc[-1] == pytest.approx(1.01 ** 3 * 1.02)


def test_daily_loss_limit_and_stop_loss():
    prices = [100, 100, 99, 97, 96]
    df = _finish(flat_bars(prices), enter=pd.Series([True] + [False] * 4, index=flat_bars(prices).index))
    df["stop_dist"] = 2.5
    res = backtest.run({"X": df}, Risk(daily_target=None, daily_loss_limit=None, max_drawdown=None,
                                       risk_per_trade=0), Costs(0, 0))
    t = res.trades.iloc[0]
    assert t["reason"] == "stop" and t["exit"] == 97  # stop at 97.5, bar opened at 97 (gap) -> fill 97

    res = backtest.run({"X": prepare("hold", flat_bars(prices))},
                       Risk(daily_target=None, daily_loss_limit=0.02, max_drawdown=None, risk_per_trade=0), Costs(0, 0))
    assert res.trades.iloc[0]["reason"] == "daily_loss"
    assert res.equity.iloc[-1] == pytest.approx(0.97)


def test_breakout_fills_at_level_once_per_day():
    df = flat_bars([100, 100, 104, 98, 104, 107])
    df.loc[df.index[1], "high"] = 103
    df = _finish(df, entry_stop=pd.Series(102.0, index=df.index))
    df["stop_dist"] = 3.0
    res = backtest.run({"X": df}, NO_GUARD, Costs(0, 0))
    # bar 1 touched 102 -> filled at the level; bar 3 gapped through the 99 stop
    t = res.trades.iloc[0]
    assert (t["entry"], t["exit"], t["reason"]) == (102, 98, "stop")
    assert len(res.trades) == 1  # bar 4 touches 102 again, but only one breakout per day
    assert res.equity.iloc[-1] == pytest.approx(98 / 102)


def test_max_drawdown_halts_forever():
    prices = [100] * 24 + [70] * 24 + [140] * 24
    res = backtest.run({"X": prepare("hold", flat_bars(prices))},
                       Risk(daily_target=None, daily_loss_limit=None, max_drawdown=0.25, risk_per_trade=0), Costs(0, 0))
    assert list(res.trades["reason"]) == ["max_drawdown"]
    assert res.equity.iloc[-1] == pytest.approx(0.7)


def test_two_markets_split_capital():
    a = prepare("hold", flat_bars([100, 110]))
    b = prepare("hold", flat_bars([100, 90]))
    res = backtest.run({"A": a, "B": b}, NO_GUARD, Costs(0, 0))
    assert res.equity.iloc[-1] == pytest.approx(1.0)


def test_metrics_basic():
    rets = pd.Series([0.1, -0.5, 0.0], index=pd.date_range("2024-01-01", periods=3, tz="UTC"))
    m = backtest.metrics(rets)
    assert m["total_return"] == pytest.approx(1.1 * 0.5 - 1)
    assert m["max_drawdown"] == pytest.approx(-0.5)
    assert m["days_ge_3pct"] == pytest.approx(1 / 3)
    assert np.isfinite(m["sharpe"])
