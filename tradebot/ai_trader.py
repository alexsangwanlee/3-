"""The "ai" sleeve: Claude and/or GPT decide its trades, check each other, and keep a shared rulebook.

At a decision point (a rule strategy just signalled an entry somewhere, or once a day while holding), one AI proposes
buy / sell / hold per market and names the rule behind it; the other AI reviews. The proposer alternates.
- A buy needs both AIs. With one AI, the bot's 3-of-4 confirmation is the second opinion.
- A sell needs either one: selling only reduces risk.
- A rule one AI proposes joins the rulebook only if the other accepts. Each rule keeps its live record.

The AIs choose what to trade, never how much or where the stop is. Those stay in code: the risk-based size, a 2-ATR
stop, the giveback guard, the daily loss limit, the kill switch and the sleeve's own budget. If the AI cannot be
reached, the sleeve makes no new buys and its stops keep working. Real money only after 28 days of paper results
(an LLM has seen past prices in training, so a backtest of its decisions would be meaningless).
"""
import json
import logging
import threading
from pathlib import Path

import numpy as np
import pandas as pd

from . import claude, config
from .data import regularize
from .live import write_json as _write
from .strategies import atr, confirmed, ema, prepare

log = logging.getLogger("tradebot")
RULEBOOK = Path("state/ai_rulebook.json")
JOURNAL = Path("state/ai_journal.json")  # recent decisions, for the panel and the AIs' own memory
DAYS = Path("state/ai_paper_days.json")  # days the AIs decided on paper: the real-money gate
LOCK = threading.Lock()  # the rulebook is updated by the bot loop (trade results) and the AI thread (new rules)
STOP_ATR, PAPER_DAYS, MAX_RULES, KEEP = 2.0, 28, 12, 30
SEED = [  # what this bot has already validated (results/knowledge.json); the AIs start from here
    "추세 먼저: 코인이 200봉 이평 위에 있고 BTC도 200봉 이평 위일 때를 우선한다 (검증된 3-of-4 확인 규칙).",
    "추격 매수 금지: 50봉 이평보다 3 ATR 넘게 오른 코인은 사지 않는다.",
    "거래량 확인: 30일 평균보다 거래량이 적은 돌파는 의심한다.",
    "수익은 길게: 올랐다는 이유만으로 팔지 않고, 추세가 꺾일 때 판다.",
    "봇의 2 ATR 손절은 최종이다. 같은 흐름에서 손절 직후 바로 다시 사지 않는다.",
]

SYSTEM = """You and a second AI jointly trade the "ai" sub-account of a long-only spot bot on Upbit (Korean exchange),
4-hour bars, KRW markets. You get compact numbers per market, computed from completed bars only, and the shared
rulebook with each rule's live record (trades, wins, total P&L).
- Decide buy / sell / hold per market. Name the rule (id) you rely on. Buy only markets not held; sell only held ones.
- Position size, the stop-loss (2 ATR), daily loss limit and kill switch are fixed by the bot; you cannot change them.
- Most of the time the right answer is hold. Fees are 0.05% per side plus slippage.
- Prefer rules with a good live record; propose removing rules whose record is bad after enough trades.
- Propose at most 2 rule changes, and only with a concrete reason from the numbers.
- When reviewing the other AI: agree only with buys you would make yourself; you may add sells for held markets.
- Write reasons in Korean, one short sentence each."""

_DECISION = {"type": "object", "additionalProperties": False, "required": ["market", "action", "rule", "reason"],
             "properties": {"market": {"type": "string"}, "action": {"type": "string", "enum": ["buy", "sell", "hold"]},
                            "rule": {"type": "string"}, "reason": {"type": "string"}}}
_RULE = {"type": "object", "additionalProperties": False, "required": ["op", "rule_id", "text", "why"],
         "properties": {"op": {"type": "string", "enum": ["add", "remove"]}, "rule_id": {"type": "string"},
                        "text": {"type": "string"}, "why": {"type": "string"}}}
PROPOSE = {"name": "propose_trades", "description": "Trade decisions for the ai sub-account and optional rule changes.",
           "input_schema": {"type": "object", "additionalProperties": False, "required": ["decisions", "rule_proposals"],
                            "properties": {"decisions": {"type": "array", "items": _DECISION},
                                           "rule_proposals": {"type": "array", "items": _RULE}}}}
REVIEW = {"name": "review_trades", "description": "Review of the other AI's proposal.",
          "input_schema": {"type": "object", "additionalProperties": False, "required": ["reviews", "sell", "rule_votes", "note"],
                           "properties": {
                               "reviews": {"type": "array", "items": {
                                   "type": "object", "additionalProperties": False, "required": ["market", "agree", "reason"],
                                   "properties": {"market": {"type": "string"}, "agree": {"type": "boolean"},
                                                  "reason": {"type": "string"}}}},
                               "sell": {"type": "array", "items": {"type": "string"}},
                               "rule_votes": {"type": "array", "items": {
                                   "type": "object", "additionalProperties": False, "required": ["index", "accept", "reason"],
                                   "properties": {"index": {"type": "integer"}, "accept": {"type": "boolean"},
                                                  "reason": {"type": "string"}}}},
                               "note": {"type": "string"}}}}


def _read(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def rulebook() -> list[dict]:
    return _read(RULEBOOK, [{"id": f"R{i + 1}", "text": t, "by": "seed", "trades": 0, "wins": 0, "pnl": 0.0}
                            for i, t in enumerate(SEED)])


def record(rule_id: str, pnl: float) -> None:
    """A closed ai-sleeve trade: credit its result to the rule that opened it."""
    with LOCK:
        rules = rulebook()
        for r in rules:
            if r["id"] == rule_id:
                r["trades"] += 1
                r["wins"] += int(pnl > 0)
                r["pnl"] = round(r["pnl"] + pnl, 4)
        _write(RULEBOOK, rules)


def _dicts(obj, key: str) -> list[dict]:
    """Model output is untrusted: the dict items of a list field, nothing else."""
    v = obj.get(key) if isinstance(obj, dict) else None
    return [d for d in v if isinstance(d, dict)] if isinstance(v, list) else []


def _text(v, n: int = 200) -> str:
    return v[:n] if isinstance(v, str) else ""


def _change_rules(rules: list[dict], accepted: list[dict], author: str) -> list[dict]:
    for p in accepted:
        if p["op"] == "remove":
            rules = [r for r in rules if r["id"] != p["rule_id"]]
        elif p["op"] == "add" and p["text"].strip() and len(rules) < MAX_RULES:
            n = max([int(r["id"][1:]) for r in rules if r["id"][1:].isdigit()] + [0]) + 1
            rules.append({"id": f"R{n}", "text": p["text"].strip()[:200], "by": author, "trades": 0, "wins": 0, "pnl": 0.0})
    return rules


def confirmed_now(hist: pd.DataFrame, btc: pd.Series | None) -> bool:
    """The rule system's 3-of-4 confirmation for the bar after `hist`. No BTC candles: not confirmed."""
    if btc is None:
        return False
    nxt = hist.index[-1] + (hist.index[-1] - hist.index[-2])
    ext = pd.concat([hist, hist.iloc[[-1]].set_axis([nxt])])
    return bool(confirmed(ext, btc)[nxt])


def candidates(bot, bar) -> set[str]:
    """Markets where a rule strategy signals an entry for `bar` (or broke out during the last completed bar):
    when the AIs are asked at all."""
    sel = config.selection().get("sleeves", {})
    out = set()
    for m in bot.cfg.markets:
        if m not in bot.candles:
            continue
        hist = regularize(bot.candles[m][bot.candles[m].index < bar], bot.cfg.timeframe)
        ext = pd.concat([hist, hist.iloc[[-1]].set_axis([bar])])  # the row the bot would trade now
        for name in ("donchian", "ema_cross"):
            p = prepare(name, ext, (sel.get(name) or {}).get("params") or {})
            done, now = p.iloc[-2], p.iloc[-1]
            if now["enter"] or (np.isfinite(done["entry_stop"]) and done["high"] >= done["entry_stop"]):
                out.add(m)
    return out


def _facts(m, hist, btc, price, pos) -> dict:
    c, d = hist["close"], int(pd.Timedelta(days=1) / (hist.index[1] - hist.index[0]))
    r = lambda x: round(float(x), 4)  # noqa: E731
    ago = lambda k: r(c.iloc[-1] / c.iloc[max(-len(c), -1 - k)] - 1)  # noqa: E731
    out = {"price": price, "ret_1d": ago(d), "ret_7d": ago(7 * d), "ret_30d": ago(30 * d),
           "vs_ema50": r(c.iloc[-1] / ema(c, 50).iloc[-1] - 1),
           "vs_ema200": r(c.iloc[-1] / ema(c, 200).iloc[-1] - 1), "atr_pct": r(atr(hist).iloc[-1] / c.iloc[-1]),
           "volume_vs_30d": r(hist["volume"].iloc[-d:].sum() / (hist["volume"].iloc[-30 * d:].sum() / 30)),
           "confirmation_3_of_4": confirmed_now(hist, btc), "held": None}
    if pos:
        out["held"] = {"entry": pos["entry"], "pnl": r(price / pos["entry"] - 1), "stop": pos["stop"],
                       "since": pos["entry_time"], "rule": pos.get("rule")}
    return out


def rows(bot, bar, prices: dict) -> dict:
    """Signal rows for the ai sleeve, shaped like strategies.prepare() rows so the bot trades them the same way.
    `bot` is the Bot's snapshot: cfg, candles, positions, can_buy (entries allowed now) and paper."""
    btc = bot.candles["KRW-BTC"]["close"] if "KRW-BTC" in bot.candles else None
    positions = {m: p for m, p in bot.state["positions"].items() if not p.get("dust")}
    out, facts = {}, {}
    for m in bot.cfg.markets:
        if m not in bot.candles or bot.candles[m].index[-1] < bar - pd.Timedelta(minutes=bot.cfg.timeframe):
            continue  # candles failed to load or stopped updating: nothing current to judge it on
        hist = regularize(bot.candles[m][bot.candles[m].index < bar], bot.cfg.timeframe)
        daily = hist["close"].resample("D").last().pct_change(fill_method=None)
        out[m] = {"enter": False, "exit": False, "entry_stop": np.nan, "size": 1.0, "rule": "",
                  "stop_dist": STOP_ATR * float(atr(hist).iloc[-1]), "vol": float(daily.rolling(20).std().iloc[-1])}
        if m in prices and (bot.can_buy or m in positions):  # can't buy now: only holdings are worth asking about
            facts[m] = _facts(m, hist, btc, prices[m], positions.get(m))
    ais = claude.providers()
    asked = (bot.can_buy and candidates(bot, bar) - set(positions)) or (positions and bar.hour == 0)
    if not ais or not asked or not facts:  # nothing to decide: no call, no tokens
        return out
    buys, sells = debate(ais, int(bar.timestamp() // (4 * 3600)), str(bar), facts)
    if bot.paper:
        with LOCK:
            _write(DAYS, sorted(set(_read(DAYS, [])) | {str(bar.date())}))
    for m, rule in buys.items():
        out[m].update(enter=True, rule=rule)
    for m in sells:
        out[m]["exit"] = True
    return out


def debate(ais: list[str], turn: int, bar: str, facts: dict) -> tuple[dict, set]:
    """One proposal, one review. Returns (market -> rule for buys both accept, markets either wants sold)."""
    rules = rulebook()
    # no cache mark: calls are hours apart, so a cache write would only cost more
    system = [{"type": "text", "text": SYSTEM},
              {"type": "text", "text": "Shared rulebook:\n" + json.dumps(rules, ensure_ascii=False) + "\nRecent decisions:\n"
               + json.dumps(_read(JOURNAL, [])[-5:], ensure_ascii=False)}]
    proposer = ais[turn % len(ais)]
    reviewer = next((a for a in ais if a != proposer), None)
    prop, usage = claude.call(proposer, system, {"bar": bar, "markets": facts}, PROPOSE)
    # model output is untrusted: only known markets and actions, buys of unheld and sells of held markets
    decisions = [{"market": d["market"], "action": d["action"], "rule": _text(d.get("rule"), 12),
                  "reason": _text(d.get("reason"))} for d in _dicts(prop, "decisions")
                 if isinstance(d.get("market"), str) and d["market"] in facts and d.get("action") in ("buy", "sell")]
    buys = {d["market"]: d["rule"] for d in decisions if d["action"] == "buy" and not facts[d["market"]]["held"]}
    sells = {d["market"] for d in decisions if d["action"] == "sell" and facts[d["market"]]["held"]}
    proposals = [{"op": p["op"], "rule_id": _text(p.get("rule_id"), 12), "text": _text(p.get("text")),
                  "why": _text(p.get("why"))} for p in _dicts(prop, "rule_proposals") if p.get("op") in ("add", "remove")][:2]
    entry = {"bar": bar, "proposer": proposer, "reviewer": reviewer or "3-of-4 rule", "proposed": decisions,
             "rule_proposals": proposals, "usage": [usage]}
    if reviewer:
        try:
            rev, usage2 = claude.call(reviewer, system, {"bar": bar, "markets": facts, "proposal": {
                "decisions": decisions, "rule_proposals": proposals}}, REVIEW)
        except Exception as e:  # no second opinion: no buys, but a sell only reduces risk
            log.warning("ai: %s review failed (%s); proposed sells only", reviewer, type(e).__name__)
            rev, usage2, buys = {}, {}, {}
        reviews = [{"market": _text(r.get("market"), 20), "agree": r.get("agree") is True, "reason": _text(r.get("reason"))}
                   for r in _dicts(rev, "reviews")]
        buys = {m: rule for m, rule in buys.items() if m in {r["market"] for r in reviews if r["agree"]}}
        sold = rev.get("sell") if isinstance(rev.get("sell"), list) else []
        sells |= {m for m in sold if isinstance(m, str) and m in facts and facts[m]["held"]}
        accepted = [proposals[v["index"]] for v in _dicts(rev, "rule_votes")
                    if v.get("accept") is True and type(v.get("index")) is int and 0 <= v["index"] < len(proposals)]
        if accepted:
            with LOCK:  # re-read: trade results may have landed while the AIs were thinking
                _write(RULEBOOK, _change_rules(rulebook(), accepted, proposer))
        entry.update(review=reviews, note=_text(rev.get("note"), 300), rules_adopted=accepted)
        entry["usage"].append(usage2)
    else:  # one AI: the bot's validated 3-of-4 confirmation is the second opinion on buys
        buys = {m: rule for m, rule in buys.items() if facts[m]["confirmation_3_of_4"]}
    entry.update(bought=sorted(buys), sold=sorted(sells))
    with LOCK:
        _write(JOURNAL, (_read(JOURNAL, []) + [entry])[-KEEP:])
    log.info("AI 판단 %s (%s 제안, %s 검토): 매수 %s, 매도 %s", bar, proposer, entry["reviewer"], sorted(buys), sorted(sells))
    return buys, sells


def paper_days() -> int:
    """Days the AIs actually decided on paper (asked and answered): the gate for real money."""
    return len(_read(DAYS, []))
