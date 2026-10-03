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

Run once, as registered (`scripts/edge_study.py`). Columns are the development period unless noted.

| variant | dev total | dev CAGR | CAGR halves | Sharpe | dev MDD | lockbox | verdict |
|---|---|---|---|---|---|---|---|
| baseline | +167.1% | +27.8% | +34.2% / +21.8% | 1.44 | -16.0% | +0.5% | |
| E1 skip trades fees would eat | +139.5% | +24.4% | +30.7% / +18.4% | 1.33 | -16.9% | +1.9% | reject |
| **E2 confirmed entries (3 of 4)** | **+197.1%** | **+31.3%** | +34.5% / +28.1% | **1.80** | **-9.9%** | **+6.5%** | **PASS, lockbox ok: adopted** |
| E3 choose parameters by CAGR | +171.0% | +28.3% | +35.7% / +21.3% | 1.34 | -16.5% | +1.4% | reject (2nd half) |
| E4 risk 2% per trade | +170.2% | +28.2% | +32.4% / +24.1% | 1.39 | -17.9% | +0.0% | reject (1st half) |
| (reference: 4h rebuilt from 1h, same clock) | +167.1% | +27.8% | +34.2% / +21.8% | 1.44 | -16.0% | +0.4% | |
| E5 clock shifted by 2 hours | +91.6% | +17.6% | +24.7% / +11.0% | 1.05 | -16.5% | +2.0% | reject |

**What it means**
- **Fees.** Skipping trades with tight stops (E1) removed good trades along with expensive ones. Costs are better cut by trading less often in bad conditions, which is what E2 does: 311 trades instead of 478 over the whole period.
- **Confirmed entries (E2), adopted.** It is the only variant that raised profit in both halves.
  - It also cut the worst drawdown from -16% to -10%, and it was the best in the lockbox.
  - It is now in `strategies.prepare()`, so the backtest, the weekly re-optimisation and live trading all use it.
  - Full period with the new rule: +216%, CAGR +29.1%, Sharpe 1.75, MDD -9.9% (before: +165%, 24.2%, 1.33, -16.0%).
- **More profit by more risk (E4) or by choosing parameters for profit (E3)** did not hold up in one of the halves. The risk limits stay as they are.
- **Not trading on everyone's clock (E5)** is rejected, but the run showed something more important about the bot itself. Shifting the 4h bars cut the old bot's return to +92–155%, depending on the shift. Part of the headline result depends on using Upbit's own 00/04/08… UTC bars, which start at 09:00 KST.

**Robustness checks** (run after the verdict and not used to choose)

| check | dev total | Sharpe | dev MDD | lockbox |
|---|---|---|---|---|
| old bot, clock +1h / +2h / +3h | +154.9% / +91.6% / +127.6% | 1.44 / 1.05 / 1.22 | -12.3% / -16.5% / -16.7% | +3.3% / +2.0% / +1.0% |
| **E2 on the +2h clock** | **+123.8%** | **1.41** | **-12.6%** | **+7.6%** |
| E2 with 2 of 4 / 4 of 4 | +164.0% / +18.1% | 1.48 / 0.72 | -14.5% / -4.7% | +6.8% / +0.0% |
| E2 dropping one rule (the other 3 all required): trend / BTC / volume / not chasing | +14% / +45% / +30% / +125% | 0.53 / 1.15 / 0.74 / 1.71 | | |

- E2's gain holds on a shifted clock: +124% against the old bot's +92% on that clock. So it is not an artifact of bar alignment.
- 4 of 4 is too strict, and every single rule carries weight.
- The sharp drop from 3 of 4 to 4 of 4 is a reason to keep watching live results against the plan's normal ranges.
