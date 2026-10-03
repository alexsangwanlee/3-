"""The operating plan in code: is the bot inside the ranges the backtest predicts, and what is the next step?

Expected ranges come from results/bands.json (written by scripts/scenarios.py). The phases are in results/plan.md.
"""
import json
from pathlib import Path

import pandas as pd

from . import config

PAPER_DAYS = 28  # Phase 0: paper trading before any real money
GROW_DAYS = 90   # live: earliest budget increase after the last change, and at most 2x


def totals(log: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """Daily ledger equity and money put in, summed over strategies."""
    def total(col):
        return log.pivot_table(index="date", columns="strategy", values=col).ffill().sum(axis=1)
    return total("equity"), total("funded")


def flow_adjusted_returns(log: pd.DataFrame) -> pd.Series:
    """Daily portfolio returns with budget changes (deposits / withdrawals) taken out."""
    eq, funded = totals(log)
    r = ((eq.diff() - funded.diff()) / eq.shift(1)).dropna()
    r.index = pd.to_datetime(r.index)
    return r


def plan_status(log: pd.DataFrame, bands: dict, mode: str) -> list[str]:
    r = flow_adjusted_returns(log)
    days = log["date"].nunique()
    curve = (1 + r).cumprod()
    total = curve.iloc[-1] - 1 if len(r) else 0.0
    dd = curve.iloc[-1] / max(curve.max(), 1.0) - 1 if len(r) else 0.0
    lines = [f"운영 {days}일, 누적 {total:+.1%} (입출금 제외), 고점 대비 {dd:+.1%}"]

    warn = []
    for n, key, label in ((30, "1m_p5", "최근 30일"), (91, "3m_p5", "최근 91일")):
        if len(r) >= n:
            ret = (1 + r.iloc[-n:]).prod() - 1
            lines.append(f"{label} {ret:+.1%} (정상 하한 {bands[key]:+.1%})")
            if ret < bands[key]:
                warn.append(f"{label}이 과거 20번에 1번 수준보다 나쁨")
    if dd < bands["1y_dd_p5"]:
        warn.append(f"낙폭 {dd:.1%}가 정상 범위({bands['1y_dd_p5']:.1%})를 넘음")
    lines.append("점검 필요: " + "; ".join(warn) + " → 모의매매로 돌리고 원인 확인 (results/plan.md 조정 규칙)"
                 if warn else "정상 범위")

    if mode == "paper":
        lines.append(f"Phase 0 모의매매 {days}/{PAPER_DAYS}일" if days < PAPER_DAYS else
                     "Phase 1 진행 가능: 설정에서 실거래로 바꾸고, 실거래 금액을 목표 금액의 10~20%로 맞추세요"
                     if not warn else "모의매매 계속: 점검 항목을 먼저 해결하세요")
    else:
        _, funded = totals(log)
        changed = funded.diff().fillna(0) != 0
        since = days - (int(changed.to_numpy().nonzero()[0][-1]) if changed.any() else 0)
        lines.append("증액 가능: 지난 90일이 정상 범위 → 실거래 금액을 최대 2배까지 (목표 금액 이내)"
                     if since >= GROW_DAYS and not warn else f"현재 예산 유지 ({min(since, GROW_DAYS)}/{GROW_DAYS}일)")
    if len(r) and total > 0 and curve.iloc[-1] >= curve.max():
        lines.append("신고점: 이번 분기 수익의 30~50% 출금 권장 (실거래 금액을 그만큼 낮추고 업비트에서 출금)")
    return lines


def learning(sel: dict | None = None) -> list[str]:
    """What the weekly self-review learned: real costs and a gated strategy recommendation."""
    sel = config.selection() if sel is None else sel
    lines = []
    rev = sel.get("self_review") or {}
    if rev.get("recommend"):
        cand = next(c for c in rev["candidates"] if c["strategies"] == rev["recommend"])
        base = rev["baseline"]
        lines.append(f"자가 점검 추천: 전략을 {' + '.join(rev['recommend'])}(으)로 바꾸면 더 꾸준했습니다 "
                     f"(과거 샤프 {base['sharpe']} → {cand['sharpe']}, 최근 180일 {base['recent']:+.1%} → {cand['recent']:+.1%}). "
                     "제어판에서 승인하면 적용됩니다.")
    costs = sel.get("costs_used") or {}
    if costs.get("slippage", 0) > 0.0005:
        lines.append(f"실제 체결에서 배운 슬리피지 {costs['slippage']:.2%}를 전략 평가에 반영 중입니다.")
    return lines


def report(mode: str, equity_path: str, bands_path: str = "results/bands.json") -> list[str]:
    p = Path(equity_path)
    if not p.exists():
        return ["아직 기록이 없습니다. 봇을 하루 이상 돌린 뒤 다시 보세요.", *learning()]
    return plan_status(pd.read_csv(p), json.loads(Path(bands_path).read_text(encoding="utf-8")), mode) + learning()
