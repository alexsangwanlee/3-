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


def test_budget_caps_what_the_bot_spends(bot):
    bot.cfg.budget_krw = 100_000  # account holds 1,000,000 but the bot may only use 100,000
    bot.step(bot.client.df.index[-1] + pd.Timedelta(minutes=5))
    pos = bot.state["positions"]["KRW-BTC"]
    assert pos["qty"] * bot.client.price == pytest.approx(100_000)


def test_stay_in_cash_mode_opens_nothing_but_still_stops_out(bot):
    now = bot.client.df.index[-1] + pd.Timedelta(minutes=5)
    bot.cfg.allow_entries = False
    bot.step(now)
    assert bot.state["positions"] == {}

    price = bot.client.price
    bot.state["positions"]["KRW-BTC"] = {"qty": 1000.0, "entry": price, "stop": price * 1.01,
                                         "entry_time": str(now - pd.Timedelta(days=1))}
    bot.state["paper"]["holdings"]["KRW-BTC"] = 1000.0
    bot.step(now + pd.Timedelta(minutes=1))
    assert bot.state["positions"] == {}  # stop still fired


def test_notify_is_silent_without_telegram_settings(monkeypatch):
    from tradebot import live
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setattr(live.requests, "post", lambda *a, **k: pytest.fail("must not call Telegram"))
    live.notify("hello")


def test_notify_never_breaks_trading_when_telegram_is_down(monkeypatch):
    from tradebot import live
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    sent = []

    def down(url, json, timeout):
        sent.append(json)
        raise OSError("telegram is down")

    monkeypatch.setattr(live.requests, "post", down)
    live.notify("BUY KRW-BTC")  # must not raise
    assert sent == [{"chat_id": "42", "text": "BUY KRW-BTC"}]


def test_live_giveback_ratchets_the_stop_and_sells_on_the_drop(bot):
    now = bot.client.df.index[-1] + pd.Timedelta(minutes=5)
    bot.step(now)
    entry = bot.state["positions"]["KRW-BTC"]["entry"]
    bot.client.price = entry * 1.50  # +50%: armed, stop moves to 1.5 * 0.85 = 1.275 x entry
    bot.guard.daily_target = None  # isolate the stop rule from the daily profit lock
    bot.step(now + pd.Timedelta(minutes=1))
    assert bot.state["positions"]["KRW-BTC"]["stop"] == pytest.approx(entry * 1.275)
    bot.client.price = entry * 1.25
    bot.step(now + pd.Timedelta(minutes=2))
    assert bot.state["positions"] == {}
