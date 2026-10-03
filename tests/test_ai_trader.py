"""The "ai" sleeve: Claude and GPT decide its trades and check each other, inside the bot's hard limits."""
import json

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from test_live_exchange import Exchange, hold, make_bot, now

from tradebot import ai_trader, claude, config, live

FACTS = {"KRW-BTC": {"held": None, "confirmation_3_of_4": True},
         "KRW-ETH": {"held": {"entry": 1.0}, "confirmation_3_of_4": True}}


@pytest.fixture
def ai(tmp_path, monkeypatch):
    """Two AIs with fake keys; `script` holds what each will answer, `calls` what each was asked."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "state").mkdir()
    monkeypatch.setattr(live.time, "sleep", lambda s: None)
    monkeypatch.setattr(live, "notify", lambda text: None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-0123456789abcdefghij")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-fake-0123456789abcdefghij")
    monkeypatch.setenv("UPBIT_SECRET_KEY", "upbit-secret-never-sent-0123456789")
    monkeypatch.setattr(ai_trader, "candidates", lambda bot, bar: {"KRW-BTC"})
    state = {"script": {}, "calls": []}

    def call(provider, system, payload, tool):
        state["calls"].append((provider, tool["name"], payload, system))
        answer = state["script"][tool["name"]]
        if isinstance(answer, Exception):
            raise answer
        if callable(answer):
            answer = answer()
        return answer, {"input_tokens": 1000, "output_tokens": 100}

    monkeypatch.setattr(claude, "call", call)
    return state


def ai_bot(tmp_path, ex, markets=("KRW-BTC",), paper=False):
    bot = make_bot(tmp_path, ex, markets=markets)
    bot.cfg.strategy = "ai"
    if paper:
        bot.broker = live.PaperBroker(bot.state["paper_holdings"], bot.cfg.costs)
    return bot


def midnight(ex, days=1):
    """A 00:xx UTC poll: the daily look at holdings (otherwise the AIs are only asked when a rule signals)."""
    return (ex.df.index[-1] + pd.Timedelta(days=days)).normalize() + pd.Timedelta(minutes=5)


def decide(bot, t):
    """Step, let the AIs' background decision finish, step again in the same bar (as the next poll would)."""
    bot.step(t)
    thread = (getattr(bot, "_ai", None) or {}).get("thread")
    if thread:
        thread.join(5)
    bot.step(t + pd.Timedelta(seconds=10))


def propose(*decisions, rules=()):
    return {"decisions": [{"market": m, "action": a, "rule": r, "reason": "근거"} for m, a, r in decisions],
            "rule_proposals": list(rules)}


def review(agree=(), sell=(), accept=()):
    return {"reviews": [{"market": m, "agree": True, "reason": "동의"} for m in agree],
            "sell": list(sell), "rule_votes": [{"index": i, "accept": True, "reason": "좋음"} for i in accept],
            "note": ""}


def test_a_buy_needs_both_ais_and_the_bot_sets_size_and_stop(tmp_path, ai):
    ex = Exchange()
    ai["script"] = {"propose_trades": propose(("KRW-BTC", "buy", "R1")), "review_trades": review(agree=["KRW-BTC"])}
    bot = ai_bot(tmp_path, ex)
    decide(bot, now(ex))
    pos = bot.state["positions"]["KRW-BTC"]
    assert [c[:2] for c in ai["calls"]] == [(ai["calls"][0][0], "propose_trades"), (ai["calls"][1][0], "review_trades")]
    assert ai["calls"][0][0] != ai["calls"][1][0]  # proposer and reviewer are different AIs
    assert pos["rule"] == "R1" and pos["stop"] < pos["entry"]  # the 2-ATR stop comes from the bot, not the AI
    assert pos["qty"] * pos["entry"] <= 500_000 * 1.01  # never more than the sleeve's own money


def test_a_buy_the_reviewer_rejects_is_not_made(tmp_path, ai):
    ex = Exchange()
    ai["script"] = {"propose_trades": propose(("KRW-BTC", "buy", "R1")), "review_trades": review(agree=[])}
    bot = ai_bot(tmp_path, ex)
    decide(bot, now(ex))
    assert bot.state["positions"] == {} and ex.placed == []


def test_either_ai_can_sell_because_selling_only_reduces_risk(tmp_path, ai):
    ex = Exchange(krw=0)
    bot = ai_bot(tmp_path, ex)
    hold(bot, ex, "KRW-BTC", 1000.0, stop=ex.price * 0.5)
    bot.state["positions"]["KRW-BTC"]["entry_time"] = "2020-01-01T00:00:00+00:00"
    ai["script"] = {"propose_trades": propose(("KRW-BTC", "hold", "R2")), "review_trades": review(sell=["KRW-BTC"])}
    decide(bot, midnight(ex))
    assert bot.state["positions"] == {}


def test_with_one_ai_the_bots_own_confirmation_is_the_second_opinion(tmp_path, ai, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY")
    ex = Exchange()
    ai["script"] = {"propose_trades": propose(("KRW-BTC", "buy", "R1"))}
    monkeypatch.setattr(ai_trader, "confirmed_now", lambda hist, btc: False)
    bot = ai_bot(tmp_path, ex)
    decide(bot, now(ex))
    assert ex.placed == [] and [c[1] for c in ai["calls"]] == ["propose_trades"]


def test_a_rule_is_adopted_only_when_the_other_ai_accepts_and_its_record_is_kept(tmp_path, ai):
    ex = Exchange()
    new_rule = {"op": "add", "rule_id": "", "text": "거래량 2배 이상 돌파만 산다", "why": "가짜 돌파 줄이기"}
    ai["script"] = {"propose_trades": propose(("KRW-BTC", "buy", "R1"), rules=[new_rule]),
                    "review_trades": review(agree=["KRW-BTC"], accept=[0])}
    bot = ai_bot(tmp_path, ex)
    decide(bot, now(ex))
    rules = json.loads(ai_trader.RULEBOOK.read_text(encoding="utf-8"))
    assert any(r["text"] == new_rule["text"] for r in rules)
    bot.state["positions"]["KRW-BTC"]["entry_time"] = "2020-01-01T00:00:00+00:00"  # simulated clock vs wall clock
    ex.price *= 1.10  # the R1 trade is closed by a sell decision and its record lands on R1
    ai["script"] = {"propose_trades": propose(("KRW-BTC", "sell", "R1")), "review_trades": review()}
    decide(bot, midnight(ex))
    r1 = next(r for r in json.loads(ai_trader.RULEBOOK.read_text(encoding="utf-8")) if r["id"] == "R1")
    assert r1["trades"] == 1 and r1["wins"] == 1 and r1["pnl"] > 0


def test_no_decision_point_means_no_call_and_no_tokens(tmp_path, ai, monkeypatch):
    monkeypatch.setattr(ai_trader, "candidates", lambda bot, bar: set())
    ex = Exchange()
    bot = ai_bot(tmp_path, ex)
    decide(bot, now(ex))
    assert ai["calls"] == []


def test_an_ai_outage_means_no_new_buys_but_stops_still_work(tmp_path, ai):
    ex = Exchange(krw=0)
    ai["script"] = {"propose_trades": RuntimeError("529 overloaded"), "review_trades": review()}
    bot = ai_bot(tmp_path, ex)
    hold(bot, ex, "KRW-ETH", 1000.0, stop=ex.price * 0.95)
    ex.price *= 0.9
    decide(bot, now(ex))
    assert "KRW-ETH" not in bot.state["positions"] and ("buy", "KRW-BTC") not in ex.placed


def test_the_ais_never_see_a_secret(tmp_path, ai):
    ex = Exchange()
    ai["script"] = {"propose_trades": propose(("KRW-BTC", "buy", "R1")), "review_trades": review(agree=["KRW-BTC"])}
    decide(ai_bot(tmp_path, ex), now(ex))
    sent = json.dumps([c[2:] for c in ai["calls"]], ensure_ascii=False, default=str)
    assert "upbit-secret-never-sent-0123456789" not in sent and "sk-ant-fake" not in sent


def test_real_money_for_the_ai_needs_28_days_of_paper_results(tmp_path, monkeypatch):
    from tradebot.__main__ import diagnose
    from test_setup import LIVE_KEYS, FakeClient
    monkeypatch.chdir(tmp_path)
    env = {**LIVE_KEYS, "ANTHROPIC_API_KEY": "sk-ant-fake-0123456789abcdefghij"}
    cfg = config.Config(mode="live", budget_krw=1_000_000, strategies=["donchian", "ai"])
    _, problems = diagnose(cfg, FakeClient(), env)
    assert any("28" in p for p in problems)
    paper_decisions(28)
    _, problems = diagnose(cfg, FakeClient(), env)
    assert not any("28" in p for p in problems)


def test_a_slow_ai_never_delays_stop_losses(tmp_path, ai, monkeypatch):
    import threading
    import time
    release = threading.Event()
    slow = ai["script"]

    def call(provider, system, payload, tool):
        release.wait(5)  # the AI takes its time
        return slow[tool["name"]], {}

    monkeypatch.setattr(claude, "call", call)
    ex = Exchange(krw=0)
    ai["script"] = {"propose_trades": propose(), "review_trades": review()}
    bot = ai_bot(tmp_path, ex)
    hold(bot, ex, "KRW-ETH", 1000.0, stop=ex.price * 0.95)
    ex.price *= 0.9
    t0 = time.time()
    bot.step(now(ex))  # the stop fires now, while the AIs are still thinking
    assert time.time() - t0 < 2 and "KRW-ETH" not in bot.state["positions"]
    release.set()


def paper_decisions(n):
    ai_trader.DAYS.parent.mkdir(exist_ok=True)
    ai_trader.DAYS.write_text(json.dumps([str(d.date()) for d in pd.date_range("2026-09-01", periods=n)]))


def test_the_run_loop_keeps_the_ai_off_real_money_until_28_paper_days(tmp_path, ai):
    """Also when "ai" is added (panel, autopilot) while the bot already runs live, past the start-up check."""
    from test_setup import FakeClient

    from tradebot.__main__ import _bots
    (tmp_path / "state" / "selected.json").write_text(json.dumps(
        {"generated_at": "2026-10-01T00:00:00+00:00", "sleeves": {"donchian": {"params": {}, "tradable": True}}}))
    cfg = config.Config(mode="live", strategies=["donchian", "ai"])

    def sleeves():
        return [(b.cfg.strategy, b.state["funded"]) for b in _bots(cfg, True, FakeClient(), None, 1_000_000)[1]]

    assert sleeves() == [("donchian", 500_000)]  # the ai's share stays unused
    paper_decisions(28)
    assert sleeves() == [("donchian", 500_000), ("ai", 500_000)]


def test_only_days_the_ais_really_decided_on_paper_count_toward_real_money(tmp_path, ai):
    ex = Exchange()
    ai["script"] = {"propose_trades": propose(), "review_trades": review()}
    decide(ai_bot(tmp_path, ex), now(ex))  # a real-money decision is not a paper result
    assert ai_trader.paper_days() == 0
    (tmp_path / "p").mkdir()
    decide(ai_bot(tmp_path / "p", ex, paper=True), now(ex))
    assert ai_trader.paper_days() == 1


def test_a_trade_closed_while_the_ais_think_keeps_its_record(tmp_path, ai):
    new_rule = {"op": "add", "rule_id": "", "text": "새 규칙", "why": "-"}

    def review_while_a_trade_closes():
        ai_trader.record("R1", 0.05)  # the main loop books a sale during the (slow) AI call
        return review(accept=[0])

    ai["script"] = {"propose_trades": propose(rules=[new_rule]), "review_trades": review_while_a_trade_closes}
    ai_trader.debate(["claude", "gpt"], 0, "bar", FACTS)
    rules = ai_trader.rulebook()
    assert next(r for r in rules if r["id"] == "R1")["trades"] == 1 and any(r["text"] == "새 규칙" for r in rules)


def test_a_broken_rulebook_never_loses_the_sale_from_the_trade_log(tmp_path, ai, monkeypatch):
    ex = Exchange(krw=0)
    bot = ai_bot(tmp_path, ex)
    hold(bot, ex, "KRW-BTC", 1000.0, stop=ex.price * 0.95)
    bot.state["positions"]["KRW-BTC"]["rule"] = "R1"

    def disk_full(rule, pnl):
        raise OSError("disk full")

    monkeypatch.setattr(ai_trader, "record", disk_full)
    ex.price *= 0.9
    bot.step(now(ex))
    assert bot.state["positions"] == {} and ",sell," in (tmp_path / "trades.csv").read_text(encoding="utf-8")


def test_if_the_reviewer_fails_sells_still_happen_and_buys_do_not(tmp_path, ai):
    ai["script"] = {"propose_trades": propose(("KRW-BTC", "buy", "R1"), ("KRW-ETH", "sell", "R2")),
                    "review_trades": RuntimeError("timeout")}
    assert ai_trader.debate(["claude", "gpt"], 0, "bar", FACTS) == ({}, {"KRW-ETH"})


def test_malformed_ai_output_is_ignored_not_trusted(tmp_path, ai):
    ai["script"] = {
        "propose_trades": {"decisions": ["junk", {"market": ["KRW-BTC"], "action": "buy"},
                                        {"market": "KRW-BTC", "action": "buy", "rule": "R1", "reason": "긴 이유" * 500}],
                           "rule_proposals": [{"op": "add", "text": "새 규칙"}, "junk"]},
        "review_trades": {"reviews": [{"market": "KRW-BTC", "agree": True}], "sell": {"KRW-ETH": 1},
                          "rule_votes": [{"index": True, "accept": True}], "note": None}}
    assert ai_trader.debate(["claude", "gpt"], 0, "bar", FACTS) == ({"KRW-BTC": "R1"}, set())
    assert not any(r["text"] == "새 규칙" for r in ai_trader.rulebook())  # True is not index 0
    assert len(ai_trader.JOURNAL.read_text(encoding="utf-8")) < 3000  # model text is stored truncated


def test_missing_candles_mean_no_ai_decision_there_and_no_unconfirmed_buy(tmp_path, ai, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY")  # one AI: the 3-of-4 confirmation must back its buys
    ex = Exchange()
    bot = ai_bot(tmp_path, ex, markets=("KRW-BTC", "KRW-ETH"))
    bot._refresh_candles(now(ex).timestamp())
    del bot.candles["KRW-BTC"]  # BTC, and with it the confirmation, failed to load
    ai["script"] = {"propose_trades": propose(("KRW-ETH", "buy", "R1"))}
    snap = SimpleNamespace(cfg=bot.cfg, candles=bot.candles, state={"positions": {}}, can_buy=True, paper=True)
    out = ai_trader.rows(snap, now(ex).floor("240min"), {"KRW-BTC": ex.price, "KRW-ETH": ex.price})
    assert "KRW-BTC" not in out and not out["KRW-ETH"]["enter"]
    assert list(ai["calls"][0][2]["markets"]) == ["KRW-ETH"]


def test_a_failed_ai_call_is_retried_once_a_few_minutes_later(tmp_path, ai):
    ex = Exchange()
    ai["script"] = {"propose_trades": RuntimeError("529 overloaded"), "review_trades": review(agree=["KRW-BTC"])}
    bot = ai_bot(tmp_path, ex)
    decide(bot, now(ex))
    decide(bot, now(ex) + pd.Timedelta(minutes=2))  # too soon
    assert len(ai["calls"]) == 1
    ai["script"]["propose_trades"] = propose(("KRW-BTC", "buy", "R1"))
    decide(bot, now(ex) + pd.Timedelta(minutes=6))
    assert ex.placed == [("buy", "KRW-BTC")]


def test_a_restart_in_the_same_bar_does_not_ask_the_ais_again(tmp_path, ai):
    ex = Exchange()
    ai["script"] = {"propose_trades": propose(), "review_trades": review()}
    decide(ai_bot(tmp_path, ex), now(ex))
    asked = len(ai["calls"])
    decide(ai_bot(tmp_path, ex), now(ex, 3))  # same ledger, same bar
    assert asked == 2 and len(ai["calls"]) == asked


def test_when_buying_is_paused_the_ais_are_not_asked_about_buys(tmp_path, ai):
    live.pause_entries("test")
    ex = Exchange()
    decide(ai_bot(tmp_path, ex), now(ex))
    assert ai["calls"] == []


def test_the_ais_are_asked_on_the_signal_for_the_bar_about_to_trade(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ex = Exchange()
    bot = ai_bot(tmp_path, ex)
    bot._refresh_candles(now(ex).timestamp())
    bar = now(ex).floor("240min")
    monkeypatch.setattr(ai_trader, "prepare", lambda name, df, params: df.assign(enter=df.index == bar, entry_stop=np.nan))
    assert ai_trader.candidates(bot, bar) == {"KRW-BTC"}


def test_a_decision_is_never_reused_in_the_next_bar(tmp_path, ai):
    ex = Exchange()
    ai["script"] = {"propose_trades": propose(("KRW-BTC", "buy", "R1")), "review_trades": review(agree=["KRW-BTC"])}
    bot = ai_bot(tmp_path, ex)
    nxt = now(ex).floor("240min") + pd.Timedelta(hours=4)
    decide(bot, nxt - pd.Timedelta(seconds=30))  # bought in the last seconds of a bar
    sold = bot.state["positions"].pop("KRW-BTC")  # and sold again
    bot.state["cash"] += sold["qty"] * sold["entry"]
    bot.step(nxt + pd.Timedelta(seconds=5))  # new bar, candles not refreshed yet
    assert ex.placed == [("buy", "KRW-BTC")]


def test_a_restart_with_live_coins_is_never_refused_for_the_ai_gate(tmp_path, monkeypatch):
    """The rule sleeves' open positions need their stops; the run loop holds the ai back by itself."""
    from test_setup import LIVE_KEYS, FakeClient

    from tradebot.__main__ import diagnose
    monkeypatch.chdir(tmp_path)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "live_donchian.json").write_text(json.dumps(
        {"cash": 0.0, "funded": 500_000, "positions": {"KRW-BTC": {"qty": 1.0, "entry": 100.0, "stop": 90.0}}}))
    env = {**LIVE_KEYS, "ANTHROPIC_API_KEY": "sk-ant-fake-0123456789abcdefghij"}
    cfg = config.Config(mode="live", budget_krw=1_000_000, strategies=["donchian", "ai"])
    ok, problems = diagnose(cfg, FakeClient(), env)
    assert not any("28" in p for p in problems) and any("28" in line for line in ok)


def test_stale_candles_mean_the_ais_wait(tmp_path, ai):
    ex = Exchange()
    ai["script"] = {"propose_trades": propose(("KRW-BTC", "buy", "R1")), "review_trades": review(agree=["KRW-BTC"])}
    decide(ai_bot(tmp_path, ex), now(ex) + pd.Timedelta(hours=12))  # Upbit's candles stopped 3 bars ago
    assert ai["calls"] == [] and ex.placed == []
