import json
import os
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
    budget_krw: float = 0  # live: most KRW the bot may use (0 = whole account)
    poll_seconds: int = 10
    history_days: int = 1825
    train_days: int = 180
    test_days: int = 60

    def resolve_strategy(self) -> tuple[str, dict, bool]:
        """(strategy, params, tradable). `strategy = "auto"` uses what `python -m tradebot optimize` selected;
        tradable=False means it found nothing worth trading: manage open positions, open no new ones."""
        if self.strategy != "auto":
            return self.strategy, self.params, True
        p = Path(SELECTED)
        if not p.exists():
            raise SystemExit(f"{SELECTED} not found: run `python -m tradebot optimize` first")
        sel = json.loads(p.read_text())
        return sel["strategy"], sel["params"], sel.get("tradable", True)


def load_env(path=".env") -> None:
    """KEY=VALUE lines into os.environ. Variables that are already set always win."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def load(path: str = "config.toml") -> Config:
    p = Path(path)
    raw = tomllib.loads(p.read_text()) if p.exists() else {}
    risk = Risk(**raw.pop("risk", {}))
    costs = Costs(**raw.pop("costs", {}))
    return Config(**raw, risk=risk, costs=costs)
