import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .risk import Costs, Risk

SELECTED = "results/selected.json"


@dataclass
class Config:
    mode: str = "paper"
    markets: list[str] = field(default_factory=lambda: ["KRW-BTC", "KRW-ETH", "KRW-XRP", "KRW-SOL", "KRW-DOGE"])
    timeframe: int = 240
    strategy: str = "auto"
    params: dict = field(default_factory=dict)
    risk: Risk = field(default_factory=Risk)
    costs: Costs = field(default_factory=Costs)
    paper_krw: float = 1_000_000
    poll_seconds: int = 10
    history_days: int = 1825
    train_days: int = 180
    test_days: int = 60

    def resolve_strategy(self) -> tuple[str, dict]:
        """`strategy = "auto"` uses whatever `python -m tradebot optimize` selected."""
        if self.strategy != "auto":
            return self.strategy, self.params
        p = Path(SELECTED)
        if not p.exists():
            raise SystemExit(f"{SELECTED} not found: run `python -m tradebot optimize` first")
        sel = json.loads(p.read_text())
        if sel["strategy"] is None:
            raise SystemExit("optimize found no strategy worth trading right now; staying in cash")
        return sel["strategy"], sel["params"]


def load(path: str = "config.toml") -> Config:
    p = Path(path)
    raw = tomllib.loads(p.read_text()) if p.exists() else {}
    risk = Risk(**raw.pop("risk", {}))
    costs = Costs(**raw.pop("costs", {}))
    return Config(**raw, risk=risk, costs=costs)
