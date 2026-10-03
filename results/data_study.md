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

## Results

(filled in after the run)
