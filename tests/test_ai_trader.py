"""The "ai" sleeve: Claude and GPT decide its trades and check each other, inside the bot's hard limits."""
import json

import pandas as pd
import pytest
from test_live_exchange import Exchange, hold, make_bot, now

from tradebot import ai_trader, claude, live


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
        return answer, {"input_tokens": 1000, "output_tokens": 100}

    monkeypatch.setattr(claude, "call", call)
    return state


def ai_bot(tmp_path, ex):
    bot = make_bot(tmp_path, ex)
    bot.cfg.strategy = "ai"
    return bot


def midnight(ex, days=1):
    """A 00:xx UTC poll: the daily look at holdings (otherwise the AIs are only asked when a rule signals)."""
    return (ex.df.index[-1] + pd.Timedelta(days=days)).normalize() + pd.Timedelta(minutes=5)


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
    bot.step(now(ex))
    pos = bot.state["positions"]["KRW-BTC"]
    assert [c[:2] for c in ai["calls"]] == [(ai["calls"][0][0], "propose_trades"), (ai["calls"][1][0], "review_trades")]
    assert ai["calls"][0][0] != ai["calls"][1][0]  # proposer and reviewer are different AIs
    assert pos["rule"] == "R1" and pos["stop"] < pos["entry"]  # the 2-ATR stop comes from the bot, not the AI
    assert pos["qty"] * pos["entry"] <= 500_000 * 1.01  # never more than the sleeve's own money


def test_a_buy_the_reviewer_rejects_is_not_made(tmp_path, ai):
    ex = Exchange()
    ai["script"] = {"propose_trades": propose(("KRW-BTC", "buy", "R1")), "review_trades": review(agree=[])}
    bot = ai_bot(tmp_path, ex)
    bot.step(now(ex))
    assert bot.state["positions"] == {} and ex.placed == []


def test_either_ai_can_sell_because_selling_only_reduces_risk(tmp_path, ai):
    ex = Exchange(krw=0)
    bot = ai_bot(tmp_path, ex)
    hold(bot, ex, "KRW-BTC", 1000.0, stop=ex.price * 0.5)
    bot.state["positions"]["KRW-BTC"]["entry_time"] = "2020-01-01T00:00:00+00:00"
    ai["script"] = {"propose_trades": propose(("KRW-BTC", "hold", "R2")), "review_trades": review(sell=["KRW-BTC"])}
    bot.step(midnight(ex))
    assert bot.state["positions"] == {}


def test_with_one_ai_the_bots_own_confirmation_is_the_second_opinion(tmp_path, ai, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY")
    ex = Exchange()
    ai["script"] = {"propose_trades": propose(("KRW-BTC", "buy", "R1"))}
    monkeypatch.setattr(ai_trader, "confirmed_now", lambda hist, btc: False)
    bot = ai_bot(tmp_path, ex)
    bot.step(now(ex))
    assert ex.placed == [] and [c[1] for c in ai["calls"]] == ["propose_trades"]


def test_a_rule_is_adopted_only_when_the_other_ai_accepts_and_its_record_is_kept(tmp_path, ai):
    ex = Exchange()
    new_rule = {"op": "add", "rule_id": "", "text": "거래량 2배 이상 돌파만 산다", "why": "가짜 돌파 줄이기"}
    ai["script"] = {"propose_trades": propose(("KRW-BTC", "buy", "R1"), rules=[new_rule]),
                    "review_trades": review(agree=["KRW-BTC"], accept=[0])}
    bot = ai_bot(tmp_path, ex)
    bot.step(now(ex))
    rules = json.loads(ai_trader.RULEBOOK.read_text(encoding="utf-8"))
    assert any(r["text"] == new_rule["text"] for r in rules)
    bot.state["positions"]["KRW-BTC"]["entry_time"] = "2020-01-01T00:00:00+00:00"  # simulated clock vs wall clock
    ex.price *= 1.10  # the R1 trade is closed by a sell decision and its record lands on R1
    ai["script"] = {"propose_trades": propose(("KRW-BTC", "sell", "R1")), "review_trades": review()}
    bot.step(midnight(ex))
    r1 = next(r for r in json.loads(ai_trader.RULEBOOK.read_text(encoding="utf-8")) if r["id"] == "R1")
    assert r1["trades"] == 1 and r1["wins"] == 1 and r1["pnl"] > 0


def test_no_decision_point_means_no_call_and_no_tokens(tmp_path, ai, monkeypatch):
    monkeypatch.setattr(ai_trader, "candidates", lambda bot, bar: set())
    ex = Exchange()
    bot = ai_bot(tmp_path, ex)
    bot.step(now(ex))
    assert ai["calls"] == []


def test_an_ai_outage_means_no_new_buys_but_stops_still_work(tmp_path, ai):
    ex = Exchange(krw=0)
    ai["script"] = {"propose_trades": RuntimeError("529 overloaded"), "review_trades": review()}
    bot = ai_bot(tmp_path, ex)
    hold(bot, ex, "KRW-ETH", 1000.0, stop=ex.price * 0.95)
    ex.price *= 0.9
    bot.step(now(ex))
    assert "KRW-ETH" not in bot.state["positions"] and ("buy", "KRW-BTC") not in ex.placed


def test_the_ais_never_see_a_secret(tmp_path, ai):
    ex = Exchange()
    ai["script"] = {"propose_trades": propose(("KRW-BTC", "buy", "R1")), "review_trades": review(agree=["KRW-BTC"])}
    ai_bot(tmp_path, ex).step(now(ex))
    sent = json.dumps([c[2:] for c in ai["calls"]], ensure_ascii=False, default=str)
    assert "upbit-secret-never-sent-0123456789" not in sent and "sk-ant-fake" not in sent


def test_real_money_for_the_ai_needs_28_days_of_paper_results(tmp_path, monkeypatch):
    from tradebot import config
    from tradebot.__main__ import diagnose
    from test_setup import LIVE_KEYS, FakeClient
    monkeypatch.chdir(tmp_path)
    env = {**LIVE_KEYS, "ANTHROPIC_API_KEY": "sk-ant-fake-0123456789abcdefghij"}
    cfg = config.Config(mode="live", budget_krw=1_000_000, strategies=["donchian", "ai"])
    _, problems = diagnose(cfg, FakeClient(), env)
    assert any("28" in p for p in problems)
    (tmp_path / "logs").mkdir()
    days = pd.date_range("2026-09-01", periods=28, freq="D").strftime("%Y-%m-%d")
    pd.DataFrame({"date": days, "strategy": "ai", "equity": 1.0, "funded": 1.0}).to_csv(tmp_path / "logs" / "paper_equity.csv", index=False)
    _, problems = diagnose(cfg, FakeClient(), env)
    assert not any("28" in p for p in problems)
