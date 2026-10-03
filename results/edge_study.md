# Costs, maximum profit, confirmed entries, and not trading like every other bot

The user asked for four things:
- fees taken into account so trades profit after costs;
- maximum profit as the goal instead of a daily target;
- entries confirmed by several rules (확실한 타점);
- a setup that does not trade like every other bot.

Each is turned into a variant of the current bot and judged on the same walk-forward as everything else.

## Where the money goes today
Same walk-forward test windows and parameters, re-run with and without costs (development period to 2026-03-31):

| | total | CAGR | Sharpe |
|---|---|---|---|
| before costs | +196.7% | +27.3% | 1.48 |
| after fees (0.05% per side) | +181.9% | +25.8% | 1.41 |
| after fees and slippage (0.05% per side): **actual** | +168.4% | +24.5% | 1.35 |

Costs take about 28 points of return, about 14% of the profit, split roughly half fees, half slippage.

## Pre-registration (committed before any variant was run)

**New goal: maximum net profit, with a survival limit.**
A variant replaces the current bot only if, on the development period (walk-forward test windows before 2026-04-01):
1. **Net CAGR is higher** than the baseline's, after fees and slippage.
2. **Both halves** of the period have net CAGR ≥ the baseline's, so the gain is not one lucky stretch.
3. **Maximum drawdown is no worse than -30%**, which keeps clear of the -35% kill switch.

Then one look at the lockbox (2026-04-01 onward): the return there must not be more than 2 points below the baseline's.
If several variants pass, their combination is tested the same way and adopted only if it passes too.

E5 is a non-inferiority test. Its benefit (not trading at the same moment as everyone else) cannot show up in candle data. It is adopted if:
- net CAGR ≥ the baseline's minus 1 point;
- MDD is no more than 3 points worse;
- both halves are within 1.5 points of the baseline's CAGR.

**Variants**

| # | Idea | Exact rule |
|---|---|---|
| E1 | Skip trades the fees would eat | No entry when the round-trip cost 2 × (fee + slippage) = 0.2% is more than 10% of the stop distance, i.e. when the stop is closer than 2% below the price |
| E2 | Confirmed entries, 3 of 4 rules | Enter only if at least 3 hold at the previous bar's close: (a) price above its 200-bar EMA; (b) BTC above its 200-bar EMA; (c) the last day's volume ≥ the 30-day daily average; (d) price no more than 3 ATR above its 50-bar EMA (not chasing) |
| E3 | Choose parameters for profit | The walk-forward picks each window's parameters by net CAGR in training instead of Sharpe |
| E4 | Size for profit | Risk 2% of the ledger per trade instead of 1% (still capped by the per-coin allocation) |
| E5 | Not on the same clock as everyone | Build 4h bars from 1h candles shifted by 2 hours (closing at 02/06/10/14/18/22 UTC instead of 00/04/08…) |

**Known limits, stated in advance**
- Five variants means one may pass by luck; rules 2 and 3 and the lockbox are there for that.
- E4 adds risk by design. That is what "maximum profit" means, and the -30% drawdown limit is what keeps it survivable.
- E5's benefit is invisible in backtests; only the absence of harm can be checked.

## Results

(filled in after the run)
