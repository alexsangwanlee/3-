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
from pathlib import Path

import pandas as pd

from . import backtest, config, data
from .optimize import best_params, walk_forward
from .strategies import STRATEGIES, prepare

TARGET = 0.03


def _frames(cfg: config.Config, offline: bool = True) -> dict[str, pd.DataFrame]:
    return {m: data.load(m, cfg.timeframe, cfg.history_days, offline=offline) for m in cfg.markets}


def _row(name: str, m: dict) -> str:
    return (f"| {name} | {m['total_return']:+.1%} | {m['cagr']:+.1%} | {m['avg_daily']:+.3%} | "
            f"{m['median_daily']:+.3%} | {m['sharpe']:.2f} | {m['max_drawdown']:.1%} | {m['days_ge_3pct']:.1%} | "
            f"{m.get('trades', 0)} | {m.get('win_rate', 0):.0%} |")


HEADER = ("| strategy | total | CAGR | avg/day | median/day | Sharpe | MDD | days ≥ +3% | trades | win |\n"
          "|---|---|---|---|---|---|---|---|---|---|")


def cmd_fetch(cfg, args):
    for m in cfg.markets:
        df = data.load(m, cfg.timeframe, cfg.history_days)
        print(f"{m}: {len(df)} bars {df.index[0]} -> {df.index[-1]}")


def _no_guard(risk):
    return dataclasses.replace(risk, daily_target=None, daily_loss_limit=None, max_drawdown=None, lock_gain=None)


def cmd_backtest(cfg, args):
    frames = _frames(cfg)
    risk = _no_guard(cfg.risk) if args.no_guard else cfg.risk
    params = json.loads(args.params) if args.params else {}
    res = backtest.run({m: prepare(args.strategy, df, params) for m, df in frames.items()}, risk, cfg.costs)
    print(HEADER)
    print(_row(args.strategy, res.metrics()))
    if args.trades:
        print(res.trades.tail(20).to_string())


def cmd_optimize(cfg, args):
    frames = _frames(cfg)
    if args.no_guard:
        cfg.risk = _no_guard(cfg.risk)
    names = list(dict.fromkeys([*(args.strategies or [n for n in STRATEGIES if n != "hold"]), *cfg.strategies]))
    results = {}
    print(f"walk-forward: train {cfg.train_days}d / test {cfg.test_days}d, markets={cfg.markets}")
    for name in names:
        results[name] = walk_forward(frames, name, cfg.risk, cfg.costs, cfg.train_days, cfg.test_days)
        print(f"  {name}: done")
    first_test = min(r["oos_returns"].index[0] for r in results.values())
    bench = backtest.run({m: prepare("hold", df) for m, df in frames.items()},
                         dataclasses.replace(_no_guard(cfg.risk), vol_reweight=False), cfg.costs, start=first_test)

    m = backtest.metrics(backtest.portfolio({s: results[s]["oos_returns"] for s in cfg.strategies}))
    m["trades"] = sum(results[s]["metrics"]["trades"] for s in cfg.strategies)
    m["win_rate"] = sum(results[s]["metrics"]["win_rate"] * results[s]["metrics"]["trades"] for s in cfg.strategies) / max(m["trades"], 1)
    lines = [HEADER] + [_row(n, r["metrics"]) for n, r in results.items()]
    lines.append(_row(f"**portfolio: {' + '.join(cfg.strategies)}**", m))
    lines.append(_row("buy & hold (equal weight)", bench.metrics()))
    table = "\n".join(lines)
    print(table)

    sleeves = {}
    for s in cfg.strategies:
        params, train_sharpe = best_params(frames, s, cfg.risk, cfg.costs, cfg.train_days)
        oos = results[s]["metrics"]
        # same rule as the walk-forward: nothing worked -> no new entries for this sleeve
        sleeves[s] = {"params": params, "tradable": bool(oos["sharpe"] > 0 and train_sharpe > 0),
                      "recent_train_sharpe": round(float(train_sharpe), 2),
                      "oos_metrics": {k: float(v) for k, v in oos.items()}, "windows": results[s]["windows"]}
    summary = {"generated_at": pd.Timestamp.now(tz="UTC").isoformat(), "strategies": cfg.strategies,
               "sleeves": sleeves, "portfolio_oos_metrics": {k: float(v) for k, v in m.items()}}
    Path("results").mkdir(exist_ok=True)
    Path(config.SELECTED).write_text(json.dumps(summary, indent=2, default=str))
    Path("results/walkforward.md").write_text(
        f"# Walk-forward out-of-sample results\n\nGenerated {summary['generated_at']}, markets {cfg.markets}, "
        f"timeframe {cfg.timeframe}m, fee {cfg.costs.fee:.2%} + slippage {cfg.costs.slippage:.2%} per side, "
        f"daily target {cfg.risk.daily_target}, daily loss limit {cfg.risk.daily_loss_limit}.\n\n{table}\n")
    for s, v in sleeves.items():
        state = "trading" if v["tradable"] else "NOT trading new entries (stopped working recently)"
        print(f"sleeve {s}: {v['params']} -> {state}")
    print(f"portfolio out-of-sample average day: {m['avg_daily']:+.3%} (target {TARGET:+.1%}) -> {config.SELECTED}")


UPBIT_ERRORS = {  # error name in Upbit's response -> what to do
    "no_authorization_ip": "업비트 > Open API 관리에서 이 PC/서버의 공인 IP를 등록하세요.",
    "invalid_access_key": "ACCESS 키가 틀렸습니다. 복사할 때 앞뒤 공백이 들어갔는지 확인하세요.",
    "jwt_verification": "SECRET 키가 틀렸습니다. 복사할 때 앞뒤 공백이 들어갔는지 확인하세요.",
    "expired_access_key": "키가 만료되었습니다. 업비트에서 재발급하세요.",
    "out_of_scope": "키 권한이 부족합니다. 자산조회·주문조회·주문하기 권한을 켜세요 (출금 권한은 끄세요).",
}


def diagnose(cfg, client, env) -> tuple[list[str], list[str]]:
    """Everything `run` needs, checked up front. Returns (ok lines, problems). Never prints secrets."""
    ok, problems = [], []
    ok.append("mode: live (실거래)" if cfg.mode == "live" else "mode: paper (모의매매, 실제 주문 없음)")
    ok.append(f"전략: {' + '.join(cfg.strategies)} (예산을 똑같이 나눠 각자 운용)")
    unknown = [s for s in cfg.strategies if s not in STRATEGIES or s == "hold"]
    if unknown or not cfg.strategies:
        problems.append(f"strategies = {cfg.strategies} 확인: {[s for s in STRATEGIES if s != 'hold']} 중에서 고르세요.")
    if cfg.mode == "live" and cfg.budget_krw <= 0:
        problems.append("budget_krw 를 정하세요 (봇이 쓸 원화, 예: 1000000). 계좌의 나머지 돈은 건드리지 않습니다.")
    try:
        client.tickers(cfg.markets)
        ok.append(f"markets: {', '.join(cfg.markets)}")
    except Exception as e:
        problems.append(f"markets 확인 실패 (오타? 예: KRW-BTC): {str(e)[:120]}")

    keys = env.get("UPBIT_ACCESS_KEY"), env.get("UPBIT_SECRET_KEY")
    if not all(keys):
        if cfg.mode == "live":
            problems.append("UPBIT_ACCESS_KEY / UPBIT_SECRET_KEY 가 .env 에 없습니다.")
    else:
        try:
            krw = sum(float(a["balance"]) for a in client.accounts() if a["currency"] == "KRW")
            ok.append(f"업비트 API 키 OK, KRW 잔고 {krw:,.0f}원")
            if cfg.mode == "live" and cfg.budget_krw > krw:
                problems.append(f"budget_krw({cfg.budget_krw:,.0f})가 잔고보다 큽니다.")
        except Exception as e:
            msg = str(e)
            for k in keys:
                msg = msg.replace(k, "***")
            advice = next((v for name, v in UPBIT_ERRORS.items() if name in msg), f"업비트 응답: {msg[:150]}")
            problems.append(f"업비트 API 키 확인 실패: {advice}")
    if env.get("TELEGRAM_BOT_TOKEN") and env.get("TELEGRAM_CHAT_ID"):
        ok.append("텔레그램 알림: 켜짐")
    else:
        ok.append("텔레그램 알림: 꺼짐 (선택, .env 에 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)")
    return ok, problems


def cmd_check(cfg, args):
    from .live import notify
    from .upbit import UpbitClient

    client = UpbitClient(os.environ.get("UPBIT_ACCESS_KEY"), os.environ.get("UPBIT_SECRET_KEY"))
    ok, problems = diagnose(cfg, client, os.environ)
    for line in ok:
        print("  OK  ", line)
    for line in problems:
        print("  FAIL", line)
    if problems:
        raise SystemExit(1)
    notify("tradebot check 통과. 이 메시지가 보이면 알림 설정 완료입니다.")
    print("준비 완료. 실행: python -m tradebot run" + (" --i-understand-the-risk" if cfg.mode == "live" else ""))


def cmd_ui(cfg, args):
    from .ui import serve

    serve(args.port, not args.no_browser)


def cmd_report(cfg, args):
    from .report import report

    print("\n".join(report(cfg.mode, f"logs/{cfg.mode}_equity.csv")))


REOPTIMIZE_DAYS = 7


def selection_age_days(path=config.SELECTED) -> float:
    p = Path(path)
    if not p.exists():
        return float("inf")
    made = pd.Timestamp(json.loads(p.read_text())["generated_at"])
    return (pd.Timestamp.now(tz="UTC") - made) / pd.Timedelta(days=1)


def refresh(cfg) -> list[tuple[str, dict, bool]]:
    """Re-download data and re-run the walk-forward once a week, then load the sleeves."""
    if selection_age_days() >= REOPTIMIZE_DAYS:
        logging.info("selection is older than %d days: fetch + optimize (a few minutes)", REOPTIMIZE_DAYS)
        try:
            cmd_fetch(cfg, None)
            cmd_optimize(cfg, argparse.Namespace(strategies=None, no_guard=False))
        except Exception:
            logging.exception("re-optimisation failed; keeping the previous selection")
    return cfg.sleeves()


def cmd_run(cfg, args):
    from .live import PID_FILE, STOP, STOP_FILE, running_pid
    from .upbit import UpbitClient

    live = cfg.mode == "live"
    if live and not args.i_understand_the_risk:
        raise SystemExit("live mode needs --i-understand-the-risk")
    client = UpbitClient(os.environ.get("UPBIT_ACCESS_KEY"), os.environ.get("UPBIT_SECRET_KEY"))
    if live:
        _, problems = diagnose(cfg, client, os.environ)
        if problems:
            raise SystemExit("python -m tradebot check 를 먼저 통과하세요:\n  " + "\n  ".join(problems))
    if running_pid():  # two processes on the same ledgers would trade twice
        raise SystemExit("봇이 이미 실행 중입니다 (state/bot.pid). 제어판이나 그 창에서 먼저 정지하세요.")
    PID_FILE.parent.mkdir(exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))
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


def _run_loop(cfg, args, live, client):
    from .live import Bot, BotConfig, UpbitBroker, notify, run_bots, stop_requested
    from .report import report

    budget = cfg.budget_krw if live else cfg.paper_krw
    broker = UpbitBroker(client) if live else None  # paper: every sleeve gets its own simulated holdings
    while not stop_requested():
        sleeves = refresh(cfg)
        if not args.once:  # weekly check against the plan's expected ranges
            notify("[tradebot] 주간 리포트\n" + "\n".join(report(cfg.mode, f"logs/{cfg.mode}_equity.csv")))
        bots = [Bot(BotConfig(markets=cfg.markets, strategy=name, params=params, risk=cfg.risk, costs=cfg.costs,
                              timeframe=cfg.timeframe, poll_seconds=cfg.poll_seconds, allow_entries=tradable,
                              state_path=f"state/{cfg.mode}_{name}.json", trades_path=f"logs/{cfg.mode}_trades.csv",
                              equity_path=f"logs/{cfg.mode}_equity.csv"),
                    client, broker=broker, funding=budget / len(sleeves))
                for name, params, tradable in sleeves]
        if args.once:
            return [b.step() for b in bots]
        notify(f"[tradebot] {cfg.mode} 시작, 예산 {budget:,.0f}원을 {len(bots)}개 전략에 나눔: "
               + ", ".join(f"{n}{'' if t else '(신규 진입 중단)'}" for n, _, t in sleeves))
        run_bots(bots, REOPTIMIZE_DAYS * 86400, cfg.poll_seconds)
    notify(f"[tradebot] {cfg.mode} 정지했습니다.")


def main():
    ap = argparse.ArgumentParser(prog="tradebot")
    ap.add_argument("--config", default="config.toml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="verify config, API keys and alerts before running")
    sub.add_parser("report", help="live/paper results vs the plan's expected ranges, and the next step")
    u = sub.add_parser("ui", help="open the control panel in the browser (settings, start/stop, status)")
    u.add_argument("--port", type=int, default=8765)
    u.add_argument("--no-browser", action="store_true")
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
    {"ui": cmd_ui, "check": cmd_check, "report": cmd_report, "fetch": cmd_fetch, "backtest": cmd_backtest, "optimize": cmd_optimize, "run": cmd_run}[args.cmd](cfg, args)


def _file_handler():
    Path("logs").mkdir(exist_ok=True)
    return [logging.FileHandler("logs/bot.log")]


if __name__ == "__main__":
    main()
