# Walk-forward out-of-sample results

Generated 2026-10-03T15:48:05.656684+00:00, markets ['KRW-BTC', 'KRW-ETH', 'KRW-XRP', 'KRW-SOL', 'KRW-DOGE'], timeframe 240m, fee 0.05% + slippage 0.05% per side, daily loss limit 0.05.

| strategy | total | CAGR | avg/day | median/day | Sharpe | MDD | trades | win |
|---|---|---|---|---|---|---|---|---|
| vbo | +34.8% | +6.8% | +0.020% | +0.000% | 0.68 | -15.9% | 1020 | 45% |
| donchian | +314.7% | +37.1% | +0.091% | +0.000% | 1.81 | -12.2% | 198 | 37% |
| rsi_mr | -8.7% | -2.0% | -0.005% | +0.000% | -0.33 | -14.0% | 188 | 62% |
| ema_cross | +118.1% | +18.9% | +0.051% | +0.000% | 1.16 | -12.3% | 113 | 19% |
| **portfolio: donchian + ema_cross** | +216.4% | +29.1% | +0.073% | +0.000% | 1.75 | -9.9% | 311 | 31% |
| buy & hold (equal weight) | +32.7% | +6.5% | +0.061% | +0.051% | 0.39 | -66.8% | 0 | 0% |
