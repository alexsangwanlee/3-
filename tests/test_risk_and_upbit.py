import hashlib
from urllib.parse import unquote, urlencode

import jwt
import pytest

from tradebot.risk import DailyGuard, inverse_vol_weights, order_value
from tradebot.upbit import UpbitClient, make_jwt


def test_guard_target_loss_and_reset():
    g = DailyGuard(daily_target=0.03, daily_loss_limit=0.02, max_drawdown=0.25)
    g.on_day("d1", 100)
    assert g.check(102) is None
    assert g.check(103) == "daily_target" and not g.can_trade
    assert g.check(110) is None  # already locked
    g.on_day("d2", 103)
    assert g.can_trade
    assert g.check(100.9) == "daily_loss"
    g.on_day("d3", 100)
    assert g.check(82) == "max_drawdown" and g.halted  # peak 110
    g.on_day("d4", 82)
    assert not g.can_trade


def test_order_value_caps():
    # slot cap: 1M * 20%
    assert order_value(1e6, 1e6, 0.2, 1.0, float("nan"), 100, 0.01, 0) == pytest.approx(2e5)
    # risk cap: lose at most 1% of 1M if the 5% stop hits -> 200k
    assert order_value(1e6, 1e6, 0.5, 1.0, 5.0, 100, 0.01, 0) == pytest.approx(2e5)
    # cash cap incl. fee
    assert order_value(1e6, 1000, 0.5, 1.0, float("nan"), 100, 0.01, 0.0005) == pytest.approx(1000 / 1.0005)


def test_inverse_vol_weights():
    # BTC at 2% daily vol gets twice the weight of DOGE at 4%; mean weight stays 1; unknown vol -> 0
    assert inverse_vol_weights([0.02, 0.04]) == pytest.approx([4 / 3, 2 / 3])
    assert list(inverse_vol_weights([0.02, float("nan")])) == [1.0, 0.0]  # a warming-up market must not block the rest


def test_jwt_matches_pyjwt():
    payload = {"access_key": "a", "nonce": "n", "query_hash": "h", "query_hash_alg": "SHA512"}
    assert jwt.decode(make_jwt(payload, "s" * 32), "s" * 32, algorithms=["HS256"]) == payload


def test_private_request_signs_query_hash():
    c = UpbitClient("ak", "k" * 32)
    params = {"market": "KRW-BTC", "side": "bid", "ord_type": "price", "price": "10000"}
    token = c._headers(params)["Authorization"].removeprefix("Bearer ")
    claims = jwt.decode(token, "k" * 32, algorithms=["HS256"])
    assert claims["access_key"] == "ak"
    assert claims["query_hash"] == hashlib.sha512(unquote(urlencode(params)).encode()).hexdigest()
