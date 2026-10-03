"""Forecast ranges, market regimes, crash days and assumption sensitivity for the shipped config.

    PYTHONPATH=. python scripts/scenarios.py      -> results/scenarios.md

Everything is computed from walk-forward out-of-sample days only (python -m tradebot fetch first).
"""
import dataclasses
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

from tradebot import config, data, optimize, strategies
from tradebot.risk import Costs

CAPITAL = 10_000_000  # KRW, for the forecast table
EVENTS = {"2022-05-09": "LUNA 붕괴", "2022-05-10": "LUNA 붕괴", "2022-05-11": "LUNA 붕괴", "2022-05-12": "LUNA 붕괴",
          "2022-11-08": "FTX 파산", "2022-11-09": "FTX 파산", "2024-12-03": "계엄 선포"}


def frames(cfg, markets=None):
    return {m: data.load(m, cfg.timeframe, cfg.history_days, offline=True) for m in markets or cfg.markets}


def delayed(name, df, params=None):
    """Every order one bar late: a slow PC, a network outage, a missed poll."""
    out = strategies.prepare(name, df, params)
    for c in ("enter", "exit"):
        out[c] = out[c].shift(1, fill_value=False)
    out["entry_stop"] = out["entry_stop"].shift(1)
    return out


def variant(args):
    label, kind = args
    cfg = config.load()
    risk, costs, markets = cfg.risk, cfg.costs, None
    if kind == "fee":
        costs = Costs(fee=0.001, slippage=cfg.costs.slippage)
    elif kind == "slip":
        costs = Costs(fee=cfg.costs.fee, slippage=0.002)
    elif kind == "delay":
        optimize.prepare = delayed
    elif kind == "btceth":
        markets = ["KRW-BTC", "KRW-ETH"]
    elif kind == "nogiveback":
        risk = dataclasses.replace(risk, lock_gain=None)
    r = optimize.walk_forward(frames(cfg, markets), "donchian", risk, costs, cfg.train_days, cfg.test_days)
    return label, r["oos_returns"]


def bootstrap(rets, days, n=10_000, block=20, seed=0):
    """Block bootstrap of daily returns (keeps streaks and calm/volatile spells together)."""
    r, rng = rets.to_numpy(), np.random.default_rng(seed)
    starts = rng.integers(0, len(r) - block, size=(n, -(-days // block)))
    paths = r[(starts[:, :, None] + np.arange(block)).reshape(n, -1)[:, :days]]
    curve = np.cumprod(1 + paths, axis=1)
    worst_dd = (curve / np.maximum.accumulate(np.maximum(curve, 1), axis=1) - 1).min(axis=1)
    return curve[:, -1] - 1, worst_dd


def stats(rets):
    curve = (1 + rets).cumprod()
    return (f"{curve.iloc[-1] - 1:+.1%} | {rets.mean() / rets.std() * np.sqrt(365):.2f} | "
            f"{(curve / curve.cummax().clip(lower=1) - 1).min():.1%}")


def main():
    cfg = config.load()
    kinds = [("기본 (현재 설정)", "base"), ("수수료 0.05% → 0.1%", "fee"), ("슬리피지 0.05% → 0.2%", "slip"),
             ("모든 주문이 한 봉(4시간) 늦게", "delay"), ("BTC·ETH만", "btceth"), ("수익 지키기(giveback) 끔", "nogiveback")]
    with ProcessPoolExecutor(3) as ex:
        res = dict(ex.map(variant, kinds))
    base = res["기본 (현재 설정)"]
    out = [f"# 예측과 변수: 이 봇이 어떻게 움직이는가\n\n생성 {pd.Timestamp.now(tz='UTC'):%Y-%m-%d}, "
           f"{cfg.timeframe}분봉, 시장 {', '.join(cfg.markets)}. 모든 숫자는 walk-forward 표본외 {len(base)}일"
           f" ({base.index[0]:%Y-%m-%d} ~ {base.index[-1]:%Y-%m-%d})에서 계산했습니다.\n"]

    out.append(f"## 1. 예측: {CAPITAL:,}원으로 시작하면\n")
    out.append("과거 표본외 수익을 20일 덩어리로 섞어 1만 번 다시 뽑은 결과입니다(블록 부트스트랩). "
               "미래 보장이 아니라 **과거와 비슷한 시장이 이어질 때의 범위**입니다.\n")
    out.append("| 기간 | 나쁜 경우 (하위 5%) | 하위 25% | 중간값 | 상위 25% | 좋은 경우 (상위 5%) | 손실 확률 | 기간 중 낙폭 -10% 이상 | 낙폭 -20% 이상 |")
    out.append("|---|---|---|---|---|---|---|---|---|")
    ranges = {}
    for days, name in [(30, "1개월"), (91, "3개월"), (365, "1년")]:
        tot, dd = bootstrap(base, days)
        q = np.percentile(tot, [5, 25, 50, 75, 95])
        ranges[days] = (q[0], np.percentile(dd, 5))
        cells = " | ".join(f"{v:+.1%} ({CAPITAL * (1 + v):,.0f}원)" for v in q)
        out.append(f"| {name} | {cells} | {(tot < 0).mean():.0%} | {(dd <= -0.10).mean():.0%} | {(dd <= -0.20).mean():.0%} |")
    out.append("\n**읽는 법:** 한 달 단위로는 절반 가까이가 손실입니다. 수익은 강세장 몇 달에 몰려서 납니다(아래 2번). "
               "손실 달에 봇을 끄면 그 몇 달을 놓칩니다.")
    out.append(f"\n하루 +3%가 30일 이어지면 +143%입니다. 1개월 시뮬레이션 1만 번 중 그 이상은 "
               f"{(bootstrap(base, 30)[0] >= 1.03 ** 30 - 1).mean():.2%}입니다.\n")

    btc = data.load("KRW-BTC", cfg.timeframe, cfg.history_days, offline=True)["close"].resample("D").last()
    trend = btc.pct_change(30).shift(1)  # known at the start of the day
    vol = btc.pct_change().rolling(30).std().shift(1)
    day = pd.DataFrame({"bot": base, "btc": btc.pct_change().reindex(base.index), "trend": trend.reindex(base.index),
                        "vol": vol.reindex(base.index)}).dropna()
    out.append("## 2. 시장 상황별 행동 (변수: 추세와 변동성)\n")
    out.append("| 상황 (직전 30일 BTC) | 일수 | 봇 일평균 | 봇이 포지션을 든 날 | BTC 일평균 |")
    out.append("|---|---|---|---|---|")
    groups = [("강세장 (+10% 초과)", day["trend"] > 0.10), ("횡보장 (±10%)", day["trend"].abs() <= 0.10),
              ("약세장 (-10% 미만)", day["trend"] < -0.10)]
    lo_v, hi_v = day["vol"].quantile([1 / 3, 2 / 3])
    groups += [("변동성 낮음 (하위 1/3)", day["vol"] <= lo_v), ("변동성 높음 (상위 1/3)", day["vol"] >= hi_v)]
    for name, g in groups:
        d = day[g]
        out.append(f"| {name} | {len(d)} | {d['bot'].mean():+.3%} | {(d['bot'] != 0).mean():.0%} | {d['btc'].mean():+.3%} |")

    out.append("\n## 3. 폭락일에 봇은 무엇을 했나 (BTC 최악의 10일)\n")
    out.append("| 날짜 | 사건 | BTC | 봇 |")
    out.append("|---|---|---|---|")
    for t, row in day.nsmallest(10, "btc").iterrows():
        out.append(f"| {t:%Y-%m-%d} | {EVENTS.get(f'{t:%Y-%m-%d}', '')} | {row['btc']:+.1%} | {row['bot']:+.1%} |")

    out.append("\n## 4. 가정이 틀리면? (변수 민감도, 같은 walk-forward를 다시 실행)\n")
    out.append("| 변수 | 누적 | 샤프 | 최대낙폭 |")
    out.append("|---|---|---|---|")
    for label, _ in kinds:
        out.append(f"| {label} | {stats(res[label])} |")

    m1, m12 = ranges[30], ranges[365]
    out.append(f"""
## 5. 행동 지침

| 상황 | 봇이 자동으로 하는 일 | 사용자가 할 일 |
|---|---|---|
| 신고가 돌파 신호 | 시장가 매수. 비중 = 1/5 × 변동성 가중, 손절가 = 진입가 - 3×ATR | 없음 |
| 가격이 손절가에 닿음 | 10초 안에 시장가 매도 | 없음. 손절은 정상 동작입니다 (승률 약 30%, 이익 거래가 손실 거래보다 큼) |
| 진입 후 +30% 이상 오른 뒤 최고가에서 -15% | 매도해서 수익 확정 | 없음 |
| 하루 손실 -5% | 전량 청산, 다음 날(09:00 KST)까지 신규 매매 중단 | 없음 |
| 최고점 대비 -35% | 전량 청산 후 봇 정지, 텔레그램 알림 | 원인 확인 후 `state/live_state.json` 삭제하고 재시작할지 결정 |
| 주간 재최적화에서 매매할 전략 없음 | 신규 진입 중단, 보유 포지션의 손절·청산만 관리 | 없음. 시장이 바뀌면 다음 주에 자동 재개 |
| API·네트워크 오류 | 10초마다 재시도, 첫 오류에 알림 | 알림이 계속 오면 `python -m tradebot check` |
| 한 달 수익률이 {m1[0]:+.1%} 이하 | (자동 대응 없음) | 과거 기준 20달에 1번 오는 나쁜 달입니다. 끄지 마세요. 손절 원칙을 지키는 게 봇의 전부입니다 |
| 1년 안 낙폭이 {m12[1]:.1%}보다 깊어짐 | -35% 전에는 계속 매매 | 과거 기준 20번에 1번보다 나쁜 상황입니다. 점검 신호로 보고, 모의매매로 돌려 원인을 확인하세요 |
| 계좌가 신고점 | (자동 대응 없음) | 수익 일부 출금 (워뇨띠: 수익의 80%를 출금, 한 번에 자산의 약 10%씩) |
""")
    Path("results/scenarios.md").write_text("\n".join(out))
    print("\n".join(out))


if __name__ == "__main__":
    main()
