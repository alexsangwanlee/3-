"""Real money path: the real UpbitBroker on a fake exchange that times out, refuses and half-fills."""
import pandas as pd
import pytest
import requests
from conftest import make_bars

from tradebot import live
from tradebot.live import Bot, BotConfig, UpbitBroker
from tradebot.risk import Costs, Risk

FEE = 0.0005


class Exchange:
    """One Upbit account. Orders fill at `price`; switches simulate the failures seen in production."""

    def __init__(self, krw=1_000_000):
        self.df = make_bars(n=24 * 30)
        self.price = float(self.df["close"].iloc[-1])
        self.krw, self.coins, self.orders, self.placed = krw, {}, {}, []
        self.lookup = "ok"     # "ok" | "wait": the order query never says done (timeout, missing permission)
        self.fill = True       # False: market sell cancelled with nothing traded
        self.down = set()      # markets whose orders raise before reaching the exchange
        self.no_price = set()  # markets the ticker endpoint rejects (delisted)

    def candles(self, market, unit, to=None, count=200):
        df = self.df if to is None else self.df[self.df.index < pd.Timestamp(to)]
        return [{"candle_date_time_utc": t.strftime("%Y-%m-%dT%H:%M:%S"), "opening_price": r.open,
                 "high_price": r.high, "low_price": r.low, "trade_price": r.close,
                 "candle_acc_trade_volume": r.volume} for t, r in df.iloc[::-1].head(count).iterrows()]

    def tickers(self, markets):
        if self.no_price & set(markets):
            raise RuntimeError('Upbit GET /ticker -> 404: {"error":{"name":404,"message":"Code not found"}}')
        return {m: self.price for m in markets}

    def accounts(self):
        return [{"currency": "KRW", "balance": str(self.krw)}] + [
            {"currency": m.split("-")[1], "balance": str(v)} for m, v in self.coins.items()]

    def _done(self, identifier, vol):
        self.orders[identifier] = {"uuid": identifier, "state": "done" if vol else "cancel",
                                   "trades": [{"volume": str(vol), "funds": str(vol * self.price)}] if vol else []}
        return {"uuid": identifier}

    def buy_market(self, market, krw, identifier=None):
        if market in self.down:
            raise requests.ConnectionError("network down")
        vol = int(krw / self.price * 1e8) / 1e8
        self.krw -= vol * self.price * (1 + FEE)
        self.coins[market] = self.coins.get(market, 0) + vol
        self.placed.append(("buy", market))
        return self._done(identifier, vol)

    def sell_market(self, market, volume, identifier=None):
        if market in self.down:
            raise requests.ConnectionError("network down")
        vol = volume if self.fill else 0.0
        self.coins[market] -= vol
        self.krw += vol * self.price * (1 - FEE)
        self.placed.append(("sell", market))
        return self._done(identifier, vol)

    def order(self, uuid=None, identifier=None):
        key = uuid or identifier
        if key not in self.orders:
            raise RuntimeError('Upbit GET /order -> 404: {"error":{"name":"order_not_found"}}')
        return {"state": "wait", "trades": []} if self.lookup == "wait" else self.orders[key]


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(live.time, "sleep", lambda s: None)
    monkeypatch.setattr(live, "notify", lambda text: None)


def make_bot(tmp_path, ex, markets=("KRW-BTC",), risk=None, funding=500_000):
    cfg = BotConfig(markets=list(markets), strategy="hold", params={},
                    risk=risk or Risk(daily_loss_limit=None, max_drawdown=None, lock_gain=None, risk_per_trade=0),
                    costs=Costs(fee=FEE, slippage=0), history_bars=500, state_path=str(tmp_path / "state.json"),
                    trades_path=str(tmp_path / "trades.csv"), equity_path=str(tmp_path / "equity.csv"))
    return Bot(cfg, ex, broker=UpbitBroker(ex), funding=funding)


def hold(bot, ex, market, qty, stop=None):
    """The bot already owns `qty` of `market`, bought at the current price."""
    ex.coins[market] = ex.coins.get(market, 0) + qty
    bot.state["positions"][market] = {"qty": qty, "entry": ex.price, "stop": stop, "high": ex.price,
                                      "entry_time": "2024-01-01T00:00:00+00:00"}
    bot.state["cash"] -= qty * ex.price


def now(ex, k=0):
    return ex.df.index[-1] + pd.Timedelta(minutes=5, seconds=10 * k)


def test_a_buy_the_exchange_did_not_confirm_is_booked_later_never_bought_twice(tmp_path):
    ex = Exchange(krw=3_000_000)  # the account holds more than the bot's 500k
    bot = make_bot(tmp_path, ex)
    ex.lookup = "wait"
    with pytest.raises(RuntimeError):
        bot.step(now(ex))
    restarted = make_bot(tmp_path, ex)  # crash + restart while the order is unconfirmed
    ex.lookup = "ok"
    restarted.step(now(ex, 1))
    restarted.step(now(ex, 2))
    assert ex.placed == [("buy", "KRW-BTC")]
    assert restarted.state["positions"]["KRW-BTC"]["qty"] == pytest.approx(ex.coins["KRW-BTC"])
    assert restarted.state["cash"] == pytest.approx(500_000 - ex.coins["KRW-BTC"] * ex.price * (1 + FEE))


def test_an_order_that_never_reached_the_exchange_is_forgotten_and_retried(tmp_path):
    ex = Exchange()
    bot = make_bot(tmp_path, ex)
    ex.down.add("KRW-BTC")
    with pytest.raises(requests.ConnectionError):
        bot.step(now(ex))
    ex.down.clear()
    bot.step(now(ex, 1))
    assert ex.placed == [("buy", "KRW-BTC")] and "KRW-BTC" in bot.state["positions"]


def test_a_sell_that_filled_nothing_keeps_the_position(tmp_path):
    ex = Exchange(krw=0)
    bot = make_bot(tmp_path, ex)
    bot.cfg.allow_entries = False
    hold(bot, ex, "KRW-BTC", 1000.0, stop=ex.price * 0.95)
    cash = bot.state["cash"]
    ex.price *= 0.94
    ex.fill = False
    bot.step(now(ex))
    assert bot.state["positions"]["KRW-BTC"]["qty"] == 1000.0 and bot.state["cash"] == cash
    ex.fill = True
    bot.step(now(ex, 1))  # still below the stop: sold on the retry
    assert bot.state["positions"] == {} and ex.coins["KRW-BTC"] == 0


def test_a_failing_market_never_blocks_the_other_stops(tmp_path):
    ex = Exchange(krw=0)
    bot = make_bot(tmp_path, ex, markets=["KRW-BTC", "KRW-ETH"])
    bot.cfg.allow_entries = False
    for m in ("KRW-BTC", "KRW-ETH"):
        hold(bot, ex, m, 1000.0, stop=ex.price * 0.95)
    ex.down.add("KRW-BTC")  # e.g. 403 market_offline
    ex.price *= 0.70
    with pytest.raises(requests.ConnectionError):  # reported, but only after every other market was handled
        bot.step(now(ex))
    assert list(bot.state["positions"]) == ["KRW-BTC"] and ex.coins["KRW-ETH"] == 0


def test_a_position_in_a_market_removed_from_the_config_still_stops_out(tmp_path):
    ex = Exchange(krw=0)
    bot = make_bot(tmp_path, ex, markets=["KRW-BTC"])
    bot.cfg.allow_entries = False
    hold(bot, ex, "KRW-XRP", 1000.0, stop=ex.price * 0.95)
    ex.price *= 0.80
    bot.step(now(ex))
    assert bot.state["positions"] == {} and ex.coins["KRW-XRP"] == 0


def test_a_delisted_market_does_not_hide_the_prices_of_the_others(tmp_path):
    ex = Exchange(krw=0)
    bot = make_bot(tmp_path, ex, markets=["KRW-BTC", "KRW-DEAD"])
    bot.cfg.allow_entries = False
    hold(bot, ex, "KRW-BTC", 1000.0, stop=ex.price * 0.95)
    ex.no_price.add("KRW-DEAD")
    ex.price *= 0.80
    bot.step(now(ex))
    assert bot.state["positions"] == {}


def test_liquidation_after_the_daily_loss_limit_is_retried_until_flat(tmp_path):
    ex = Exchange(krw=0)
    bot = make_bot(tmp_path, ex, markets=["KRW-BTC", "KRW-ETH"],
                   risk=Risk(daily_loss_limit=0.05, max_drawdown=None, lock_gain=None, risk_per_trade=0))
    bot.cfg.allow_entries = False
    for m in ("KRW-BTC", "KRW-ETH"):
        hold(bot, ex, m, 2500.0)
    bot.step(now(ex))
    ex.price *= 0.93  # -7% on the day
    ex.down.add("KRW-BTC")
    with pytest.raises(requests.ConnectionError):
        bot.step(now(ex, 1))
    ex.down.clear()  # network back
    bot.step(now(ex, 2))
    assert bot.state["positions"] == {} and not bot.state.get("flatten")


def test_a_leftover_too_small_to_sell_stays_on_the_books_instead_of_counting_as_a_loss(tmp_path):
    ex = Exchange(krw=100_000)
    bot = make_bot(tmp_path, ex, funding=50_000,
                   risk=Risk(daily_loss_limit=0.05, max_drawdown=None, lock_gain=None, risk_per_trade=0))
    bot.cfg.allow_entries = False
    bot.step(now(ex))
    hold(bot, ex, "KRW-BTC", 5_200 / ex.price, stop=ex.price * 0.96)
    ex.price *= 0.955  # stop hit, but the position is now worth < 5,000 KRW (Upbit's minimum)
    bot.step(now(ex, 1))
    assert "KRW-BTC" in bot.state["positions"] and not bot.guard.locked
    assert bot.equity({"KRW-BTC": ex.price}) == pytest.approx(50_000 - 5_200 * 0.045)


def test_giveback_zero_turns_the_rule_off(tmp_path):
    ex = Exchange(krw=0)
    bot = make_bot(tmp_path, ex, risk=Risk(daily_loss_limit=None, max_drawdown=None, lock_gain=0.30, lock_giveback=0))
    bot.cfg.allow_entries = False
    hold(bot, ex, "KRW-BTC", 1000.0, stop=ex.price * 0.9)
    ex.price *= 1.31
    bot.step(now(ex))
    ex.price *= 0.9999
    bot.step(now(ex, 1))
    assert "KRW-BTC" in bot.state["positions"]


def test_an_unconfirmed_sell_is_not_sent_again_by_the_liquidation_in_the_same_step(tmp_path):
    ex = Exchange(krw=0)
    bot = make_bot(tmp_path, ex, markets=["KRW-BTC", "KRW-ETH"],
                   risk=Risk(daily_loss_limit=0.05, max_drawdown=None, lock_gain=None, risk_per_trade=0))
    bot.cfg.allow_entries = False
    hold(bot, ex, "KRW-BTC", 2500.0, stop=ex.price * 0.95)
    hold(bot, ex, "KRW-ETH", 2500.0)
    bot.step(now(ex))
    ex.price *= 0.93  # BTC stop and the daily loss limit in the same step
    ex.lookup = "wait"  # the stop-loss sell executes but is never confirmed
    with pytest.raises(RuntimeError):
        bot.step(now(ex, 1))
    assert ex.placed.count(("sell", "KRW-BTC")) == 1
    ex.lookup = "ok"
    bot.step(now(ex, 2))
    assert bot.state["positions"] == {} and ex.placed.count(("sell", "KRW-BTC")) == 1


def test_paused_buys_still_let_stops_fire(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "PAUSE_FILE", tmp_path / "paused.json")
    live.pause_entries("Claude: 점검 필요")
    ex = Exchange(krw=1_000_000)
    bot = make_bot(tmp_path, ex, markets=["KRW-BTC", "KRW-ETH"])
    hold(bot, ex, "KRW-ETH", 1000.0, stop=ex.price * 0.95)
    bot.step(now(ex))
    assert ("buy", "KRW-BTC") not in ex.placed  # paused: the hold strategy would buy otherwise
    ex.price *= 0.90
    bot.step(now(ex, 1))
    assert "KRW-ETH" not in bot.state["positions"]
    live.PAUSE_FILE.unlink()
    bot.step(now(ex, 2))
    assert ("buy", "KRW-BTC") in ex.placed


def test_every_fill_records_its_fee_and_slippage_in_krw(tmp_path):
    ex = Exchange(krw=1_000_000)
    bot = make_bot(tmp_path, ex)
    bot.step(now(ex))
    ex.price *= 1.10
    bot.cfg.allow_entries = False
    bot.state["positions"]["KRW-BTC"]["stop"] = ex.price * 2  # force a sell
    bot.step(now(ex, 1))
    rows = pd.read_csv(tmp_path / "trades.csv")
    assert list(rows["side"]) == ["buy", "sell"]
    assert rows["fee"].to_numpy() == pytest.approx(rows["value"].to_numpy() * FEE, abs=0.01)
    assert rows["value"].iloc[0] == pytest.approx(rows["qty"].iloc[0] * rows["price"].iloc[0])
    assert "slip_cost" in rows


def test_the_real_money_check_buys_and_sells_back_through_the_bots_order_path(monkeypatch):
    from tradebot.__main__ import order_roundtrip
    ex = Exchange(krw=50_000)
    lines = order_roundtrip(ex, krw=6_000)
    assert ex.placed == [("buy", "KRW-BTC"), ("sell", "KRW-BTC")]
    assert ex.coins["KRW-BTC"] == pytest.approx(0, abs=1e-8)  # nothing left behind
    assert any("주문번호로 다시 조회" in line for line in lines)
    assert ex.krw == pytest.approx(50_000 - 6_000 * 2 * FEE, abs=1)  # only the fees were spent


def test_the_real_money_check_refuses_without_enough_krw():
    from tradebot.__main__ import order_roundtrip
    ex = Exchange(krw=3_000)
    with pytest.raises(SystemExit):
        order_roundtrip(ex, krw=6_000)
    assert ex.placed == []


def test_without_btc_candles_exits_still_work_and_nothing_is_bought_unconfirmed(tmp_path, monkeypatch):
    ex = Exchange()
    bot = make_bot(tmp_path, ex, markets=["KRW-ETH"])
    bot.cfg.strategy = "ema_cross"
    real = ex.candles
    monkeypatch.setattr(ex, "candles", lambda m, *a, **k: (_ for _ in ()).throw(RuntimeError("503")) if m == "KRW-BTC"
                        else real(m, *a, **k))
    hold(bot, ex, "KRW-ETH", 1000.0, stop=ex.price * 0.5)
    monkeypatch.setattr(live, "prepare", lambda *a, **k: pd.DataFrame(
        {"enter": [True], "exit": [True], "entry_stop": [ex.price], "stop_dist": [1.0], "size": [1.0], "vol": [0.01]}))
    bot.step(now(ex))
    assert "KRW-ETH" not in bot.state["positions"]  # the exit signal still sold
    assert ("buy", "KRW-ETH") not in ex.placed


def test_sell_volume_keeps_every_satoshi():
    from tradebot.upbit import UpbitClient
    sent = {}
    c = UpbitClient("a" * 40, "s" * 40)
    c._request = lambda method, path, params=None, auth=False: sent.update(params) or {"uuid": "x"}
    c.sell_market("KRW-BTC", 0.00100002)
    assert sent["volume"] == "0.00100002"


def test_the_real_money_check_does_not_say_sell_by_hand_when_the_sell_went_through(monkeypatch):
    from tradebot.__main__ import order_roundtrip
    ex = Exchange(krw=50_000)
    real_sell = ex.sell_market

    def sold_but_reply_lost(market, volume, identifier=None):
        real_sell(market, volume, identifier)
        raise requests.ReadTimeout("reply lost")

    monkeypatch.setattr(ex, "sell_market", sold_but_reply_lost)
    lines = order_roundtrip(ex, krw=6_000)  # no SystemExit telling the user to sell coins they no longer have
    assert ex.coins["KRW-BTC"] == pytest.approx(0, abs=1e-8) and any("매도 체결" in line for line in lines)
