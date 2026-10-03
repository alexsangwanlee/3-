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

(filled in after the run)
