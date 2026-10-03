# Learning from 워뇨띠's published trading record

Reproduce with `PYTHONPATH=. python scripts/wonyotti_study.py`. It downloads into the gitignored `data/wonyotti/`.

## The data

- Published by the account holder on 2026-09-22 in the DCInside 차트 갤러리 (post 5051684).
- BitMEX account `aoa`, 2018-03 → 2021-12: 1,439,207 fills, plus the wallet ledger.
- Archive SHA-256 `b6f1dc7a…01b9a`, which matches the hash recorded by
  [twoimo/aoa-bitmex-analysis](https://github.com/twoimo/aoa-bitmex-analysis).
- That audit matched 81,263 of 81,268 sampled fills against BitMEX's own public trade archive.
- His terms: analysis and your own programs are fine; **do not sell bots or products built on this data**. The raw files are not committed here.
- His own letter: *"매매봇이 실제 수익이 날 것 같을 경우 이미 자료가 있는 제가 먼저 만들어서 돌리고 있었을 것입니다."*

## What his record shows (independently audited)

These facts come from the audit's FINDINGS.md, which labels them "Solid"; the fill count and maker share were re-checked here.

- 80% of profit came from BTC and ETH. Altcoin futures lost money.
- Win rate 67% with a payoff ratio of 0.84: the arithmetic of a scalper, not a trend follower.
- 67% of fills were maker orders. Rebates plus funding made his trading costs a net **credit**; all-taker, his fees would have been about 5× larger.
- About 80% of profit was withdrawn, usually right at a new equity high.
- Activity fell after losing days.

## What this study measured

| | result |
|---|---|
| Hours with a long / flat / short BTC bet | 21% / 36% / 42% (short-biased) |
| Long share after the biggest 24h dumps vs pumps | 26% vs 19%; short share after pumps 49%: he **faded** big moves |
| Copy his hourly positions, long-only, BTC 2018–2021 | +53% (Sharpe 0.32) |
| Copy his long **and** short hourly positions | +111% (Sharpe 0.34) |
| Buy & hold BTC, same period | +302% (Sharpe 0.46) |
| Clone (logistic, market features → "is he long"), AUC on 2018–2020 | 0.63 |
| Same clone, AUC on 2021 (held out) | **0.40**, worse than a coin flip |
| Clone traded on Upbit BTC+ETH 1h, 2022-04 → 2026-03 | **-34.7%**, Sharpe -1.35 |

Note: BitMEX XBTUSD is BTC-margined, and he counted profit in BTC. His bet is therefore measured as
contracts ÷ account value in USD; a raw USD delta would misread a collateral hedge as a short.

## Conclusion

- His edge does not live at a timescale a spot bot can copy. A perfect hourly copy of his positions keeps a small fraction of
  his result, and less than buy & hold. The edge was inside the hour: maker fills, rebates, funding and fast scalping.
  Upbit spot charges the same 0.05% to makers and takers, with no rebate and no shorting.
- His decisions cannot be learned from price features. The clone fails on his own final year, and loses money afterwards.
- Two of his risk habits were tested on top of the bot (see [`optimization.md`](optimization.md) for the setup):

  | habit | Sharpe | MDD | total | verdict |
  |---|---|---|---|---|
  | baseline (4h donchian, union grid + re-weighting) | 1.30 | -13.8% | +167% | |
  | concentrate on BTC+ETH | 1.37 | -13.3% | +101% | worst day -3.2% vs -9.6%; much lower return |
  | halve size after a losing day | 1.02 | -17.4% | +104% | worse; not adopted |

  BTC+ETH-only is a reasonable **lower-risk option**. It is not the default, because it gives up a third of the return.
- His most transferable habit costs nothing to adopt and needs no code: **withdraw part of the profit at every new
  equity high**. He moved about 80% of his profit off the exchange, typically about 10% of equity at a time.
