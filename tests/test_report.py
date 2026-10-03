"""The plan in code: equity log -> are we inside the expected ranges, and which phase are we in?"""
import pandas as pd

from tradebot.report import flow_adjusted_returns, plan_status

BANDS = {"1m_p5": -0.057, "3m_p5": -0.097, "1y_dd_p5": -0.239}


def log(daily_returns, funded=1_000_000, start="2026-01-01", top_up=None):
    """Equity log as the bot writes it: one row per day per sleeve."""
    rows, eq, f = [], funded, funded
    for i, r in enumerate(daily_returns):
        if top_up and i == top_up[0]:
            eq, f = eq + top_up[1], f + top_up[1]
        eq *= 1 + r
        rows.append({"date": str(pd.Timestamp(start) + pd.Timedelta(days=i))[:10], "strategy": "donchian",
                     "equity": eq, "funded": f})
    return pd.DataFrame(rows)


def test_new_money_is_not_counted_as_return():
    r = flow_adjusted_returns(log([0.0] * 10, top_up=(5, 500_000)))
    assert r.abs().max() < 1e-12


def test_paper_phase_counts_down_to_live():
    text = "\n".join(plan_status(log([0.001] * 10), BANDS, "paper"))
    assert "Phase 0" in text and "10/28" in text


def test_paper_phase_done_when_inside_ranges():
    text = "\n".join(plan_status(log([0.0005] * 30), BANDS, "paper"))
    assert "Phase 1" in text and "점검 필요" not in text


def test_a_drawdown_beyond_the_expected_range_asks_for_a_review():
    text = "\n".join(plan_status(log([0.0] * 20 + [-0.02] * 15), BANDS, "live"))  # about -26%
    assert "점검 필요" in text and "증액" not in text


def test_live_budget_can_grow_after_90_good_days():
    text = "\n".join(plan_status(log([0.001] * 95), BANDS, "live"))
    assert "증액 가능" in text
