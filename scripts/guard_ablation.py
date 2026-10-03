import sys
from concurrent.futures import ProcessPoolExecutor
from tradebot import data
from tradebot.optimize import walk_forward
from tradebot.risk import Risk, Costs

MARKETS = ["KRW-BTC", "KRW-ETH", "KRW-XRP", "KRW-SOL", "KRW-DOGE"]
VARIANTS = {
    "all on (3% lock, -2% stop, 25% kill)": Risk(0.03, 0.02, 0.25),
    "all off": Risk(None, None, None),
    "no 3% lock (-2% stop, 25% kill)": Risk(None, 0.02, 0.25),
    "3% lock only": Risk(0.03, None, None),
    "-2% daily stop only": Risk(None, 0.02, None),
    "25% kill only": Risk(None, None, 0.25),
    "-5% daily stop + 35% kill": Risk(None, 0.05, 0.35),
}

def job(args):
    name, label = args
    frames = {m: data.load(m, 60, 1095, offline=True) for m in MARKETS}
    m = walk_forward(frames, name, VARIANTS[label], Costs())["metrics"]
    return name, label, m

if __name__ == "__main__":
    names = sys.argv[1:] or ["donchian"]
    with ProcessPoolExecutor(8) as ex:
        for name, label, m in ex.map(job, [(n, v) for n in names for v in VARIANTS]):
            print(f"| {name} | {label} | {m['total_return']:+.1%} | {m['cagr']:+.1%} | {m['avg_daily']:+.3%} | {m['sharpe']:.2f} | {m['max_drawdown']:.1%} | {m['days_ge_3pct']:.1%} |", flush=True)
