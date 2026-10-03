# Coins that suddenly take off: trade them when the rules agree?

Question from the user: also watch coins that suddenly take off (갑자기 뜨는 코인), and trade them when they meet the rules.
Today the bot only trades 5 fixed coins (BTC, ETH, XRP, SOL, DOGE).

## Pre-registration (written and committed before anything was run)

**Surge flag.** Coin m is flagged on UTC day d when all hold:
- its KRW turnover on d is at least 5 × its average daily turnover over the 30 days before d (at least 20 days of history);
- its turnover on d ranks in the top 20 of all non-stablecoin KRW markets that day;
- it is not one of the 5 core coins (those are always traded).

**Variant S.** The 5 core coins plus every coin flagged on any of the previous 7 completed days. Only completed days are used.
- New entries in a flagged coin need the same rules as today: donchian or ema_cross signals, the 3-of-4 confirmation, the same stops,
  giveback guard, daily loss limit and kill switch.
- Allocation is 1/5 of the sleeve per coin, the same as each core coin today. Inverse-volatility weights are taken across the coins
  eligible at that bar.
- A coin that drops out of the window keeps its open position until the normal exit or stop.
- Parameters are re-chosen by the walk-forward (train 180 days, test 60) with the same gate in selection and test.
- **Costs:** surge coins pay 0.3% slippage per side, 6 × the default 0.05%; the core coins keep the default.

**Baseline.** Today's production: 5 coins with the confirmation, default costs.

**Adopt only if all of these hold:**
1. The adoption rule for the current goal (results/edge_study.md), on the development period (test windows before 2026-04-01):
   - net CAGR higher than the baseline;
   - CAGR in both halves at least the baseline's;
   - maximum drawdown no worse than -30%.
2. Cost check: with 0.6% slippage per side on surge coins, the dev CAGR is still higher than the baseline's.
3. Then one look at the lockbox (2026-04-01 onward): its return no more than 2 points below the baseline's.

**Reported but not used to choose** (robustness): a 3-day window instead of 7; a 3 × multiplier instead of 5 ×.

**Gate frequency, checked before the run (turnover only, no returns):** the 5 × rule flags 5,368 coin-days across 250 coins
from 2021-10 to 2026-10; at least one coin is flagged on 85% of days. 99.3% of flagged coin-days have 4h candles in `data/`.

**Known biases, stated in advance**
- Survivorship: Upbit's delisted coins cannot be downloaded. Pump-and-dump coins are the ones most often delisted, so this
  study never sees the worst surges. It flatters S, possibly a lot.
- Upbit's past investment warnings (유의종목) are not available, so the backtest may trade coins that were under a warning.
  A production version would skip coins with a current warning.
- 0.7% of flagged coin-days have no 4h candles and are skipped.

**If S is adopted**, production would scan all KRW markets once a day (daily candles), keep the flagged coins for 7 days,
load their 4h candles, skip coins under an Upbit warning, and trade them through the same sleeves and limits.

## Results

Run with `scripts/surge_study.py`. "Surge-coin trades" counts the closed trades in coins outside the core 5 on the
development period.

| variant | dev total | dev CAGR | CAGR halves | dev MDD | Sharpe | lockbox | surge-coin trades (dev) | verdict |
|---|---|---|---|---|---|---|---|---|
| **baseline: 5 coins (production)** | +197.1% | +31.3% | +34.5% / +28.1% | -9.9% | 1.80 | +6.5% | 0 in 0 coins, 0% won, mean +0.0% |  |
| **S: 5x turnover, top 20, 7 days, 0.3% slippage** | -33.0% | -9.5% | +0.5% / -18.5% | -36.0% | -0.40 | +10.3% | 627 in 141 coins, 19% won, mean -4.5% | reject (higher CAGR, both halves, MDD ≥ -30%) |
| cost check: S with 0.6% slippage | -45.7% | -14.2% | -2.0% / -24.8% | -45.7% | -0.67 | +26.3% | 622 in 139 coins, 18% won, mean -5.4% | reject (higher CAGR, both halves, MDD ≥ -30%) |
| robustness: 3-day window | -0.9% | -0.2% | +17.0% / -14.9% | -33.1% | 0.08 | +11.8% | 437 in 128 coins, 18% won, mean -4.5% | reject (higher CAGR, both halves, MDD ≥ -30%) |
| robustness: 3x turnover | -45.1% | -13.9% | +5.4% / -29.7% | -53.2% | -0.67 | +32.8% | 669 in 143 coins, 19% won, mean -4.1% | reject (higher CAGR, both halves, MDD ≥ -30%) |

**Verdict: rejected. Every variant fails every development rule, even though survivorship bias works in its favour.**
- Variant S loses 33% on the development period, where the 5 coins alone make +197%. Its worst drawdown, -36%, is past
  the -35% kill switch.
- The surge coins themselves are the cause: 627 trades in 141 coins, 19% of them winners, an average of -4.5% per trade
  after costs. The same rules that ride trends on the large coins mostly buy the top of a burst.
- It is not a cost question: even at 0.3% slippage it loses, and at 0.6% it loses more. Neither a shorter window (3 days)
  nor a looser trigger (3 ×) changes the verdict.
- The lockbox was printed for every row and is higher than the baseline's (+10.3% vs +6.5% for S). Six months against
  four years of clear losses is not evidence; by the pre-registered rule the lockbox is only consulted for a variant that
  passed the development period, and none did.

Nothing reaches production.
