"""Self-improvement with a gate: learn real costs from fills; change strategies only on out-of-sample proof."""
import numpy as np
import pandas as pd

from tradebot.learn import learned_slippage, review

DAYS = pd.date_range("2022-01-01", periods=900, freq="D", tz="UTC")


def returns(mean, seed, vol=0.01):
    return pd.Series(np.random.default_rng(seed).normal(mean, vol, len(DAYS)), index=DAYS)


def test_slippage_is_learned_from_live_fills_only_when_there_are_enough(tmp_path):
    p = tmp_path / "live_trades.csv"
    pd.DataFrame({"slip": [0.002] * 10}).to_csv(p, index=False)
    assert learned_slippage(p) is None  # 10 fills: too few to trust
    pd.DataFrame({"slip": [0.002] * 30 + [0.5]}).to_csv(p, index=False)
    assert learned_slippage(p) == 0.002  # median ignores one freak fill
    pd.DataFrame({"slip": [0.03] * 30}).to_csv(p, index=False)
    assert learned_slippage(p) == 0.005  # capped


def test_a_clearly_better_strategy_is_recommended():
    rets = {"a": returns(0.001, 1), "b": returns(0.001, 2), "c": returns(0.003, 3)}
    out = review(rets, ["a", "b"])
    assert out["recommend"] == ["a", "b", "c"]


def test_a_change_that_only_worked_in_the_past_is_not_recommended():
    c = returns(0.003, 3)
    c.iloc[-180:] = returns(-0.003, 4).iloc[-180:]  # great before, losing in the confirmation window
    out = review({"a": returns(0.001, 1), "b": returns(0.001, 2), "c": c}, ["a", "b"])
    assert out["recommend"] is None


def test_nothing_better_means_no_change():
    out = review({"a": returns(0.001, 1), "b": returns(0.001, 2), "c": returns(-0.001, 3)}, ["a", "b"])
    assert out["recommend"] is None
