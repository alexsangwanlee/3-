"""Self-improvement with a gate, so the bot learns from new data without learning noise.

1. Costs: the slippage the bot really gets on live fills replaces the assumption when it is worse,
   so every weekly re-optimisation scores strategies with real trading costs.
2. Strategy set: a small fixed menu (add or drop one strategy) is re-scored on the weekly walk-forward
   results. A change is recommended only if it wins on older data AND does not lose on the last 180 days,
   which played no part in choosing it. Nothing changes without the user's approval.
"""
from pathlib import Path

import pandas as pd

from .backtest import metrics, portfolio

MIN_FILLS, MAX_SLIP = 20, 0.005
HOLDOUT_DAYS, MARGIN = 180, 0.15


def learned_slippage(trades_csv) -> float | None:
    """Median slippage of the last 100 live fills (None until there are 20)."""
    p = Path(trades_csv)
    if not p.exists():
        return None
    slip = pd.read_csv(p).get("slip")
    if slip is None:
        return None
    slip = pd.to_numeric(slip, errors="coerce").dropna().tail(100)
    if len(slip) < MIN_FILLS:
        return None
    return float(min(max(slip.median(), 0.0), MAX_SLIP))


def _quarters_up(r: pd.Series) -> float:
    q = (1 + r).cumprod().resample("QE").last()
    return float((q.pct_change().fillna(q.iloc[0] - 1) > 0).mean())


def review(rets: dict[str, pd.Series], current: list[str]) -> dict:
    """Score `current` against adding or dropping one strategy. Returns the recommendation (or None)."""
    menu = [current + [s] for s in rets if s not in current]
    if len(current) > 1:
        menu += [[x for x in current if x != s] for s in current]
    cut = max(r.index.max() for r in rets.values()) - pd.Timedelta(days=HOLDOUT_DAYS)

    def score(names):
        r = portfolio({n: rets[n] for n in names})
        old, recent = r[r.index <= cut], r[r.index > cut]
        return {"sharpe": round(float(metrics(old)["sharpe"]), 2), "quarters_up": round(_quarters_up(old), 2),
                "recent": round(float((1 + recent).prod() - 1), 4)}

    base = score(current)
    rows, best = [], None
    for names in menu:
        s = score(names)
        ok = (s["sharpe"] > 0 and s["sharpe"] >= base["sharpe"] + MARGIN and s["quarters_up"] >= base["quarters_up"]
              and s["recent"] >= base["recent"])
        rows.append({"strategies": names, **s, "passes": ok})
        if ok and (best is None or s["sharpe"] > best[1]["sharpe"]):
            best = (names, s)
    return {"current": current, "baseline": base, "candidates": rows, "holdout_from": str(cut.date()),
            "recommend": best[0] if best else None}
