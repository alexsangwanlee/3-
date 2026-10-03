"""Paper / live trading loop. Uses the same strategy columns and risk rules as the backtester."""
import csv
import json
import logging
import math
import os
import subprocess
import threading
import time
import uuid
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
PID_FILE = Path("state/bot.pid")
STOP_FILE = Path("state/stop.request")  # the control panel's stop button: finish the step, save, exit
STOP = threading.Event()                # Ctrl+C / SIGTERM do the same
PAUSE_FILE = Path("state/entries_paused.json")  # no new buys (Claude's review or the user); stops still run


def stop_requested() -> bool:
    return STOP.is_set() or STOP_FILE.exists()


def ledgers(mode: str) -> dict[str, dict]:
    """Every strategy ledger saved for `mode` (state/<mode>_<strategy>.json), including removed strategies."""
    return {p.stem.removeprefix(f"{mode}_"): json.loads(p.read_text(encoding="utf-8"))
            for p in sorted(Path("state").glob(f"{mode}_*.json"))}


def entries_paused() -> bool:
    return PAUSE_FILE.exists()


def pause_info() -> dict | None:
    try:
        return json.loads(PAUSE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"reason": "?"} if PAUSE_FILE.exists() else None


def pause_entries(reason: str) -> None:
    PAUSE_FILE.parent.mkdir(exist_ok=True)
    PAUSE_FILE.write_text(json.dumps({"reason": reason, "at": pd.Timestamp.now(tz="UTC").isoformat()},
                                     ensure_ascii=False), encoding="utf-8")
    log.warning("new buys paused: %s", reason)


def _alive(pid: int) -> bool:
    """Is `pid` a running tradebot? (Never os.kill(pid, 0): on Windows that terminates the process.)"""
    try:
        if os.name == "nt":
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                                 capture_output=True, text=True, timeout=5).stdout
            return "python" in out.lower()  # ponytail: image name only; a reused PID of another python looks alive
        proc = Path(f"/proc/{pid}/cmdline")
        if proc.exists():
            return b"tradebot" in proc.read_bytes()
        out = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, timeout=5).stdout
        return "tradebot" in out
    except (OSError, subprocess.SubprocessError):
        return False


def running_pid() -> int | None:
    try:
        pid = int(PID_FILE.read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None
    return pid if pid != os.getpid() and _alive(pid) else None  # Docker: a restarted container is PID 1 again


def running_mode() -> str | None:
    """"paper" / "live" of the bot that is running now (state/bot.pid holds "<pid> <mode>")."""
    return PID_FILE.read_text().split()[1] if running_pid() else None


def send_telegram(text: str, env=None) -> bool:
    """Telegram message when TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are set. Never raises. True if delivered."""
    env = os.environ if env is None else env
    token, chat = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat, "text": text},
                          timeout=10)
        if r.ok:
            return True
        log.warning("telegram notify failed: HTTP %s", r.status_code)
    except Exception:
        log.warning("telegram notify failed")  # no exception text: it contains the URL, i.e. the token
    return False


def notify(text: str) -> None:
    """Fire and forget, so a slow Telegram never delays a stop-loss."""
    threading.Thread(target=send_telegram, args=(text,), daemon=True).start()


class PaperBroker:
    """Simulated fills at the current price with slippage. Cash lives in the bot's ledger."""

    def __init__(self, holdings: dict, costs: Costs):
        self.held, self.costs = holdings, costs

    def available_krw(self) -> float:
        return math.inf

    def holdings(self) -> dict[str, float]:
        return {m: q for m, q in self.held.items() if q > 0}

    def buy(self, market: str, krw: float, price: float, ident: str) -> tuple[float, float]:
        fill = price * (1 + self.costs.slippage)
        self.held[market] = self.held.get(market, 0.0) + krw / fill
        return krw / fill, fill

    def sell(self, market: str, qty: float, price: float, ident: str) -> tuple[float, float]:
        self.held[market] = max(0.0, self.held.get(market, 0.0) - qty)
        return qty, price * (1 - self.costs.slippage)

    def resolve(self, ident: str) -> None:
        return None  # a paper order interrupted by a crash never happened


class UpbitBroker:
    """Real market orders on Upbit."""

    def __init__(self, client: UpbitClient):
        self.client = client

    def available_krw(self) -> float:
        return sum(float(a["balance"]) for a in self.client.accounts() if a["currency"] == "KRW")

    def holdings(self) -> dict[str, float]:
        return {f"KRW-{a['currency']}": float(a["balance"]) for a in self.client.accounts()
                if a["currency"] != "KRW" and float(a["balance"]) > 0}

    def _filled(self, **query) -> tuple[float, float]:
        """(executed volume, average price) once the order is closed."""
        for _ in range(20):
            o = self.client.order(**query)
            if o["state"] in ("done", "cancel"):
                vol = sum(float(t["volume"]) for t in o.get("trades", []))
                funds = sum(float(t["funds"]) for t in o.get("trades", []))
                return vol, (funds / vol if vol else 0.0)
            time.sleep(0.5)
        raise RuntimeError(f"order {query} not settled yet")

    def buy(self, market: str, krw: float, price: float, ident: str) -> tuple[float, float]:
        return self._filled(uuid=self.client.buy_market(market, krw, ident)["uuid"])

    def sell(self, market: str, qty: float, price: float, ident: str) -> tuple[float, float]:
        return self._filled(uuid=self.client.sell_market(market, qty, ident)["uuid"])

    def resolve(self, ident: str) -> tuple[float, float] | None:
        """Outcome of an order whose reply was lost. None: it never reached the exchange."""
        try:
            return self._filled(identifier=ident)
        except RuntimeError as e:
            if "order_not_found" in str(e):
                return None
            raise


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
    equity_path: str = "logs/equity.csv"
    allow_entries: bool = True  # False = optimize found nothing worth trading: only manage open positions


class Bot:
    """One strategy trading its own sub-account. `funding` is the KRW given to it (budget_krw / sleeves).

    The ledger (state["cash"] + positions) is the bot's equity, so KRW deposited or withdrawn on the
    exchange account by hand never looks like profit or loss to the bot.
    """

    def __init__(self, cfg: BotConfig, client: UpbitClient, broker=None, funding: float = 1_000_000):
        self.cfg, self.client = cfg, client
        self.state = self._load_state()
        self.broker = broker or PaperBroker(self.state["paper_holdings"], cfg.costs)
        g = self.state.get("guard") or DailyGuard.from_risk(cfg.risk).to_dict()
        self.guard = DailyGuard(**{**g, "daily_target": cfg.risk.daily_target,
                                   "daily_loss_limit": cfg.risk.daily_loss_limit,
                                   "max_drawdown": cfg.risk.max_drawdown})
        self.candles: dict[str, pd.DataFrame] = {}
        self.last_refresh = 0.0
        self.last_bar = None
        self._fund(funding)

    # -- persistence -----------------------------------------------------
    def _load_state(self) -> dict:
        p = Path(self.cfg.state_path)
        state = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {
            "cash": 0.0, "funded": 0.0, "positions": {}, "last_entry_day": {}, "last_enter_bar": {}, "guard": None,
            "paper_holdings": {}}
        state.setdefault("pending", {})  # orders sent but not yet booked: {identifier: order}
        return state

    def _fund(self, target: float) -> None:
        """budget_krw changed since the last start: move the difference into or out of the ledger."""
        delta = target - self.state["funded"]
        if not delta:
            return
        self.state["cash"] += delta
        self.state["funded"] = target
        if self.guard.day_start:  # new money is not profit: shift the daily and drawdown references too
            self.guard.day_start += delta
            self.guard.peak += delta
        log.info("%s: funding %s KRW (total %s)", self.cfg.strategy, f"{delta:+,.0f}", f"{target:,.0f}")
        self.save()

    def save(self) -> None:
        self.state["guard"] = self.guard.to_dict()
        p = Path(self.cfg.state_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")  # a crash mid-write must never leave a half ledger
        tmp.write_text(json.dumps(self.state, indent=2, default=str), encoding="utf-8")
        os.replace(tmp, p)

    @staticmethod
    def _append(path: str, **row) -> None:
        """Add a CSV row. A log file open in Excel (locked on Windows) never stops trading."""
        p = Path(path)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            if p.exists():
                with p.open(encoding="utf-8") as f:
                    header = f.readline().strip().split(",")
                if header != list(row):  # columns changed in an update: keep old rows, rewrite the header
                    old = pd.read_csv(p, on_bad_lines="skip")
                    old.reindex(columns=list(dict.fromkeys([*row, *old.columns]))).to_csv(p, index=False)
                    row = {k: row.get(k, "") for k in pd.read_csv(p, nrows=0).columns}
            new = not p.exists()
            with p.open("a", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=list(row))
                if new:
                    w.writeheader()
                w.writerow(row)
        except OSError as e:
            log.warning("could not write %s: %s", path, e)

    # -- market data -----------------------------------------------------
    def _refresh_candles(self, now: float) -> None:
        if now - self.last_refresh < 60 and self.candles:
            return
        for m in dict.fromkeys([*self.cfg.markets, "KRW-BTC"]):  # BTC: the entry confirmation needs it
            try:
                self._load_candles(m)
            except Exception as e:  # that market gets no new entries until it answers again
                log.warning("%s: candles unavailable (%s)", m, type(e).__name__)
        self.last_refresh = now

    def _load_candles(self, m: str) -> None:
        if m in self.candles:  # only the newest page is needed after the first load
            df = pd.concat([self.candles[m], to_frame(self.client.candles(m, self.cfg.timeframe, count=200))])
            self.candles[m] = df[~df.index.duplicated(keep="last")].iloc[-self.cfg.history_bars:]
            return
        rows, to = [], None
        while len(rows) < self.cfg.history_bars:
            page = self.client.candles(m, self.cfg.timeframe, to=to, count=200)
            if not page:
                break
            rows += page
            to = page[-1]["candle_date_time_utc"] + "Z"
            time.sleep(0.12)  # candles: 10 requests/s per IP, shared by every sleeve
        self.candles[m] = to_frame(rows)

    def _prices(self) -> dict[str, float]:
        """Tickers for the configured markets and every open position. One bad market never hides the rest."""
        markets = list(dict.fromkeys([*self.cfg.markets, *self.state["positions"]]))
        try:
            prices = self.client.tickers(markets)
        except Exception:
            prices = {}
            for m in markets:
                try:
                    prices.update(self.client.tickers([m]))
                except Exception as e:
                    log.warning("%s: no price (%s)", m, type(e).__name__)
        for m, pos in self.state["positions"].items():
            if m in prices:
                pos["last"] = prices[m]
        return prices

    def _signal_row(self, market: str, bar: pd.Timestamp, price: float) -> pd.Series:
        df = self.candles[market]
        if df.index[-1] < bar:  # no trade yet in the current bar
            df = pd.concat([df, pd.DataFrame({"open": price, "high": price, "low": price, "close": price,
                                              "volume": 0.0}, index=[bar])])
        btc = self.candles["KRW-BTC"]["close"]  # missing -> this market gets no signal this step (never unconfirmed)
        prepared = prepare(self.cfg.strategy, regularize(df, self.cfg.timeframe), self.cfg.params, btc=btc)
        return prepared.iloc[-1]

    def _ai_rows(self, bar, prices: dict) -> dict:
        """The ai sleeve's rows: the AIs decide once per bar; if they cannot, no new buys (stops still run)."""
        if getattr(self, "_ai", (None,))[0] != bar:
            from .ai_trader import rows

            try:
                got = rows(self, bar, prices)
            except Exception as e:
                log.warning("ai: no decision this bar (%s)", type(e).__name__)
                got = {}
            self._ai = (bar, got)
        return {m: r for m, r in self._ai[1].items() if m in prices}

    # -- trading ---------------------------------------------------------
    def _order(self, side: str, market: str, amount: float, price: float, **meta) -> None:
        """Saved before it is sent, so a lost reply or a crash can never book it twice or lose it."""
        ident = f"tradebot-{uuid.uuid4().hex}"
        self.state["pending"][ident] = {"side": side, "market": market, "price": price, **meta}
        self.save()
        vol, fill = (self.broker.buy if side == "buy" else self.broker.sell)(market, amount, price, ident)
        self._book(self.state["pending"].pop(ident), vol, fill)

    def _resolve_pending(self) -> None:
        """Book orders whose outcome was unknown. Unanswered ones stay pending and block their market."""
        for ident, order in list(self.state["pending"].items()):
            try:
                got = self.broker.resolve(ident)
            except Exception as e:
                log.warning("%s: order %s still unconfirmed (%s)", order["market"], ident, type(e).__name__)
                continue
            del self.state["pending"][ident]
            if got:
                self._book(order, *got)
            self.save()

    def _busy(self, market: str) -> bool:
        """An order in this market has no confirmed outcome yet: never send another one."""
        return any(o["market"] == market for o in self.state["pending"].values())

    def _book(self, order: dict, vol: float, fill: float) -> None:
        """Record what really executed (a partial or empty fill books only that)."""
        m, fee, positions = order["market"], self.cfg.costs.fee, self.state["positions"]
        t = pd.Timestamp.now(tz="UTC").isoformat()
        if vol <= 0:
            log.warning("%s: %s order executed nothing; will retry if still needed", m, order["side"])
            self.save()
            return
        if order["side"] == "buy":
            self.state["cash"] -= vol * fill * (1 + fee)
            old = positions.get(m, {"qty": 0.0, "entry": fill})  # a leftover too small to sell joins the new one
            qty = old["qty"] + vol
            stop = fill - order["stop_dist"] if math.isfinite(order["stop_dist"]) else None
            positions[m] = {"qty": qty, "entry": (old["qty"] * old["entry"] + vol * fill) / qty, "stop": stop,
                            "high": fill, "last": fill, "entry_time": t,
                            **({"rule": order["reason"][3:]} if order["reason"].startswith("ai:") else {})}
            self.save()
            log.info("BUY  %s qty=%.8f @ %.4f (%s) stop=%s", m, vol, fill, order["reason"], stop)
            notify(f"[tradebot] 매수 {m} {vol * fill:,.0f}원 @ {fill:,.0f}" + (f", 손절가 {stop:,.0f}" if stop else ""))
            self._append(self.cfg.trades_path, time=t, strategy=self.cfg.strategy, market=m, side="buy", qty=vol, price=fill,
                            reason=order["reason"], pnl="", slip=round(fill / order["price"] - 1, 6),
                            **self._costs(vol, fill, fill - order["price"]))
            return
        self.state["cash"] += vol * fill * (1 - fee)
        pos = positions.get(m)
        if pos:
            pos["qty"] -= vol
            if pos["qty"] * fill < 1:  # ponytail: under 1 KRW left counts as fully sold
                del positions[m]
        self.save()
        pnl = fill * (1 - fee) / (order["entry"] * (1 + fee)) - 1
        if pos and pos.get("rule") and m not in positions:  # an ai trade closed: its rule earns the result
            from .ai_trader import record

            record(pos["rule"], pnl)
        log.info("SELL %s qty=%.8f @ %.4f (%s) pnl=%.2f%%", m, vol, fill, order["reason"], pnl * 100)
        notify(f"[tradebot] 매도 {m} @ {fill:,.0f} ({order['reason']}) 손익 {pnl:+.2%}")
        self._append(self.cfg.trades_path, time=t, strategy=self.cfg.strategy, market=m, side="sell", qty=vol, price=fill,
                        reason=order["reason"], pnl=round(pnl, 6),
                        slip=round(1 - fill / order["price"], 6),  # vs the price the decision saw: learned by optimize
                        **self._costs(vol, fill, order["price"] - fill))

    def _costs(self, vol: float, fill: float, worse_by: float) -> dict:
        """What this fill cost in KRW: the exchange fee, and slippage against the price the decision saw."""
        value = vol * fill
        return {"value": round(value, 2), "fee": round(value * self.cfg.costs.fee, 2), "slip_cost": round(vol * worse_by, 2)}

    def _sell(self, market: str, price: float, reason: str) -> None:
        pos = self.state["positions"][market]
        # never sell more than the bot bought (the account may hold the same coin outside the bot),
        # nor more than the account still has (coins sold by hand leave the books too)
        qty = min(pos["qty"], self.broker.holdings().get(market, 0.0))
        if qty <= 0:
            log.warning("%s: the account no longer holds this position; removing it from the books", market)
            del self.state["positions"][market]
        elif qty * price < MIN_ORDER_KRW:  # Upbit refuses it: keep it on the books, the next buy absorbs it
            if not pos.get("dust"):
                log.warning("%s: %.8f left is below Upbit's minimum order; kept until the next buy", market, qty)
            pos.update(qty=qty, dust=True)
        else:
            self._order("sell", market, qty, price, reason=reason, entry=pos["entry"])

    def equity(self, prices: dict[str, float]) -> float:
        return self.state["cash"] + sum(p["qty"] * prices.get(m, p.get("last", p["entry"]))
                                        for m, p in self.state["positions"].items())

    def _cash(self) -> float:
        return max(0.0, min(self.state["cash"], self.broker.available_krw()))

    def step(self, now: pd.Timestamp | None = None) -> None:
        """One pass: book unconfirmed orders, exits and stops, entries, the daily guard. A failure in one
        market is logged and the others still run; the first failure is re-raised at the end."""
        now = now or pd.Timestamp.now(tz="UTC")
        errors = []

        def attempt(fn, *args, **kw) -> bool:
            try:
                fn(*args, **kw)
                return True
            except Exception as e:
                log.warning("%s %s failed: %s", fn.__name__, args[:2], type(e).__name__)
                errors.append(e)
                return False

        self._resolve_pending()
        self._refresh_candles(now.timestamp())
        prices = self._prices()
        positions = self.state["positions"]
        bar = now.floor(f"{self.cfg.timeframe}min")
        day = str(now.date())
        self.guard.on_day(day, self.equity(prices))
        if self.state.get("logged_day") != day:  # one equity row per day per sleeve, for `tradebot report`
            self._append(self.cfg.equity_path, date=day, strategy=self.cfg.strategy,
                         equity=round(self.equity(prices), 2), funded=self.state["funded"])
            self.state["logged_day"] = day
        rows = self._ai_rows(bar, prices) if self.cfg.strategy == "ai" else {}
        for m in self.cfg.markets if self.cfg.strategy != "ai" else ():
            if m in prices and m in self.candles:
                try:
                    rows[m] = self._signal_row(m, bar, prices[m])
                except Exception as e:
                    log.warning("%s: no signal (%s)", m, type(e).__name__)

        def sellable(m):
            return m in prices and not self._busy(m) and not positions[m].get("dust")

        for m in [m for m in positions if sellable(m)]:
            row, pos = rows.get(m), positions[m]
            if row is not None and row["exit"] and pd.Timestamp(pos["entry_time"]) < bar:
                attempt(self._sell, m, prices[m], "exit")
            elif pos["stop"] is not None and prices[m] <= pos["stop"]:
                attempt(self._sell, m, prices[m], "stop")
        risk = self.cfg.risk
        for m, pos in positions.items():  # giveback guard, after the stop check like the backtester
            if m not in prices:
                continue
            pos["high"] = max(pos.get("high", pos["entry"]), prices[m])
            if risk.lock_gain and risk.lock_giveback and pos["high"] >= pos["entry"] * (1 + risk.lock_gain):
                pos["stop"] = max(pos["stop"] or 0.0, pos["high"] * (1 - risk.lock_giveback))

        if self.guard.can_trade and self.cfg.allow_entries and not self.state.get("flatten") and not entries_paused():
            eq = self.equity(prices)
            alloc = self.cfg.risk.alloc_per_market or 1.0 / len(self.cfg.markets)
            vols = [rows[m]["vol"] if m in rows else math.nan for m in self.cfg.markets]
            weight = dict(zip(self.cfg.markets, inverse_vol_weights(vols)
                              if self.cfg.risk.vol_reweight else [1.0] * len(self.cfg.markets)))
            for m in self.cfg.markets:
                if m not in rows or self._busy(m) or (m in positions and not positions[m].get("dust")):
                    continue
                row, price = rows[m], prices[m]
                if row["enter"] and self.state["last_enter_bar"].get(m) != str(bar):
                    reason = f"ai:{row['rule']}" if self.cfg.strategy == "ai" else "enter"
                elif row["entry_stop"] <= price and self.state["last_entry_day"].get(m) != day:
                    reason = "breakout"
                else:
                    continue
                krw = order_value(eq, self._cash(), alloc, row["size"] * weight[m], row["stop_dist"], price,
                                  self.cfg.risk.risk_per_trade, self.cfg.costs.fee)
                # a buy that failed before reaching the exchange is retried on the next poll
                if krw < MIN_ORDER_KRW or attempt(self._order, "buy", m, krw, price,
                                                  stop_dist=float(row["stop_dist"]), reason=reason):
                    self.state["last_enter_bar"][m] = str(bar)
                    self.state["last_entry_day"][m] = day

        reason = self.guard.check(self.equity(prices))
        if reason:
            log.warning("risk guard: %s -> closing all positions", reason)
            notify(f"[tradebot] 리스크 가드 발동: {reason} -> 전량 청산"
                   + (" 후 신규 매수 중단. 원인 확인 후 정지하고 ./start.sh resume 으로 해제" if reason == "max_drawdown"
                      else " 후 오늘은 신규 매수 중단"))
            self.state["flatten"] = reason
        if self.state.get("flatten"):  # kept until every position is gone, so a failed sell is retried
            for m in [m for m in positions if sellable(m)]:
                attempt(self._sell, m, prices[m], self.state["flatten"])
            if all(p.get("dust") for p in positions.values()):
                self.state.pop("flatten")
        self.save()
        if bar != self.last_bar:  # a sign of life once per candle (trades can be days apart)
            self.last_bar = bar
            log.info("%s: 장부 %s원, 보유 %d개, 다음 봉까지 대기", self.cfg.strategy, f"{self.equity(prices):,.0f}",
                     len(positions))
        if errors:
            raise errors[0]


def run_bots(bots: list[Bot], seconds: float, poll_seconds: int, done=lambda: False) -> None:
    """Step every sleeve each poll for `seconds` (or until `done()`). One sleeve failing never stops the others."""
    for b in bots:
        log.info("sleeve %s: params=%s entries=%s", b.cfg.strategy, b.cfg.params, b.cfg.allow_entries)
    end, failing = time.time() + seconds, set()
    while time.time() < end and not stop_requested() and not done():
        for b in bots:
            try:
                b.step()
                if b.cfg.strategy in failing:
                    log.info("%s: 다시 정상입니다", b.cfg.strategy)
                failing.discard(b.cfg.strategy)
            except Exception as e:  # keep running through network hiccups
                if b.cfg.strategy in failing:  # one traceback per outage, not one every poll
                    log.warning("%s: 아직 실패 중, 재시도 (%s)", b.cfg.strategy, type(e).__name__)
                    continue
                log.exception("%s: step failed", b.cfg.strategy)
                notify(f"[tradebot] {b.cfg.strategy} 오류, 재시도 중 (logs/bot.log 확인)")
                failing.add(b.cfg.strategy)
        for _ in range(int(poll_seconds)):  # 1 s slices: the panel's stop button is noticed within a second
            if stop_requested() or done():
                break
            STOP.wait(1)
