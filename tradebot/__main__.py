"""CLI: python -m tradebot {ui,check,report,run,fetch,optimize,backtest}"""
import sys

if sys.version_info < (3, 11):
    sys.exit("Python 3.11 이상이 필요합니다 (지금 %d.%d). https://www.python.org/downloads/" % sys.version_info[:2])

import argparse
import dataclasses
import json
import logging
import os
import signal
import threading
import time
import uuid
from pathlib import Path

import pandas as pd
import requests

from . import backtest, config, data
from .optimize import best_params, walk_forward
from .strategies import STRATEGIES, prepare

BTC = "KRW-BTC"

def _frames(cfg: config.Config, offline: bool = True) -> dict[str, pd.DataFrame]:
    return {m: data.load(m, cfg.timeframe, cfg.history_days, offline=offline) for m in cfg.markets}


def _btc(cfg: config.Config) -> pd.Series:
    """BTC closes for the entry confirmation, whether or not BTC is traded."""
    return data.load(BTC, cfg.timeframe, cfg.history_days, offline=True)["close"]


def _row(name: str, m: dict) -> str:
    return (f"| {name} | {m['total_return']:+.1%} | {m['cagr']:+.1%} | {m['avg_daily']:+.3%} | "
            f"{m['median_daily']:+.3%} | {m['sharpe']:.2f} | {m['max_drawdown']:.1%} | "
            f"{m.get('trades', 0)} | {m.get('win_rate', 0):.0%} |")


HEADER = ("| strategy | total | CAGR | avg/day | median/day | Sharpe | MDD | trades | win |\n"
          "|---|---|---|---|---|---|---|---|---|")


def cmd_fetch(cfg, args):
    for m in dict.fromkeys([*cfg.markets, BTC]):  # BTC is always needed for the entry confirmation
        df = data.load(m, cfg.timeframe, cfg.history_days)
        print(f"{m}: {len(df)} bars {df.index[0]} -> {df.index[-1]}")


def _no_guard(risk):
    return dataclasses.replace(risk, daily_target=None, daily_loss_limit=None, max_drawdown=None, lock_gain=None)


def cmd_backtest(cfg, args):
    frames = _frames(cfg)
    risk = _no_guard(cfg.risk) if args.no_guard else cfg.risk
    params = json.loads(args.params) if args.params else {}
    res = backtest.run({m: prepare(args.strategy, df, params, btc=_btc(cfg)) for m, df in frames.items()}, risk, cfg.costs)
    print(HEADER)
    print(_row(args.strategy, res.metrics()))
    if args.trades:
        print(res.trades.tail(20).to_string())


def cmd_optimize(cfg, args):
    from .learn import learned_slippage, review

    frames, btc = _frames(cfg), _btc(cfg)
    if args.no_guard:
        cfg.risk = _no_guard(cfg.risk)
    slip = learned_slippage("logs/live_trades.csv")
    if slip and slip > cfg.costs.slippage:  # real fills are worse than assumed: score strategies with reality
        print(f"live fills: slippage {slip:.3%} per side (assumed {cfg.costs.slippage:.3%}) -> using {slip:.3%}")
        cfg.costs = dataclasses.replace(cfg.costs, slippage=slip)
    rule_strategies = [s for s in cfg.strategies if s in STRATEGIES]  # the ai sleeve has nothing to backtest
    names = list(dict.fromkeys([*(args.strategies or [n for n in STRATEGIES if n != "hold"]), *rule_strategies]))
    results = {}
    print(f"walk-forward: train {cfg.train_days}d / test {cfg.test_days}d, markets={cfg.markets}")
    for name in names:
        results[name] = walk_forward(frames, name, cfg.risk, cfg.costs, cfg.train_days, cfg.test_days, btc=btc)
        logging.info("optimize: %s done", name)
    first_test = min(r["oos_returns"].index[0] for r in results.values())
    bench = backtest.run({m: prepare("hold", df) for m, df in frames.items()},
                         dataclasses.replace(_no_guard(cfg.risk), vol_reweight=False), cfg.costs, start=first_test)

    m = backtest.metrics(backtest.portfolio({s: results[s]["oos_returns"] for s in rule_strategies}))
    m["trades"] = sum(results[s]["metrics"]["trades"] for s in rule_strategies)
    m["win_rate"] = sum(results[s]["metrics"]["win_rate"] * results[s]["metrics"]["trades"] for s in rule_strategies) / max(m["trades"], 1)
    lines = [HEADER] + [_row(n, r["metrics"]) for n, r in results.items()]
    lines.append(_row(f"**portfolio: {' + '.join(rule_strategies)}**", m))
    lines.append(_row("buy & hold (equal weight)", bench.metrics()))
    table = "\n".join(lines)
    print(table)

    sleeves = {}
    for s in rule_strategies:
        params, train_sharpe = best_params(frames, s, cfg.risk, cfg.costs, cfg.train_days, btc=btc)
        oos = results[s]["metrics"]
        # same rule as the walk-forward: nothing worked -> no new entries for this sleeve
        sleeves[s] = {"params": params, "tradable": bool(oos["sharpe"] > 0 and train_sharpe > 0),
                      "recent_train_sharpe": round(float(train_sharpe), 2),
                      "oos_metrics": {k: float(v) for k, v in oos.items()}, "windows": results[s]["windows"]}
    rev = review({n: r["oos_returns"] for n, r in results.items()}, rule_strategies)
    summary = {"generated_at": pd.Timestamp.now(tz="UTC").isoformat(), "strategies": rule_strategies,
               "sleeves": sleeves, "portfolio_oos_metrics": {k: float(v) for k, v in m.items()},
               "costs_used": {"fee": cfg.costs.fee, "slippage": cfg.costs.slippage}, "self_review": rev}
    Path(config.SELECTED).parent.mkdir(exist_ok=True)
    Path(config.SELECTED).write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    Path(config.SELECTED).with_name("walkforward.md").write_text(
        f"# Walk-forward out-of-sample results\n\nGenerated {summary['generated_at']}, markets {cfg.markets}, "
        f"timeframe {cfg.timeframe}m, fee {cfg.costs.fee:.2%} + slippage {cfg.costs.slippage:.2%} per side, "
        f"daily loss limit {cfg.risk.daily_loss_limit}.\n\n{table}\n",
        encoding="utf-8")
    logging.info("self-review (chosen on data before %s, confirmed after): %s", rev["holdout_from"],
                 f"recommend strategies = {rev['recommend']}" if rev["recommend"] else "keep the current strategies")
    for s, v in sleeves.items():
        state = "trading" if v["tradable"] else "NOT trading new entries (stopped working recently)"
        print(f"sleeve {s}: {v['params']} -> {state}")
    print(f"portfolio out-of-sample: CAGR {m['cagr']:+.1%}, MDD {m['max_drawdown']:.1%} -> {config.SELECTED}")


UPBIT_ERRORS = {  # error name in Upbit's response -> what to do
    "no_authorization_ip": "업비트 > Open API 관리에서 이 PC/서버의 공인 IP를 등록하세요.",
    "invalid_access_key": "ACCESS 키가 틀렸습니다. 복사할 때 앞뒤 공백이 들어갔는지 확인하세요.",
    "jwt_verification": "SECRET 키가 틀렸습니다. 복사할 때 앞뒤 공백이 들어갔는지 확인하세요.",
    "expired_access_key": "키가 만료되었습니다. 업비트에서 재발급하세요.",
    "out_of_scope": "키 권한이 부족합니다. 자산조회·주문조회·주문하기 권한을 켜세요 (출금 권한은 끄세요).",
}


def _advice(e: Exception, keys=()) -> str:
    """Upbit/network error -> what the user should do. Secrets are masked."""
    if isinstance(e, requests.RequestException):
        return "업비트에 접속할 수 없습니다. 인터넷 연결을 확인하세요."
    msg = str(e)
    for k in keys:
        msg = msg.replace(k, "***") if k else msg
    return next((v for name, v in UPBIT_ERRORS.items() if name in msg), f"업비트 응답: {msg[:150]}")


def _ledger_cash(budget: float) -> tuple[float, bool]:
    """KRW the live ledgers will hold after this start's funding, and whether any ledger exists yet."""
    from .live import ledgers

    books = ledgers("live").values()
    return budget - sum(s["funded"] for s in books) + sum(s["cash"] for s in books), bool(books)


def diagnose(cfg, client, env, telegram: bool = False) -> tuple[list[str], list[str]]:
    """Everything `run` needs, checked up front. Returns (ok lines, problems). Never prints secrets.
    telegram=True also sends a test message (the check commands do; starting the bot does not)."""
    from .live import MIN_ORDER_KRW, send_telegram

    ok, problems = [], []
    live = cfg.mode == "live"
    if cfg.timeframe not in (60, 240):
        problems.append(f"config.toml 의 timeframe 은 240(4시간, 검증된 값) 또는 60만 됩니다 (지금 {cfg.timeframe}).")
    if cfg.mode not in ("paper", "live"):
        problems.append(f'config.toml 의 mode 는 "paper"(모의매매) 또는 "live"(실거래)만 됩니다 (지금 "{cfg.mode}").')
    ok.append("모드: 실거래" if live else "모드: 모의매매 (실제 주문 없음)")
    ok.append(f"전략: {' + '.join(cfg.strategies)} (예산을 똑같이 나눠 각자 운용)")
    unknown = [s for s in cfg.strategies if (s not in STRATEGIES or s == "hold") and s != "ai"]
    if unknown or not cfg.strategies:
        problems.append(f"config.toml 의 strategies = {cfg.strategies} 확인: "
                        f"{[s for s in STRATEGIES if s != 'hold'] + ['ai']} 중에서 고르세요.")
    if "ai" in cfg.strategies:
        from .ai_trader import PAPER_DAYS, paper_days

        if not (env.get("ANTHROPIC_API_KEY") or env.get("OPENAI_API_KEY")):
            problems.append("ai 전략은 Claude 또는 GPT API 키가 필요합니다 (제어판의 AI 검토 칸).")
        if not [x for x in cfg.strategies if x != "ai"]:
            problems.append("ai 전략은 규칙 전략(donchian, ema_cross 등) 하나 이상과 함께 고르세요. AI는 규칙 전략의 신호가 날 때 판단합니다.")
        if live and paper_days() < PAPER_DAYS:
            problems.append(f"ai 전략의 실거래는 모의매매에서 AI가 실제로 판단한 날이 {PAPER_DAYS}일 이상 있어야 합니다 "
                            f"(지금 {paper_days()}일). 모의매매로 먼저 돌리거나 전략에서 ai 를 빼세요.")
    if live and cfg.budget_krw <= 0:
        problems.append("config.toml 의 budget_krw 를 정하세요 (봇이 쓸 원화, 예: 1000000). 계좌의 나머지 돈은 건드리지 않습니다.")
    try:
        bad = [m for m in cfg.markets if m not in client.markets()]
        if bad:
            problems.append(f"{', '.join(bad)} 는 업비트 원화마켓에 없습니다 (예: KRW-BTC). config.toml 의 markets 를 고치세요.")
        else:
            ok.append(f"코인: {', '.join(cfg.markets)}")
    except Exception as e:
        problems.append(f"시세 확인 실패: {_advice(e)}")

    keys = env.get("UPBIT_ACCESS_KEY"), env.get("UPBIT_SECRET_KEY")
    if not all(keys):
        if live:
            problems.append("업비트 API 키가 없습니다. 제어판의 '업비트 API 키' 칸에 넣으세요 (.env 파일에 저장됩니다).")
    else:
        try:
            krw = sum(float(a["balance"]) for a in client.accounts() if a["currency"] == "KRW")
            ok.append(f"업비트 API 키 OK, KRW 잔고 {krw:,.0f}원")
            if live:
                need, restarted = _ledger_cash(cfg.budget_krw)
                if need > krw * 1.01:
                    msg = (f"봇 장부의 현금 {need:,.0f}원이 업비트 KRW 잔고 {krw:,.0f}원보다 많습니다. "
                           "원화를 입금하거나 config.toml 의 budget_krw 를 낮추세요.")
                    # after buys the KRW sits in coins: never refuse a restart, the open positions need their stops
                    (ok if restarted else problems).append(("주의: " if restarted else "") + msg)
                try:  # 주문조회 permission: a lookup of an order that cannot exist must say "not found"
                    client.order(identifier=f"tradebot-check-{uuid.uuid4().hex}")
                except RuntimeError as e:
                    if "order_not_found" not in str(e):
                        raise
                try:  # 주문하기 permission: Upbit's test endpoint validates an order without placing it
                    client.buy_market(cfg.markets[0], MIN_ORDER_KRW, path="/orders/test")
                except RuntimeError as e:
                    if "insufficient_funds" not in str(e):
                        raise
                ok.append("주문 권한 OK (업비트 주문 테스트, 실제 주문은 나가지 않음)")
        except Exception as e:
            problems.append(f"업비트 API 키 확인 실패: {_advice(e, keys)}")
    if env.get("TELEGRAM_BOT_TOKEN") and env.get("TELEGRAM_CHAT_ID"):
        if telegram and not send_telegram("tradebot 연결 점검: 이 메시지가 보이면 알림 설정 완료입니다.", env):
            problems.append("텔레그램 전송 실패: 토큰과 chat id 를 확인하고, 텔레그램에서 내 봇에게 /start 를 먼저 보내세요.")
        else:
            ok.append("텔레그램 알림: 켜짐" + (" (테스트 메시지 보냄)" if telegram else ""))
    else:
        ok.append("텔레그램 알림: 꺼짐 (선택, 제어판의 텔레그램 칸)")
    ai = "Claude" if env.get("ANTHROPIC_API_KEY") else "GPT" if env.get("OPENAI_API_KEY") else None
    ok.append(f"AI 주간 검토: {ai} 켜짐 (자동 실행 {'켜짐' if cfg.claude_autopilot else '꺼짐'})"
              if ai else "AI 주간 검토: 꺼짐 (선택, 제어판의 AI 검토 칸에 Claude 또는 GPT 키)")
    return ok, problems


def order_roundtrip(client, market: str = BTC, krw: int = 6_000) -> list[str]:
    """Real-money end-to-end check: buy `krw` of `market` and sell it straight back through the bot's own broker
    (orders sent with an identifier, fills read from the exchange, then looked up by identifier as after a crash).
    6,000 KRW so the sell side stays above Upbit's 5,000 KRW minimum unless the price falls 16% in between."""
    from .live import UpbitBroker

    broker = UpbitBroker(client)
    if broker.available_krw() < krw * 1.01:
        raise SystemExit(f"KRW 잔고가 {krw:,}원보다 적어 시험 주문을 하지 않았습니다.")
    price = client.tickers([market])[market]
    def sent(side, amount, at):  # a lost reply is not a failed order: ask Upbit what really happened
        ident = f"tradebot-test-{uuid.uuid4().hex}"
        try:
            return (broker.buy if side == "buy" else broker.sell)(market, amount, at, ident), ident
        except Exception as e:
            try:
                got = broker.resolve(ident)
            except Exception:
                got = "unknown"
            if got and got != "unknown" and got[0] > 0:
                return got, ident
            what = "매수" if side == "buy" else "매도"
            raise SystemExit(f"{what} 주문의 결과를 확인하지 못했습니다 ({_advice(e)}). 업비트 앱의 거래내역에서 "
                             f"주문번호 {ident} 를 확인하세요." + (" 매수한 코인이 남아 있으면 앱에서 직접 파세요."
                                                            if side == "sell" else ""))

    (vol, fill), _ = sent("buy", krw, price)
    lines = [f"매수 체결: {vol:.8f} {market.split('-')[1]} @ {fill:,.0f}원 (주문 직전 시세 {price:,.0f}원)"]
    (sold, sell_fill), ident = sent("sell", vol, fill)
    again = broker.resolve(ident)
    lines.append(f"매도 체결: {sold:.8f} @ {sell_fill:,.0f}원")
    lines.append("주문번호로 다시 조회: " + ("일치 (봇이 꺼졌다 켜져도 주문을 놓치지 않습니다)" if again and
                                         abs(again[0] - sold) < 1e-12 else f"불일치 {again}"))
    fee = config.Config().costs.fee
    cost = vol * fill * (1 + fee) - sold * sell_fill * (1 - fee)
    lines.append(f"왕복 비용 {cost:,.1f}원 = 수수료 {vol * fill * fee + sold * sell_fill * fee:,.1f}원 + 시세 차이·슬리피지")
    return lines


def cmd_check(cfg, args):
    from .upbit import UpbitClient

    client = UpbitClient(os.environ.get("UPBIT_ACCESS_KEY"), os.environ.get("UPBIT_SECRET_KEY"))
    ok, problems = diagnose(cfg, client, os.environ, telegram=True)
    for line in ok:
        print("  OK  ", line)
    for line in problems:
        print("  FAIL", line)
    if problems:
        raise SystemExit(1)
    if getattr(args, "test_order", False):
        print(f"\n실제 주문 점검: 업비트에서 {BTC} 6,000원어치를 사고 바로 팝니다. 비용은 수수료 약 6원과 시세 차이입니다.")
        if input("계속하려면 yes 를 입력하세요: ").strip().lower() != "yes":
            raise SystemExit("취소했습니다.")
        for line in order_roundtrip(client):
            print("  OK  ", line)
    print("준비 완료.")


def cmd_ui(cfg, args):
    from .ui import serve

    serve(args.port, not args.no_browser)


def cmd_report(cfg, args):
    from .report import report

    print("\n".join(report(cfg.mode, f"logs/{cfg.mode}_equity.csv")))


def cmd_review(cfg, args):
    from .claude import ACTIONS, run

    out = run(cfg)
    if out is None:
        raise SystemExit("AI 검토를 하지 못했습니다: 제어판에서 Claude 또는 GPT API 키를 넣었는지, 인터넷이 되는지 확인하세요.")
    rv = out["review"]
    print(rv["summary"], *(f"- 위험: {r}" for r in rv["risks"]), *(f"- 확인: {c}" for c in rv["user_checks"]),
          f"조치: {ACTIONS[rv['action']]} ({out['status']}) - {rv['reason']}", sep="\n")


def cmd_resume(cfg, args):
    """Clear the kill switch, after you have looked into why it fired."""
    from .live import clear_halt, running_pid

    if running_pid():
        raise SystemExit("봇을 먼저 정지하세요 (제어판의 정지 버튼).")
    cleared = clear_halt(cfg.mode)
    for name in cleared:
        print(f"{name}: 비상 정지를 풀었습니다. 다시 시작하면 지금 장부를 새 고점으로 삼아 매매합니다.")
    print("제어판에서 다시 시작하세요." if cleared else "풀 것이 없습니다.")


REOPTIMIZE_DAYS = 7


def selection_age_days() -> float:
    made = config.selection().get("generated_at")
    return (pd.Timestamp.now(tz="UTC") - pd.Timestamp(made)) / pd.Timedelta(days=1) if made else float("inf")


def needs_reoptimize(cfg) -> bool:
    return selection_age_days() >= REOPTIMIZE_DAYS or len(cfg.sleeves()) < len(cfg.strategies)


def reoptimize(cfg) -> None:
    """Re-download data and re-run the walk-forward (a few minutes). The bots keep trading meanwhile."""
    logging.info("주간 재최적화 시작: 데이터 갱신 + 전략 재검증 (몇 분, 그동안 매매는 계속)")
    try:
        cmd_fetch(cfg, None)
        cmd_optimize(cfg, argparse.Namespace(strategies=None, no_guard=False))
        logging.info("주간 재최적화 완료: 새 설정으로 다시 시작합니다")
    except Exception:
        logging.exception("re-optimisation failed; keeping the previous selection")
    from .claude import run as claude_review

    claude_review(cfg)  # no-op without ANTHROPIC_API_KEY


def cmd_run(cfg, args):
    from .live import PID_FILE, STOP, STOP_FILE, running_pid
    from .upbit import UpbitClient

    live = cfg.mode == "live"
    if live and not args.i_understand_the_risk:
        raise SystemExit("실거래 모드입니다. 위험을 이해했다면 제어판의 '시작'을 쓰거나 "
                         "./start.sh run --i-understand-the-risk 로 실행하세요 (Windows: start.bat run --i-understand-the-risk).")
    client = UpbitClient(os.environ.get("UPBIT_ACCESS_KEY"), os.environ.get("UPBIT_SECRET_KEY"))
    _, problems = diagnose(cfg, client, os.environ)
    if problems:
        raise SystemExit("시작하지 못했습니다. 아래를 고친 뒤 다시 시작하세요:\n  " + "\n  ".join(problems))
    if running_pid():  # two processes on the same ledgers would trade twice
        raise SystemExit("봇이 이미 실행 중입니다 (state/bot.pid). 제어판이나 그 창에서 먼저 정지하세요.")
    PID_FILE.parent.mkdir(exist_ok=True)
    PID_FILE.write_text(f"{os.getpid()} {cfg.mode}")
    STOP_FILE.unlink(missing_ok=True)

    def on_signal(signum, frame):
        if STOP.is_set():  # second Ctrl+C: stop right away
            raise KeyboardInterrupt
        logging.info("정지 요청을 받았습니다. 지금 단계를 마치고 저장한 뒤 멈춥니다 (한 번 더 누르면 즉시 종료).")
        STOP.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, on_signal)
    try:
        _run_loop(cfg, args, live, client)
    finally:
        PID_FILE.unlink(missing_ok=True)
        STOP_FILE.unlink(missing_ok=True)


def _bots(cfg, live, client, broker, budget):
    """One bot per strategy, each funded budget / number of strategies. A strategy removed from the config
    while it still holds coins keeps a bot that only manages them (stops and exits, no new buys)."""
    from .live import Bot, BotConfig, ledgers

    def bot(name, params, tradable, funding):
        return Bot(BotConfig(markets=cfg.markets, strategy=name, params=params, risk=cfg.risk, costs=cfg.costs,
                             timeframe=cfg.timeframe, poll_seconds=cfg.poll_seconds, allow_entries=tradable,
                             state_path=f"state/{cfg.mode}_{name}.json", trades_path=f"logs/{cfg.mode}_trades.csv",
                             equity_path=f"logs/{cfg.mode}_equity.csv"), client, broker=broker, funding=funding)

    from .ai_trader import PAPER_DAYS, paper_days

    sleeves = cfg.sleeves()
    if live and "ai" in {n for n, _, _ in sleeves} and paper_days() < PAPER_DAYS:  # also when added while running
        logging.warning("ai: 모의 투자에서 AI 판단이 %d일 쌓여야 실제 돈으로 매매합니다 (지금 %d일). 이번에는 빼고 돌립니다",
                        PAPER_DAYS, paper_days())
        sleeves = [s for s in sleeves if s[0] != "ai"]
    held = {name: st for name, st in ledgers(cfg.mode).items() if (name in STRATEGIES or name == "ai")
            and name not in {n for n, _, _ in sleeves} and st["positions"]}
    tied = sum(p["qty"] * p["entry"] for st in held.values() for p in st["positions"].values())
    bots = [bot(n, p, t, max(budget - tied, 0) / len(cfg.strategies)) for n, p, t in sleeves]
    for name, st in held.items():
        logging.warning("%s: 설정에서 빠졌지만 보유 중인 코인이 있어 손절/청산만 계속합니다", name)
        bots.append(bot(name, {}, False, st["funded"]))
    return sleeves, bots


def _run_loop(cfg, args, live, client):
    from .live import UpbitBroker, notify, run_bots, stop_requested
    from .report import report

    budget = cfg.budget_krw if live else cfg.paper_krw
    broker = UpbitBroker(client) if live else None  # paper: every sleeve gets its own simulated holdings
    tried, announced = 0.0, None
    while not stop_requested():
        cfg.strategies = config.load(args.config).strategies  # Claude or the panel may have applied a recommendation
        job = None
        if needs_reoptimize(cfg) and time.time() - tried > 3600:
            tried = time.time()  # ponytail: a failed run is retried hourly; more often only hammers Upbit
            job = threading.Thread(target=reoptimize, args=(dataclasses.replace(cfg),), daemon=True)
            job.start()
            if args.once:
                job.join()
        sleeves, bots = _bots(cfg, live, client, broker, budget)
        if args.once:
            return [b.step() for b in bots]
        stamp = config.selection().get("generated_at")
        if job is None and stamp != announced:  # once per (weekly) selection
            announced = stamp
            notify("[tradebot] 주간 리포트\n" + "\n".join(report(cfg.mode, f"logs/{cfg.mode}_equity.csv")))
            notify(f"[tradebot] {cfg.mode} 시작, 예산 {budget:,.0f}원을 {len(cfg.strategies)}개 전략에 나눔: "
                   + ", ".join(f"{n}{'' if t else '(신규 진입 중단)'}" for n, _, t in sleeves))
        if job:  # trade with the previous selection until the new one is ready, then rebuild
            run_bots(bots, float("inf"), cfg.poll_seconds, done=lambda: not job.is_alive())
        else:
            run_bots(bots, max((REOPTIMIZE_DAYS - selection_age_days()) * 86400, 3600), cfg.poll_seconds)
    notify(f"[tradebot] {cfg.mode} 정지했습니다.")


def main():
    ap = argparse.ArgumentParser(prog="tradebot")
    ap.add_argument("--config", default="config.toml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="verify config, API keys and alerts before running")
    c.add_argument("--test-order", action="store_true", help="also buy 6,000 KRW of BTC and sell it back (real money)")
    sub.add_parser("report", help="live/paper results vs the plan's expected ranges, and the next step")
    u = sub.add_parser("ui", help="open the control panel in the browser (settings, start/stop, status)")
    u.add_argument("--port", type=int, default=8765)
    u.add_argument("--no-browser", action="store_true")
    sub.add_parser("resume", help="clear the -35% kill switch after checking why it fired")
    sub.add_parser("review", help="ask Claude or GPT to review the bot now (ANTHROPIC_API_KEY or OPENAI_API_KEY)")
    sub.add_parser("fetch", help="download/update candle history")
    b = sub.add_parser("backtest", help="backtest one strategy on the cached history")
    b.add_argument("--strategy", required=True, choices=list(STRATEGIES))
    b.add_argument("--params", help='JSON, e.g. \'{"k": 0.5}\'')
    b.add_argument("--no-guard", action="store_true", help="disable daily target / loss limit / max drawdown")
    b.add_argument("--trades", action="store_true")
    o = sub.add_parser("optimize", help="walk-forward all strategies and select the best")
    o.add_argument("--strategies", nargs="*")
    o.add_argument("--no-guard", action="store_true", help="optimise without daily target / loss limit / max drawdown")
    r = sub.add_parser("run", help="run the bot (paper unless config mode = live)")
    r.add_argument("--once", action="store_true", help="single step, for testing")
    r.add_argument("--i-understand-the-risk", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), *(_file_handler() if args.cmd == "run" else [])])
    config.load_env()
    cfg = config.load(args.config)
    {"ui": cmd_ui, "check": cmd_check, "report": cmd_report, "resume": cmd_resume, "review": cmd_review, "fetch": cmd_fetch, "backtest": cmd_backtest, "optimize": cmd_optimize, "run": cmd_run}[args.cmd](cfg, args)


def _file_handler():
    Path("logs").mkdir(exist_ok=True)
    return [logging.FileHandler("logs/bot.log", encoding="utf-8")]


if __name__ == "__main__":
    main()
