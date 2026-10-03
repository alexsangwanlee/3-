# Finding the best setup: team experiments (2026-10-03)

## Protocol (fixed before any experiment)

- Data: Upbit 1h candles 2021-10-04 → 2026-10-03, KRW-BTC/ETH/XRP/SOL/DOGE (SOL listed 2021-10-15).
- **Dev period:** walk-forward (train 180d / test 60d) out-of-sample returns up to 2026-03-31 (2022-04 → 2026-03, ~1,460 days).
- **Lockbox:** 2026-04-01 → 2026-10-03. Opened once, at the end, for the final candidate only.
- Pass bar: beat the baseline's Sharpe **and** hold in both halves of the dev period.
- Costs everywhere: 0.05% fee + 0.05% slippage per side; risk defaults (-5% daily stop, -35% kill switch).
- Five workers, one hypothesis each, each capped at ~6 variants to limit multiple testing.

## Dev results (one row per worker's key variants)

| variant | total | CAGR | Sharpe | Sharpe 1st/2nd half | MDD | worst day | verdict |
|---|---|---|---|---|---|---|---|
| baseline donchian 1h | +112.4% | +20.7% | 0.82 | 0.69 / 0.93 | -26.8% | -20.5% | baseline |
| baseline ema_cross 1h | +184.7% | +29.9% | 1.08 | 1.47 / 0.67 | -27.2% | -7.0% | baseline |
| **4h bars**, donchian, existing grid | +202.6% | +31.9% | 1.38 | 1.27 / 1.47 | -14.6% | -7.5% | pass |
| 4h bars, donchian, grid ÷4 | +306.9% | +42.0% | 1.62 | 1.29 / 1.90 | -14.3% | -9.9% | pass |
| 4h bars, ema_cross, existing grid | +167.3% | +27.9% | 1.37 | 1.84 / 0.67 | -13.1% | -6.2% | fails 2nd half |
| **inverse-vol re-weighting**, donchian | +112.3% | +20.7% | 0.91 | 0.83 / 0.97 | -20.1% | -15.4% | pass |
| inverse-vol re-weighting, ema_cross | +225.2% | +34.3% | 1.23 | 1.43 / 1.06 | -19.4% | -7.6% | pass (1st half flat) |
| vol cap 2.5%/day, ema_cross | +107.8% | +20.1% | 0.96 | 1.36 / 0.58 | -18.0% | -5.7% | fail |
| donchian + ema_cross, equal split | +148.6% | +25.6% | 1.03 | 1.22 / 0.84 | -18.9% | -8.5% | steadier, no new edge |
| all 4 strategies, equal split | +65.7% | +13.5% | 0.81 | 1.01 / 0.66 | -16.1% | -6.4% | fail |
| chandelier trailing stop, donchian | +3.9% | +1.0% | 0.14 | -0.15 / 0.38 | -22.7% | -2.3% | fail |
| trailing stop, ema_cross | -26.2% | -7.3% | -0.63 | -0.97 / -0.46 | -30.4% | -2.6% | fail |
| BTC > SMA200 regime filter, donchian | +90.5% | +17.5% | 0.75 | 1.05 / 0.58 | -36.8% | -20.5% | fail |
| BTC > SMA50 regime filter, ema_cross | +34.2% | +7.6% | 0.50 | 1.41 / -0.72 | -24.3% | -6.7% | fail |

Why the losers lost:

- **Trailing stops** cut the few long trends that trend followers live on. Win rate rose; returns collapsed.
- **Regime filters** mostly blocked the first entries of new up-moves, because BTC is still under its average when a breakout fires.
- **A vol cap** is just less exposure. Re-weighting, which keeps the average size at 1, is what helped.

## Combining the winners (dev)

| variant | total | Sharpe | Sharpe 1st/2nd half | MDD | worst day |
|---|---|---|---|---|---|
| 4h donchian, union of both grids | +158.2% | 1.18 | 0.46 / 1.71 | -20.4% | -9.9% |
| 4h donchian, union grid + re-weighting | +167.3% | 1.30 | 0.88 / 1.62 | -13.8% | -9.6% |
| **4h donchian, existing grid + re-weighting (chosen)** | +162.1% | 1.17 | 1.16 / 1.20 | -18.4% | -10.5% |

The union grid did worse than either grid on its own: more choices make the walk-forward pick noisier. To avoid cherry-picking
the best-looking grid, the final candidate was fixed before its run: the smallest change (4h bars, existing grid, re-weighting).

## Lockbox (2026-04-01 → 2026-10-03, opened once)

| | total | Sharpe | MDD | days in market |
|---|---|---|---|---|
| current bot (1h donchian) | -0.7% | -0.14 | -3.1% | 20% |
| chosen candidate (4h + re-weighting) | -3.7% | -1.18 | -3.7% | 18% |
| buy & hold, equal weight | +9.2% | | | 100% |

The lockbox does **not** confirm the improvement. The difference between the two is noise (t = -0.93 on daily
differences), and both lost slightly in a choppy rising market that trend following missed.

The change was adopted on the 4-year dev evidence: stable halves, about half the drawdown, and a smaller worst day.
To revert, set `timeframe = 60` and `vol_reweight = false` in `config.toml`.

## Full-period result of the shipped config (`python -m tradebot optimize`, 4h, re-weighting)

See [`walkforward.md`](walkforward.md): donchian, out-of-sample 2022-04 → 2026-10, +146.5% total, CAGR +22.1%,
Sharpe 1.04, MDD -18.4%. Equal-weight buy & hold made +38.7% with a -66.3% drawdown.
