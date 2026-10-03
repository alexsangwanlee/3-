# More data, newer techniques: does any of it help?

Question from the user: decide on much more data, and compare the newest techniques found on GitHub.
Each idea below is a variant of the current bot (donchian + ema_cross, 4h bars, 5 coins, same risk rules and costs).
Everything is judged on the same walk-forward: each 60-day window trades with parameters chosen on the 180 days before it.

## Pre-registration (written and committed before any variant was run)

**Data**
- Upbit daily candles for all 291 KRW markets (liquidity ranking).
- Upbit 4h candles for every coin that was ever among the 30 most traded.
- Fear & Greed index (alternative.me).
- Binance USDT-perp funding rates per coin (data.binance.vision).
- Binance BTC open interest.
- Kimchi premium: Upbit KRW-BTC vs Binance BTCUSDT × USD/KRW from the ECB.
- Every feature uses data up to the previous bar or day only.

**Variants**

| # | Idea | Where it comes from | Exact rule |
|---|---|---|---|
| V1 | Wider universe | cross-sectional crypto momentum papers, FreqAI `include_corr_pairlist` | Trade the coins ranked in the top 10 (V1a) or top 20 (V1b) by trailing 30-day KRW turnover, re-ranked daily; stablecoins excluded; allocation 1/N per coin; parameters re-chosen by the walk-forward on that universe |
| V2a | Greed gate | Fear & Greed index | No new entries when the previous day's index ≥ 80 |
| V2b | Leverage gate | perp funding | No new entries when BTC's 7-day mean funding > 0.03% per 8h |
| V2c | Kimchi premium gate | Korean retail overheating | No new entries when the previous day's premium > 5% |
| V3 | LightGBM filter | qlib Alpha158 / FreqAI | Gradient-boosted trees on ~30 features (returns, volatility, volume, BTC trend, cross-sectional rank, funding, Fear & Greed, kimchi premium, open interest) from every bar of every universe coin. Label: next 5 days' return > 0. Retrained every window on bars whose label was known before it. Skip entries scored below the training set's 30th percentile |
| V4 | Kronos filter | shiyu-coder/Kronos (AAAI 2026), Kronos-small zero-shot | At every bar where an entry could fire, forecast the next 6 bars (24h) from the last 400 (5 samples, T = 1.0, top_p = 0.9). Skip the entry if the forecast return < 0 |

**Adopt a variant only if, on the development period (walk-forward test windows up to 2026-03-31), all of these hold:**
1. Portfolio Sharpe ≥ baseline + 0.10.
2. Sharpe in both halves ≥ the baseline's.
3. Maximum drawdown no more than 3 percentage points worse.
4. V3 and V4 only: out-of-sample AUC > 0.55 at the decisions they make.

A variant that passes is then checked once on the lockbox (2026-04-01 onward). Its return there must not be more than 2 points below the baseline's.

**Known biases, stated in advance**
- V1 sees only coins still listed today; delisted coins cannot be downloaded from Upbit. This flatters V1.
- Kronos was pre-trained on market data that may include 2022–2025 crypto candles. Its development result is only a screen; the lockbox (after its release) is the real test.
- Four ideas and six variants mean some will look good by chance. That is why rule 2 and the lockbox exist.

## Addendum (written and committed before V1 was run)

After this study was registered, the bot adopted the 3-of-4 entry confirmation and the goal changed to maximum net profit
(results/edge_study.md). So V1 runs twice:
- **V1 as registered:** against the registered baseline (5 coins, no confirmation), with the rule above.
- **V1 with today's rules:** confirmation on, against today's baseline (5 coins with confirmation). It is judged by the
  current goal's rule from results/edge_study.md:
  - net CAGR higher on the development period;
  - both halves' CAGR at least the baseline's;
  - MDD no worse than -30%;
  - then the lockbox return no more than 2 points below the baseline's.

The rotating cast is large: 186 coins have been in the daily top 10 by 30-day turnover at some point, and 244 in the top 20.
Survivorship bias is stronger here than anywhere else in this repo. Coins Upbit delisted (several hundred since 2021,
e.g. LUNA) cannot be downloaded, so V1 never holds the coins that went to zero.

## Results

### V2: data gates (registered rule: Sharpe vs the baseline without confirmation)

| variant | result | verdict |
|---|---|---|
| V2a Fear & Greed ≥ 80 | Sharpe 1.44 -> 1.39 | reject |
| V2b BTC funding > 0.03% per 8h | Sharpe 1.44 -> 1.47, below the +0.10 bar | reject |
| V2c kimchi premium > 5% | Sharpe 1.80, but the 1st half is worse; the gain came from one quarter through a different parameter path, not from the gate | reject (watch) |

### V1: wider universe (top 10 / 20 by trailing 30-day KRW turnover, re-ranked daily)

Run with `scripts/universe_study.py`.

| variant | dev total | Sharpe | halves (Sharpe) | dev CAGR | CAGR halves | dev MDD | lockbox | verdict |
|---|---|---|---|---|---|---|---|---|
| **baseline 5 coins, as registered (no confirmation)** | +167.1% | 1.44 | 1.62 / 1.23 | +27.8% | +34.2% / +21.8% | -16.0% | +0.5% | |
| top 10 by turnover, as registered (no confirmation) | +30.6% | 0.64 | 1.12 / -0.09 | +6.9% | +15.6% / -1.1% | -17.2% | -3.5% | reject (Sharpe +0.10, both halves) |
| top 20 by turnover, as registered (no confirmation) | +1.1% | 0.07 | 0.91 / -0.95 | +0.3% | +8.3% / -7.2% | -20.1% | +0.0% | reject (Sharpe +0.10, both halves, MDD) |
| **baseline 5 coins, today's rules (confirmation)** | +197.1% | 1.80 | 1.88 / 1.70 | +31.3% | +34.5% / +28.1% | -9.9% | +6.5% | |
| top 10 by turnover, today's rules (confirmation) | +9.8% | 0.31 | 0.63 / -0.11 | +2.4% | +6.0% / -1.1% | -16.0% | +2.6% | reject (higher CAGR, both halves) |
| top 20 by turnover, today's rules (confirmation) | +1.9% | 0.10 | 0.58 / -0.49 | +0.5% | +4.3% / -3.2% | -15.4% | +3.3% | reject (higher CAGR, both halves) |

**Verdict: rejected in both forms, by a wide margin, even though survivorship bias works in its favour.**
- With today's rules, the top-10 universe makes +9.8% on the development period against +197.1% for the 5 coins, and loses
  money in the second half. The top-20 universe is flat.
- The same breakout and trend rules that pay on the 5 large coins lose on the coins that rotate into the turnover ranking.
  A reading of the numbers, not a separate test: a coin usually enters the top 10 because of a burst of trading, often near
  its top, and its breakouts fail more often.
- The script printed the lockbox for every row; it played no part in the verdicts (no variant passed the development rule).

### V3, V4

Not run yet. The Kronos forecasts are about two thirds done (`scripts/kronos_forecasts.py` is resumable).
