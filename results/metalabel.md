# Meta-labeling entry filter: rejected

Question: can a classifier trained on the bot's own past trades skip the entries most likely to lose?
Run once, with every choice fixed before the run, on the same walk-forward windows, parameters, risk rules and costs as production.

## Setup
- **Labels:** win or loss of each out-of-sample baseline trade. Each test window trains only on trades that exited before it starts (at least 40, else unfiltered).
- **Features (8), all up to the previous bar's close:**
  - ATR(14)/close
  - close/EMA50 - 1, close/EMA200 - 1
  - 7- and 30-day return
  - last bar's volume / 180-bar mean volume
  - BTC close/EMA200 - 1
  - signal strength (donchian: close / previous n-bar high - 1; ema_cross: fast/slow EMA - 1)
- **Model:** logistic regression in numpy, L2 penalty λ = 1, solved by Newton's method. Features standardised on the training set.
- **Filter:** skip entries scored below the training set's 30th percentile.
- **Adopt only if all three hold:** portfolio Sharpe beats baseline, both halves improve, and out-of-sample AUC > 0.55.

## Result

| variant | total | CAGR | Sharpe | Sharpe 1st / 2nd half | MDD | positive quarters | trades | win |
|---|---|---|---|---|---|---|---|---|
| donchian, baseline | +182.9% | +25.9% | 1.22 | 0.83 / 1.57 | -17.6% | 42% | 308 | 29.9% |
| donchian, filtered | +179.6% | +25.6% | 1.30 | 0.96 / 1.61 | -15.9% | 42% | 266 | 30.1% |
| ema_cross, baseline | +147.8% | +22.3% | 1.19 | 1.41 / 0.92 | -18.4% | 58% | 170 | 15.3% |
| ema_cross, filtered | +66.6% | +12.0% | 0.87 | 1.16 / 0.46 | -13.1% | 53% | 112 | 18.8% |
| **portfolio, baseline** | +165.3% | +24.2% | **1.33** | 1.26 / 1.44 | -16.0% | 63% | 478 | 24.7% |
| **portfolio, filtered** | +123.1% | +19.5% | **1.27** | 1.16 / 1.41 | -12.2% | 53% | 378 | 26.7% |

- **Pooled AUC:** 0.545 on 429 trades (about 1.4 standard errors above chance). The mean of the per-window AUCs was 0.433. By strategy: donchian 0.496, ema_cross 0.607.
- **Flagged vs kept trades:** the flagged trades did lose more often (17% vs 29% win rate). But one flagged ema_cross trade made +90.6%.
- **Why it fails:** for a trend follower that wins 15% of the time, skipping one big winner costs more than avoiding many small losers.

**Verdict:** it fails all three criteria (Sharpe 1.27 < 1.33, worse in both halves, AUC 0.545 ≤ 0.55), so it is not in the bot.
The only gain was a shallower drawdown (-16.0% → -12.2%), which cost 42 percentage points of return.

**Caveats:**
- The sample is small (about 109 winners), so the AUC is noisy.
- Positions still open at a window's end are not labelled; the baseline has the same gap.
