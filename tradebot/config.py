import json
import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

from .risk import Costs, Risk

SELECTED = "results/selected.json"


@dataclass
class Config:
    mode: str = "paper"
    markets: list[str] = field(default_factory=lambda: ["KRW-BTC", "KRW-ETH", "KRW-XRP", "KRW-SOL", "KRW-DOGE"])
    timeframe: int = 240
    # Each strategy trades its own equal share of the budget; params are re-tuned weekly by `optimize`.
    strategies: list[str] = field(default_factory=lambda: ["donchian", "ema_cross"])
    risk: Risk = field(default_factory=Risk)
    costs: Costs = field(default_factory=Costs)
    paper_krw: float = 1_000_000
    budget_krw: float = 0  # live: most KRW the bot may use (0 = whole account)
    poll_seconds: int = 10
    history_days: int = 1825
    train_days: int = 180
    test_days: int = 60

    def sleeves(self) -> list[tuple[str, dict, bool]]:
        """[(strategy, params, tradable)] from the last `python -m tradebot optimize`.
        tradable=False: that strategy stopped working recently, so it only manages open positions."""
        p = Path(SELECTED)
        sel = json.loads(p.read_text()).get("sleeves", {}) if p.exists() else {}
        missing = [s for s in self.strategies if s not in sel]
        if missing:
            raise SystemExit(f"{missing} not in {SELECTED}: run `python -m tradebot optimize` first")
        return [(s, sel[s]["params"], sel[s]["tradable"]) for s in self.strategies]


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
    unknown = set(raw) - {f.name for f in fields(Config)}
    if unknown:  # e.g. `strategy` / `params` from older versions
        print(f"{path}: 알 수 없는 설정 무시 {sorted(unknown)} (config.example.toml 참고)")
    return Config(**{k: v for k, v in raw.items() if k not in unknown}, risk=risk, costs=costs)
