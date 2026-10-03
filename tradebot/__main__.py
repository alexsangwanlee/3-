"""CLI: python -m tradebot {fetch,backtest,optimize,run}"""
import argparse
import dataclasses
import json
import logging
import os
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
    names = args.strategies or [n for n in STRATEGIES if n != "hold"]
    results = {}
    print(f"walk-forward: train {cfg.train_days}d / test {cfg.test_days}d, markets={cfg.markets}")
    for name in names:
        results[name] = walk_forward(frames, name, cfg.risk, cfg.costs, cfg.train_days, cfg.test_days)
        print(f"  {name}: done")
    first_test = min(r["oos_returns"].index[0] for r in results.values())
    bench = backtest.run({m: prepare("hold", df) for m, df in frames.items()},
                         dataclasses.replace(_no_guard(cfg.risk), vol_reweight=False), cfg.costs, start=first_test)

    lines = [HEADER] + [_row(n, r["metrics"]) for n, r in results.items()]
    lines.append(_row("buy & hold (equal weight)", bench.metrics()))
    table = "\n".join(lines)
    print(table)

    ranked = sorted(results.values(), key=lambda r: r["metrics"]["sharpe"], reverse=True)
    pick = ranked[0]
    params, train_sharpe = best_params(frames, pick["strategy"], cfg.risk, cfg.costs, cfg.train_days)
    m = pick["metrics"]
    # same rule as the walk-forward: nothing worked -> stay in cash
    tradable = m["sharpe"] > 0 and train_sharpe > 0
    summary = {
        "strategy": pick["strategy"], "params": params, "tradable": bool(tradable),
        "recent_train_sharpe": round(float(train_sharpe), 2),
        "oos_metrics": {k: float(v) for k, v in m.items()},
        "windows": pick["windows"],
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
    }
    Path("results").mkdir(exist_ok=True)
    Path(config.SELECTED).write_text(json.dumps(summary, indent=2, default=str))
    Path("results/walkforward.md").write_text(
        f"# Walk-forward out-of-sample results\n\nGenerated {summary['generated_at']}, markets {cfg.markets}, "
        f"timeframe {cfg.timeframe}m, fee {cfg.costs.fee:.2%} + slippage {cfg.costs.slippage:.2%} per side, "
        f"daily target {cfg.risk.daily_target}, daily loss limit {cfg.risk.daily_loss_limit}.\n\n{table}\n")
    if tradable:
        print(f"\nselected: {pick['strategy']} {params} -> {config.SELECTED}")
    else:
        print(f"\nno strategy is profitable out-of-sample / recently -> the bot will stay in cash ({config.SELECTED})")
    print(f"out-of-sample average day: {m['avg_daily']:+.3%} (target {TARGET:+.1%}), "
          f"days that reached +3%: {m['days_ge_3pct']:.1%}")


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
    if cfg.strategy != "auto" and cfg.strategy not in STRATEGIES:
        problems.append(f"strategy = {cfg.strategy!r} 는 없는 전략입니다. auto 또는 {list(STRATEGIES)} 중 하나로.")
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


REOPTIMIZE_DAYS = 7


def selection_age_days(path=config.SELECTED) -> float:
    p = Path(path)
    if not p.exists():
        return float("inf")
    made = pd.Timestamp(json.loads(p.read_text())["generated_at"])
    return (pd.Timestamp.now(tz="UTC") - made) / pd.Timedelta(days=1)


def refresh(cfg) -> tuple[str, dict, bool]:
    """Re-download data and re-run the walk-forward once a week, then load the selection."""
    if cfg.strategy == "auto" and selection_age_days() >= REOPTIMIZE_DAYS:
        logging.info("selection is older than %d days: fetch + optimize (a few minutes)", REOPTIMIZE_DAYS)
        try:
            cmd_fetch(cfg, None)
            cmd_optimize(cfg, argparse.Namespace(strategies=None, no_guard=False))
        except Exception:
            logging.exception("re-optimisation failed; keeping the previous selection")
    return cfg.resolve_strategy()


def cmd_run(cfg, args):
    from .live import Bot, BotConfig, UpbitBroker, notify
    from .upbit import UpbitClient

    live = cfg.mode == "live"
    if live and not args.i_understand_the_risk:
        raise SystemExit("live mode needs --i-understand-the-risk")
    client = UpbitClient(os.environ.get("UPBIT_ACCESS_KEY"), os.environ.get("UPBIT_SECRET_KEY"))
    if live:
        _, problems = diagnose(cfg, client, os.environ)
        if problems:
            raise SystemExit("python -m tradebot check 를 먼저 통과하세요:\n  " + "\n  ".join(problems))
    while True:
        name, params, tradable = refresh(cfg)
        bot_cfg = BotConfig(markets=cfg.markets, strategy=name, params=params, risk=cfg.risk, costs=cfg.costs,
                            timeframe=cfg.timeframe, poll_seconds=cfg.poll_seconds,
                            budget_krw=cfg.budget_krw if live else 0, allow_entries=tradable,
                            state_path=f"state/{cfg.mode}_state.json", trades_path=f"logs/{cfg.mode}_trades.csv")
        bot = Bot(bot_cfg, client, broker=UpbitBroker(client) if live else None, paper_krw=cfg.paper_krw)
        if args.once:
            return bot.step()
        notify(f"[tradebot] {cfg.mode} 시작: {name} {params}" + ("" if tradable else " (신규 진입 없음, 현금 대기)"))
        bot.run_for(REOPTIMIZE_DAYS * 86400)


def main():
    ap = argparse.ArgumentParser(prog="tradebot")
    ap.add_argument("--config", default="config.toml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="verify config, API keys and alerts before running")
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
    {"check": cmd_check, "fetch": cmd_fetch, "backtest": cmd_backtest, "optimize": cmd_optimize, "run": cmd_run}[args.cmd](cfg, args)


def _file_handler():
    Path("logs").mkdir(exist_ok=True)
    return [logging.FileHandler("logs/bot.log")]


if __name__ == "__main__":
    main()
