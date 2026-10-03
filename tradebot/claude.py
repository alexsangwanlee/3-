"""An AI (Claude, or GPT when only an OpenAI key is set) reviews the bot once a week and can act, within limits.

It reads only the bot's own numbers: the plan report, recent trades, real costs, the weekly self-review and a
little market context. It explains them in Korean and picks one action. It never places orders and never sees a key.

    keep                  nothing to do (the default)
    pause_entries         stop new buys; open positions keep their stops and exits   } safer: autopilot may run these
    apply_recommendation  use the strategies the weekly self-review already validated }
    resume_entries        allow new buys again: riskier, so it always waits for the user
"""
import hashlib
import json
import logging
import os
from pathlib import Path

import pandas as pd
import requests

from . import config, live
from .live import notify
from .report import flow_adjusted_returns, report

log = logging.getLogger("tradebot")
API = "https://api.anthropic.com/v1/messages"
MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")
GPT_API = "https://api.openai.com/v1/responses"
GPT_MODEL = os.environ.get("OPENAI_MODEL", "gpt-6-astra")
REVIEW = Path("state/claude_review.json")
KNOWLEDGE = Path("results/knowledge.json")  # every idea already tested, so Claude never spends tokens rediscovering it
ACTIONS = {"keep": "그대로 유지", "pause_entries": "신규 매수 멈춤", "apply_recommendation": "검증된 전략 추천 적용",
           "resume_entries": "신규 매수 다시 허용"}
AUTO = {"pause_entries", "apply_recommendation"}  # what autopilot may do without asking: never adds risk

SYSTEM = """You review a small long-only trend-following bot on Upbit (Korean crypto spot exchange) for its owner,
a non-programmer. You receive JSON facts the bot produced. Reply only through the weekly_review tool, in Korean,
in plain short sentences.

Rules:
- Use only the facts given. Never predict prices.
- The rules were validated out of sample: about half of all months lose money and that is normal. Do not react to a
  bad week or month that the report calls 정상 범위.
- Pick exactly one action:
  keep: the default.
  pause_entries: only if at least one holds, and name which: the report says 점검 필요; the bot logged repeated
    errors; real slippage is far above the assumption (over 0.3% per side); a ledger looks inconsistent
    (for example negative cash); the kill switch fired.
  apply_recommendation: only if selection.self_review.recommend is not null (it passed the bot's own
    out-of-sample test). Never propose strategies yourself.
  resume_entries: only if entries are paused and the reason no longer holds.
- Never suggest raising the budget, adding leverage or loosening risk limits. That is the owner's decision under
  results/plan.md.
- user_checks: concrete things the owner should look at, if any."""

TOOL = {
    "name": "weekly_review",
    "description": "The weekly review of the trading bot, shown to its owner and possibly acted on.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "3-5 Korean sentences: how the bot did and why"},
            "risks": {"type": "array", "items": {"type": "string"}, "description": "anything off-plan, in Korean"},
            "action": {"type": "string", "enum": list(ACTIONS)},
            "reason": {"type": "string", "description": "which rule justifies the action, in Korean"},
            "user_checks": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["summary", "risks", "action", "reason", "user_checks"],
        "additionalProperties": False,  # OpenAI strict mode needs it; Claude accepts it
    },
}


def _csv_tail(path: str, n: int) -> list[dict]:
    p = Path(path)
    return pd.read_csv(p).tail(n).fillna("").to_dict("records") if p.exists() else []


def market_context() -> dict:
    """Best effort: Fear & Greed and the Upbit USDT premium (a kimchi-premium proxy). Missing is fine."""
    out = {}
    try:
        out["fear_greed"] = int(requests.get("https://api.alternative.me/fng/", timeout=10).json()["data"][0]["value"])
        usdt = float(requests.get("https://api.upbit.com/v1/ticker", params={"markets": "KRW-USDT"},
                                  timeout=10).json()[0]["trade_price"])
        fx = requests.get("https://api.frankfurter.dev/v1/latest", params={"from": "USD", "to": "KRW"},
                          timeout=10).json()["rates"]["KRW"]
        out["usdt_premium"] = round(usdt / fx - 1, 4)
    except Exception:
        log.info("market context unavailable")
    return out


def facts(cfg: config.Config) -> dict:
    """Everything Claude may see. Built from files the bot writes; no environment variable is read here."""
    eq_path = f"logs/{cfg.mode}_equity.csv"
    sel = config.selection()
    recent = {}
    if Path(eq_path).exists():
        r = flow_adjusted_returns(pd.read_csv(eq_path))
        recent = {f"last_{n}d": round(float((1 + r.iloc[-n:]).prod() - 1), 4) for n in (7, 30, 90) if len(r) >= n}
    tail = Path("logs/bot.log").read_text(encoding="utf-8", errors="replace").splitlines()[-2000:] \
        if Path("logs/bot.log").exists() else []
    problems = [line[20:200] for line in tail if " WARNING " in line or " ERROR " in line]
    return {
        "mode": cfg.mode, "budget_krw": cfg.budget_krw if cfg.mode == "live" else cfg.paper_krw,
        "strategies": cfg.strategies, "markets": cfg.markets,
        "report": report(cfg.mode, eq_path), "returns": recent,
        "recent_trades": _csv_tail(f"logs/{cfg.mode}_trades.csv", 30),
        "selection": {"generated_at": sel.get("generated_at"), "costs_used": sel.get("costs_used"),
                      "self_review": sel.get("self_review"),
                      "sleeves": {k: {"tradable": v.get("tradable"), "params": v.get("params")}
                                  for k, v in sel.get("sleeves", {}).items()}},
        "entries_paused": live.pause_info(), "kill_switch": _halted(cfg.mode),
        "log_problems": {"count": len(problems), "last": problems[-8:]},
        "market": market_context(),
    }


def _halted(mode: str) -> list[str]:
    return [name for name, st in live.ledgers(mode).items() if (st.get("guard") or {}).get("halted")]


def _system() -> list[dict]:
    """The fixed part of the prompt. The knowledge digest sits last with a cache mark, so repeated calls within the
    cache window are billed at the cached rate, and Claude is told what is settled instead of re-deriving it."""
    known = KNOWLEDGE.read_text(encoding="utf-8") if KNOWLEDGE.exists() else "{}"
    return [{"type": "text", "text": SYSTEM},
            {"type": "text", "text": "Already tested on this bot (do not re-propose rejected ideas):\n" + known,
             "cache_control": {"type": "ephemeral"}}]


def _clean(got: dict) -> dict:
    """Model output is untrusted input: keep only known fields, known actions, bounded text."""
    text = lambda v, n=600: str(v)[:n]  # noqa: E731
    items = lambda v: [text(x, 300) for x in (v if isinstance(v, list) else [])][:6]  # noqa: E731
    return {"summary": text(got.get("summary", "")), "risks": items(got.get("risks")),
            "action": got.get("action") if got.get("action") in ACTIONS else "keep",
            "reason": text(got.get("reason", ""), 400), "user_checks": items(got.get("user_checks"))}


def _usage(u: dict, cached: int = 0) -> dict:
    return {"input_tokens": int(u.get("input_tokens", 0)), "output_tokens": int(u.get("output_tokens", 0)),
            "cache_read_input_tokens": int(u.get("cache_read_input_tokens", cached))}


def call(provider: str, system: list[dict], payload: dict, tool: dict) -> tuple[dict, dict]:
    """One structured answer from Claude ("claude") or GPT ("gpt"): (the tool/schema object, token usage).
    `system` blocks come first so both providers' prompt caches cover them. Keys are read here and nowhere else."""
    content = json.dumps(payload, ensure_ascii=False, default=str)
    if provider == "claude":
        r = requests.post(API, headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01",
                                        "content-type": "application/json"},
                          json={"model": MODEL, "max_tokens": 3000, "system": system, "tools": [tool],
                                "tool_choice": {"type": "tool", "name": tool["name"]},
                                "messages": [{"role": "user", "content": content}]}, timeout=120)
        r.raise_for_status()
        body = r.json()
        return next(b["input"] for b in body["content"] if b.get("type") == "tool_use"), _usage(body.get("usage") or {})
    r = requests.post(GPT_API, headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}",
                                        "content-type": "application/json"},
                      json={"model": GPT_MODEL, "instructions": "\n\n".join(b["text"] for b in system), "input": content,
                            "text": {"format": {"type": "json_schema", "name": tool["name"],
                                                "schema": tool["input_schema"], "strict": True}},
                            "reasoning": {"effort": "low"}, "max_output_tokens": 6000}, timeout=120)
    r.raise_for_status()
    body = r.json()
    out = next(c["text"] for item in body.get("output", []) if item.get("type") == "message"
               for c in item.get("content", []) if c.get("type") == "output_text")
    u = body.get("usage") or {}
    return json.loads(out), _usage(u, (u.get("input_tokens_details") or {}).get("cached_tokens", 0))


def providers() -> list[str]:
    """The AIs with a key, Claude first."""
    return [p for p, k in (("claude", "ANTHROPIC_API_KEY"), ("gpt", "OPENAI_API_KEY")) if os.environ.get(k)]


def _possible(action: str) -> bool:
    if action == "apply_recommendation":
        return bool((config.selection().get("self_review") or {}).get("recommend"))
    if action == "resume_entries":
        return live.entries_paused()
    return action != "keep"


def apply(action: str, reason: str) -> None:
    if action == "pause_entries":
        live.pause_entries(f"AI 검토: {reason}")
    elif action == "resume_entries":
        live.PAUSE_FILE.unlink(missing_ok=True)
    elif action == "apply_recommendation":
        from .ui import save  # same validated path as the panel's button

        save({"strategies": config.selection()["self_review"]["recommend"]})


def run(cfg: config.Config) -> dict | None:
    """Ask Claude, act if allowed, save and notify. None when no key is set or the API is unavailable."""
    ais = providers()
    if not ais:
        return None
    payload = facts(cfg)
    digest = hashlib.sha256(json.dumps({k: v for k, v in payload.items() if k != "market"}, sort_keys=True,
                                       ensure_ascii=False, default=str).encode()).hexdigest()
    last = json.loads(REVIEW.read_text(encoding="utf-8")) if REVIEW.exists() else {}
    if last.get("digest") == digest:  # nothing the bot knows has changed: no call, no tokens
        log.info("AI review skipped: nothing changed since %s", last.get("at"))
        return {**last, "skipped": True}
    try:
        got, usage = call(ais[0], _system(), payload, TOOL)
        rv = _clean(got)
    except Exception as e:  # never let the review break trading
        log.warning("AI review failed: %s", type(e).__name__)
        return None
    if not _possible(rv["action"]):
        rv["action"] = "keep"
    status = "none"
    if rv["action"] != "keep":
        status = "waiting"
        if cfg.claude_autopilot and rv["action"] in AUTO:
            apply(rv["action"], rv["reason"])
            status = "applied"
    out = {"at": pd.Timestamp.now(tz="UTC").isoformat(), "model": MODEL if ais[0] == "claude" else GPT_MODEL, "review": rv,
           "status": status,
           "usage": usage, "digest": digest}
    REVIEW.parent.mkdir(exist_ok=True)
    REVIEW.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    label = ACTIONS[rv["action"]] + {"applied": " (자동 실행함)", "waiting": " (제어판에서 승인하면 실행)", "none": ""}[status]
    notify(f"[tradebot] AI 주간 검토 ({out['model']})\n{rv['summary']}\n조치: {label}")
    return out


def approve(cfg: config.Config) -> str:
    """The user approved the waiting action in the control panel."""
    out = json.loads(REVIEW.read_text(encoding="utf-8"))
    if out["status"] != "waiting" or not _possible(out["review"]["action"]):
        return out["status"]
    apply(out["review"]["action"], out["review"]["reason"])
    out["status"] = "applied"
    REVIEW.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return "applied"
