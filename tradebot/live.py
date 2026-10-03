"""Paper / live trading loop. Uses the same strategy columns and risk rules as the backtester."""
import csv
import json
import logging
import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import requests

from .data import regularize, to_frame
from .risk import Costs, DailyGuard, Risk, inverse_vol_weights, order_value
from .strategies import prepare
from .upbit import UpbitClient

log = logging.getLogger("tradebot")
MIN_ORDER_KRW = 5000


def notify(text: str) -> None:
    """Telegram message when TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are set. Never raises."""
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat, "text": text}, timeout=10)
    except Exception:
        log.warning("telegram notify failed")  # no exception text: it contains the URL, i.e. the token


class PaperBroker:
    """Simulated fills at the current price with fee and slippage."""

    def __init__(self, state: dict, costs: Costs):
        self.s, self.costs = state, costs

    def cash(self) -> float:
        return self.s["cash"]

    def holdings(self) -> dict[str, float]:
        return {m: q for m, q in self.s["holdings"].items() if q > 0}

    def buy(self, market: str, krw: float, price: float) -> tuple[float, float]:
        fill = price * (1 + self.costs.slippage)
        qty = krw / fill
        self.s["cash"] -= krw * (1 + self.costs.fee)
        self.s["holdings"][market] = self.s["holdings"].get(market, 0.0) + qty
        return qty, fill

    def sell(self, market: str, qty: float, price: float) -> float:
        fill = price * (1 - self.costs.slippage)
        self.s["cash"] += qty * fill * (1 - self.costs.fee)
        self.s["holdings"][market] = 0.0
        return fill


class UpbitBroker:
    """Real market orders on Upbit."""

    def __init__(self, client: UpbitClient):
        self.client = client

    def cash(self) -> float:
        return sum(float(a["balance"]) for a in self.client.accounts() if a["currency"] == "KRW")

    def holdings(self) -> dict[str, float]:
        return {f"KRW-{a['currency']}": float(a["balance"]) for a in self.client.accounts()
                if a["currency"] != "KRW" and float(a["balance"]) > 0}

    def _filled(self, order_uuid: str) -> tuple[float, float]:
        for _ in range(20):
            o = self.client.order(order_uuid)
            if o["state"] in ("done", "cancel"):
                vol = sum(float(t["volume"]) for t in o.get("trades", []))
                funds = sum(float(t["funds"]) for t in o.get("trades", []))
                return vol, (funds / vol if vol else 0.0)
            time.sleep(0.5)
        raise RuntimeError(f"order {order_uuid} not filled")

    def buy(self, market: str, krw: float, price: float) -> tuple[float, float]:
        return self._filled(self.client.buy_market(market, krw)["uuid"])

    def sell(self, market: str, qty: float, price: float) -> float:
        return self._filled(self.client.sell_market(market, qty)["uuid"])[1] or price


@dataclass
class BotConfig:
    markets: list[str]
    strategy: str
    params: dict
    risk: Risk = field(default_factory=Risk)
    costs: Costs = field(default_factory=Costs)
    timeframe: int = 60
    history_bars: int = 1000  # enough warm-up for the slowest indicator
    poll_seconds: int = 10
    state_path: str = "state/bot_state.json"
    trades_path: str = "logs/trades.csv"
    budget_krw: float = 0       # most KRW the bot may use; profits above it are left alone (0 = everything)
    allow_entries: bool = True  # False = optimize found nothing worth trading: only manage open positions


class Bot:
    def __init__(self, cfg: BotConfig, client: UpbitClient, broker=None, paper_krw: float = 1_000_000):
        self.cfg, self.client = cfg, client
        self.state = self._load_state(paper_krw)
        self.broker = broker or PaperBroker(self.state["paper"], cfg.costs)
        g = self.state.get("guard") or DailyGuard.from_risk(cfg.risk).to_dict()
        self.guard = DailyGuard(**{**g, "daily_target": cfg.risk.daily_target,
                                   "daily_loss_limit": cfg.risk.daily_loss_limit,
                                   "max_drawdown": cfg.risk.max_drawdown})
        self.candles: dict[str, pd.DataFrame] = {}
        self.last_refresh = 0.0

    # -- persistence -----------------------------------------------------
    def _load_state(self, paper_krw: float) -> dict:
        p = Path(self.cfg.state_path)
        if p.exists():
            return json.loads(p.read_text())
        return {"positions": {}, "last_entry_day": {}, "last_enter_bar": {}, "guard": None,
                "paper": {"cash": paper_krw, "holdings": {}}}

    def save(self) -> None:
        self.state["guard"] = self.guard.to_dict()
        p = Path(self.cfg.state_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.state, indent=2, default=str))

    def _log_trade(self, **row) -> None:
        p = Path(self.cfg.trades_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        new = not p.exists()
        with p.open("a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(row))
            if new:
                w.writeheader()
            w.writerow(row)

    # -- market data -----------------------------------------------------
    def _refresh_candles(self, now: float) -> None:
        if now - self.last_refresh < 60 and self.candles:
            return
        for m in self.cfg.markets:
            if m in self.candles:  # only the newest page is needed after the first load
                df = pd.concat([self.candles[m], to_frame(self.client.candles(m, self.cfg.timeframe, count=200))])
                self.candles[m] = df[~df.index.duplicated(keep="last")].iloc[-self.cfg.history_bars:]
                continue
            rows, to = [], None
            while len(rows) < self.cfg.history_bars:
                page = self.client.candles(m, self.cfg.timeframe, to=to, count=200)
                if not page:
                    break
                rows += page
                to = page[-1]["candle_date_time_utc"] + "Z"
            self.candles[m] = to_frame(rows)
        self.last_refresh = now

    def _signal_row(self, market: str, bar: pd.Timestamp, price: float) -> pd.Series:
        df = self.candles[market]
        if df.index[-1] < bar:  # no trade yet in the current bar
            df = pd.concat([df, pd.DataFrame({"open": price, "high": price, "low": price, "close": price,
                                              "volume": 0.0}, index=[bar])])
        prepared = prepare(self.cfg.strategy, regularize(df, self.cfg.timeframe), self.cfg.params)
        return prepared.iloc[-1]

    # -- trading ---------------------------------------------------------
    def _sell(self, market: str, price: float, reason: str) -> None:
        pos = self.state["positions"][market]
        # never sell more than the bot bought (the account may hold the same coin outside the bot)
        qty = min(pos["qty"], self.broker.holdings().get(market, 0.0))
        if qty * price < MIN_ORDER_KRW:
            log.warning("%s: %.8f left is below the minimum order, dropping position", market, qty)
            del self.state["positions"][market]
            return
        fill = self.broker.sell(market, qty, price)
        del self.state["positions"][market]
        self.save()
        pnl = fill * (1 - self.cfg.costs.fee) / (pos["entry"] * (1 + self.cfg.costs.fee)) - 1
        log.info("SELL %s qty=%.8f @ %.4f (%s) pnl=%.2f%%", market, qty, fill, reason, pnl * 100)
        notify(f"[tradebot] 매도 {market} @ {fill:,.0f} ({reason}) 손익 {pnl:+.2%}")
        self._log_trade(time=pd.Timestamp.now(tz="UTC").isoformat(), market=market, side="sell",
                        qty=qty, price=fill, reason=reason, pnl=round(pnl, 6))

    def _buy(self, market: str, krw: float, price: float, stop_dist: float, reason: str) -> None:
        qty, fill = self.broker.buy(market, krw, price)
        if qty <= 0:
            return
        stop = fill - stop_dist if math.isfinite(stop_dist) else None
        self.state["positions"][market] = {"qty": qty, "entry": fill, "stop": stop, "high": fill,
                                           "entry_time": pd.Timestamp.now(tz="UTC").isoformat()}
        self.save()
        log.info("BUY  %s %.0f KRW qty=%.8f @ %.4f (%s) stop=%s", market, krw, qty, fill, reason, stop)
        notify(f"[tradebot] 매수 {market} {krw:,.0f}원 @ {fill:,.0f}" + (f", 손절가 {stop:,.0f}" if stop else ""))
        self._log_trade(time=pd.Timestamp.now(tz="UTC").isoformat(), market=market, side="buy",
                        qty=qty, price=fill, reason=reason, pnl="")

    def equity(self, prices: dict[str, float]) -> float:
        invested = sum(p["qty"] * prices[m] for m, p in self.state["positions"].items())
        total = self.broker.cash() + invested
        return min(total, self.cfg.budget_krw) if self.cfg.budget_krw else total

    def _cash(self, prices: dict[str, float]) -> float:
        invested = sum(p["qty"] * prices[m] for m, p in self.state["positions"].items())
        return max(0.0, min(self.broker.cash(), self.equity(prices) - invested))

    def step(self, now: pd.Timestamp | None = None) -> None:
        now = now or pd.Timestamp.now(tz="UTC")
        self._refresh_candles(now.timestamp())
        prices = self.client.tickers(self.cfg.markets)
        bar = now.floor(f"{self.cfg.timeframe}min")
        day = str(now.date())
        self.guard.on_day(day, self.equity(prices))
        rows = {m: self._signal_row(m, bar, prices[m]) for m in self.cfg.markets}
        positions = self.state["positions"]

        for m in list(positions):
            row, pos = rows[m], positions[m]
            if row["exit"] and pd.Timestamp(pos["entry_time"]) < bar:
                self._sell(m, prices[m], "exit")
            elif pos["stop"] is not None and prices[m] <= pos["stop"]:
                self._sell(m, prices[m], "stop")
        risk = self.cfg.risk
        for m, pos in positions.items():  # giveback guard, after the stop check like the backtester
            pos["high"] = max(pos.get("high", pos["entry"]), prices[m])
            if risk.lock_gain and pos["high"] >= pos["entry"] * (1 + risk.lock_gain):
                pos["stop"] = max(pos["stop"] or 0.0, pos["high"] * (1 - risk.lock_giveback))

        if self.guard.can_trade and self.cfg.allow_entries:
            eq = self.equity(prices)
            alloc = self.cfg.risk.alloc_per_market or 1.0 / len(self.cfg.markets)
            weight = dict(zip(self.cfg.markets, inverse_vol_weights([rows[m]["vol"] for m in self.cfg.markets])
                              if self.cfg.risk.vol_reweight else [1.0] * len(self.cfg.markets)))
            for m in self.cfg.markets:
                if m in positions:
                    continue
                row, price = rows[m], prices[m]
                if row["enter"] and self.state["last_enter_bar"].get(m) != str(bar):
                    reason = "enter"
                elif row["entry_stop"] <= price and self.state["last_entry_day"].get(m) != day:
                    reason = "breakout"
                else:
                    continue
                krw = order_value(eq, self._cash(prices), alloc, row["size"] * weight[m], row["stop_dist"], price,
                                  self.cfg.risk.risk_per_trade, self.cfg.costs.fee)
                self.state["last_enter_bar"][m] = str(bar)
                self.state["last_entry_day"][m] = day
                if krw < MIN_ORDER_KRW:
                    continue
                self._buy(m, krw, price, row["stop_dist"], reason)

        reason = self.guard.check(self.equity(prices))
        if reason:
            log.warning("risk guard: %s -> closing all positions", reason)
            notify(f"[tradebot] 리스크 가드 발동: {reason} -> 전량 청산"
                   + (" (봇 정지, 확인 후 수동 재시작)" if reason == "max_drawdown" else " (내일까지 매매 중단)"))
            for m in list(positions):
                self._sell(m, prices[m], reason)
        self.save()

    def run_for(self, seconds: float) -> None:
        log.info("bot running: strategy=%s params=%s markets=%s entries=%s",
                 self.cfg.strategy, self.cfg.params, self.cfg.markets, self.cfg.allow_entries)
        end, failing = time.time() + seconds, False
        while time.time() < end:
            try:
                self.step()
                failing = False
            except Exception:  # keep running through network hiccups
                log.exception("step failed")
                if not failing:
                    notify("[tradebot] 오류 발생, 재시도 중 (logs/bot.log 확인)")
                failing = True
            time.sleep(self.cfg.poll_seconds)
