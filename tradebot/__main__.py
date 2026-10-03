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
    return dataclasses.replace(risk, daily_target=None, daily_loss_limit=None, max_drawdown=None)


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
    bench = backtest.run({m: prepare("hold", df) for m, df in frames.items()}, _no_guard(cfg.risk),
                         cfg.costs, start=first_test)

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
        "strategy": pick["strategy"] if tradable else None, "params": params,
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


def cmd_run(cfg, args):
    from .live import Bot, BotConfig, UpbitBroker
    from .upbit import UpbitClient

    name, params = cfg.resolve_strategy()
    live = cfg.mode == "live"
    if live and not args.i_understand_the_risk:
        raise SystemExit("live mode needs --i-understand-the-risk")
    client = UpbitClient(os.environ.get("UPBIT_ACCESS_KEY"), os.environ.get("UPBIT_SECRET_KEY"))
    bot_cfg = BotConfig(markets=cfg.markets, strategy=name, params=params, risk=cfg.risk, costs=cfg.costs,
                        timeframe=cfg.timeframe, poll_seconds=cfg.poll_seconds,
                        state_path=f"state/{cfg.mode}_state.json", trades_path=f"logs/{cfg.mode}_trades.csv")
    bot = Bot(bot_cfg, client, broker=UpbitBroker(client) if live else None, paper_krw=cfg.paper_krw)
    if args.once:
        bot.step()
    else:
        bot.run_forever()


def main():
    ap = argparse.ArgumentParser(prog="tradebot")
    ap.add_argument("--config", default="config.toml")
    sub = ap.add_subparsers(dest="cmd", required=True)
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
    cfg = config.load(args.config)
    {"fetch": cmd_fetch, "backtest": cmd_backtest, "optimize": cmd_optimize, "run": cmd_run}[args.cmd](cfg, args)


def _file_handler():
    Path("logs").mkdir(exist_ok=True)
    return [logging.FileHandler("logs/bot.log")]


if __name__ == "__main__":
    main()
