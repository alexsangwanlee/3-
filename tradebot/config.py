import json
import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

from .risk import Costs, Risk

SELECTED = "state/selected.json"  # written by the weekly re-optimisation (not in git, so `git pull` stays clean)
SEED = "results/selected.json"    # the research snapshot shipped with the code: used until the first re-optimisation


def selection() -> dict:
    for path in (SELECTED, SEED):
        if Path(path).exists():
            return json.loads(Path(path).read_text(encoding="utf-8"))
    return {}


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
        """[(strategy, params, tradable)] from the last `python -m tradebot optimize`, for the strategies it covers.
        tradable=False: that strategy stopped working recently, so it only manages open positions."""
        sel = selection().get("sleeves", {})
        return [(s, sel[s]["params"], sel[s]["tradable"]) for s in self.strategies if s in sel]


def load_env(path=".env") -> None:
    """KEY=VALUE lines into os.environ. Variables that are already set always win."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8-sig").splitlines():  # -sig: Notepad's BOM
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        value = value.split(" #", 1)[0].strip().strip("'\"").strip()  # `KEY=abc   # comment` -> abc
        os.environ.setdefault(key.strip(), value)


def load(path: str = "config.toml") -> Config:
    p = Path(path)
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        risk, costs = Risk(**raw.pop("risk", {})), Costs(**raw.pop("costs", {}))
        unknown = set(raw) - {f.name for f in fields(Config)}
        if unknown:  # e.g. `strategy` / `params` from older versions
            print(f"{path}: 알 수 없는 설정 무시 {sorted(unknown)} (config.example.toml 참고)")
        cfg = Config(**{k: v for k, v in raw.items() if k not in unknown}, risk=risk, costs=costs)
        cfg.budget_krw, cfg.paper_krw = float(cfg.budget_krw), float(cfg.paper_krw)
        cfg.markets = [m.strip().upper() for m in cfg.markets]
    except (tomllib.TOMLDecodeError, TypeError, ValueError, OSError, UnicodeDecodeError, AttributeError) as e:
        raise SystemExit(f"{path} 을 읽지 못했습니다: {e}\n"
                         '  글자는 큰따옴표로 (mode = "live"), 숫자는 따옴표·쉼표 없이 (budget_krw = 1000000).\n'
                         "  모르겠으면 제어판에서 다시 저장하거나 config.example.toml 을 config.toml 로 복사하세요.")
    return cfg
