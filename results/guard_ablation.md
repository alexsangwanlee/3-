# Daily guard ablation (walk-forward, out-of-sample)

Same walk-forward as `python -m tradebot optimize` (train 180d / test 60d, Upbit 60m candles
2023-10-04 → 2026-10-03, KRW-BTC/ETH/XRP/SOL/DOGE, fee 0.05% + slippage 0.05% per side),
re-run with each risk-guard combination. Reproduce with `PYTHONPATH=. python scripts/guard_ablation.py donchian vbo ema_cross`.

| strategy | guard | total | CAGR | avg/day | Sharpe | MDD | days ≥ +3% |
|---|---|---|---|---|---|---|---|
| donchian | all on (3% lock, -2% stop, 25% kill) | +15.9% | +6.0% | +0.020% | 0.44 | -23.5% | 2.5% |
| donchian | all off | +103.0% | +32.6% | +0.089% | 1.10 | -28.7% | 3.9% |
| donchian | 3% lock only | +26.0% | +9.6% | +0.029% | 0.66 | -19.5% | 2.8% |
| donchian | -2% daily stop only | +62.6% | +21.4% | +0.062% | 0.86 | -20.6% | 3.3% |
| donchian | 25% kill only | +103.0% | +32.6% | +0.089% | 1.10 | -28.7% | 3.9% |
| donchian | **-5% daily stop + 35% kill (default)** | +99.8% | +31.8% | +0.086% | 1.12 | -23.4% | 3.8% |
| vbo | all on (3% lock, -2% stop, 25% kill) | -9.4% | -3.9% | -0.008% | -0.21 | -23.9% | 1.5% |
| vbo | all off | +14.9% | +5.7% | +0.019% | 0.42 | -17.2% | 1.2% |
| vbo | 3% lock only | +7.3% | +2.9% | +0.010% | 0.28 | -16.8% | 1.4% |
| vbo | -2% daily stop only | +10.9% | +4.2% | +0.015% | 0.33 | -14.1% | 1.2% |
| vbo | -5% daily stop + 35% kill (default) | +14.9% | +5.7% | +0.019% | 0.42 | -17.2% | 1.2% |
| ema_cross | all on (3% lock, -2% stop, 25% kill) | -3.1% | -1.2% | +0.000% | 0.01 | -25.2% | 2.0% |
| ema_cross | all off | +14.2% | +5.4% | +0.023% | 0.33 | -26.3% | 2.4% |
| ema_cross | 3% lock only | -25.0% | -10.9% | -0.028% | -0.64 | -27.7% | 1.6% |
| ema_cross | -2% daily stop only | +53.5% | +18.6% | +0.055% | 0.82 | -24.0% | 2.6% |
| ema_cross | -5% daily stop + 35% kill (default) | +28.4% | +10.5% | +0.035% | 0.54 | -21.6% | 2.4% |

Takeaways

- The **+3% daily profit lock lowered returns for every strategy**. Trend strategies make most of
  their money on a few large days; selling at +3% cuts exactly those days.
- A tight -2% daily stop helps some strategies and hurts others; a loose -5% stop plus a -35%
  drawdown kill switch keeps the upside and trims the worst drawdown, so it is the default.
