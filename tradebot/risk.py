"""Risk rules shared by the backtester and the live bot."""
import math
from dataclasses import asdict, dataclass


@dataclass
class Risk:
    # Defaults come from results/guard_ablation.md: a +3% profit lock cut out-of-sample
    # returns for every strategy (it sells the big trend days), so it is off by default.
    daily_target: float | None = None      # e.g. 0.03: +3% on the day -> flatten and stop until tomorrow
    daily_loss_limit: float | None = 0.05  # -5% on the day -> flatten and stop until tomorrow
    max_drawdown: float | None = 0.35      # -35% from the equity peak -> flatten and halt for good
    risk_per_trade: float = 0.01           # max equity lost if a stop-loss is hit
    alloc_per_market: float | None = None  # default: 1 / number of markets


@dataclass
class Costs:
    fee: float = 0.0005       # Upbit KRW market, per side
    slippage: float = 0.0005  # per side


def order_value(equity: float, cash: float, alloc: float, size: float, stop_dist: float,
                price: float, risk_per_trade: float, fee: float) -> float:
    """KRW to spend on a new position."""
    value = equity * alloc * size
    if risk_per_trade and stop_dist and math.isfinite(stop_dist) and stop_dist > 0:
        value = min(value, equity * risk_per_trade * price / stop_dist)
    return max(0.0, min(value, cash / (1 + fee)))


@dataclass
class DailyGuard:
    """Daily profit lock, daily loss limit and the all-time drawdown kill switch."""
    daily_target: float | None = None
    daily_loss_limit: float | None = None
    max_drawdown: float | None = None
    day: str | int | None = None
    day_start: float = 0.0
    peak: float = 0.0
    locked: bool = False
    halted: bool = False

    @classmethod
    def from_risk(cls, risk: Risk) -> "DailyGuard":
        return cls(risk.daily_target, risk.daily_loss_limit, risk.max_drawdown)

    @property
    def can_trade(self) -> bool:
        return not (self.locked or self.halted)

    def on_day(self, day: str | int, equity: float) -> None:
        if day != self.day:
            self.day, self.day_start, self.locked = day, equity, False
        self.peak = max(self.peak, equity)

    def check(self, equity: float) -> str | None:
        """Returns the reason to flatten everything, if any."""
        self.peak = max(self.peak, equity)
        if self.halted:
            return None
        if self.max_drawdown and equity / self.peak - 1 <= -self.max_drawdown:
            self.halted = True
            return "max_drawdown"
        if self.locked or not self.day_start:
            return None
        day_pnl = equity / self.day_start - 1
        if self.daily_target and day_pnl >= self.daily_target:
            self.locked = True
            return "daily_target"
        if self.daily_loss_limit and day_pnl <= -self.daily_loss_limit:
            self.locked = True
            return "daily_loss"
        return None

    def to_dict(self) -> dict:
        return asdict(self)
