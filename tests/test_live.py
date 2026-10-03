import pandas as pd
import pytest
from conftest import make_bars

from tradebot.live import Bot, BotConfig
from tradebot.risk import Costs, Risk


class FakeClient:
    """Serves synthetic candles; `price` is the live ticker price."""

    def __init__(self, df):
        self.df, self.price = df, float(df["close"].iloc[-1])

    def candles(self, market, unit, to=None, count=200):
        df = self.df if to is None else self.df[self.df.index < pd.Timestamp(to)]
        return [{"candle_date_time_utc": t.strftime("%Y-%m-%dT%H:%M:%S"), "opening_price": r.open,
                 "high_price": r.high, "low_price": r.low, "trade_price": r.close,
                 "candle_acc_trade_volume": r.volume} for t, r in df.iloc[::-1].head(count).iterrows()]

    def tickers(self, markets):
        return {m: self.price for m in markets}


@pytest.fixture
def bot(tmp_path):
    df = make_bars(n=24 * 30)
    cfg = BotConfig(markets=["KRW-BTC"], strategy="hold", params={},
                    risk=Risk(daily_target=0.03, daily_loss_limit=0.02, max_drawdown=None, risk_per_trade=0),
                    costs=Costs(0, 0), history_bars=500,
                    state_path=str(tmp_path / "state.json"), trades_path=str(tmp_path / "trades.csv"))
    return Bot(cfg, FakeClient(df), paper_krw=1_000_000)


def test_paper_bot_enters_then_locks_daily_target(bot):
    now = bot.client.df.index[-1] + pd.Timedelta(minutes=5)
    bot.step(now)
    pos = bot.state["positions"]["KRW-BTC"]
    assert pos["qty"] * bot.client.price == pytest.approx(1_000_000)

    bot.client.price *= 1.04
    bot.step(now + pd.Timedelta(minutes=1))
    assert bot.state["positions"] == {} and bot.guard.locked
    assert bot.state["paper"]["cash"] == pytest.approx(1_040_000)

    bot.step(now + pd.Timedelta(minutes=2))  # locked: stays flat for the rest of the day
    assert bot.state["positions"] == {}


def test_state_survives_restart(bot):
    now = bot.client.df.index[-1] + pd.Timedelta(minutes=5)
    bot.step(now)
    again = Bot(bot.cfg, bot.client)
    assert again.state["positions"].keys() == {"KRW-BTC"}
    assert again.guard.day == bot.guard.day


def test_live_sell_never_touches_coins_the_bot_did_not_buy(bot):
    class AccountBroker:  # account already held 1 BTC before the bot bought 0.01
        sold = None

        def cash(self):
            return 0.0

        def holdings(self):
            return {"KRW-BTC": 1.01}

        def sell(self, market, qty, price):
            AccountBroker.sold = qty
            return price

    bot.broker = AccountBroker()
    bot.state["positions"]["KRW-BTC"] = {"qty": 0.01, "entry": 100.0, "stop": None, "entry_time": "2024-01-01"}
    bot._sell("KRW-BTC", 1e6, "exit")
    assert AccountBroker.sold == 0.01 and bot.state["positions"] == {}
