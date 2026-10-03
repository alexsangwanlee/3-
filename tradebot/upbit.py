"""Minimal Upbit REST client: public candles/tickers + private orders (JWT HS256)."""
import base64
import hashlib
import hmac
import json
import time
import uuid
from decimal import ROUND_DOWN, Decimal
from urllib.parse import unquote, urlencode

import requests

API = "https://api.upbit.com/v1"
BLOCK_SECONDS = 60  # after a 418 (temporary IP block) with no Retry-After: stay quiet this long
_blocked_until = 0.0  # per process, like Upbit's block (per IP): every client here waits, not just the one blocked


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def make_jwt(payload: dict, secret: str) -> str:
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{header}.{body}"
    sig = hmac.new(secret.encode(), signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url(sig)}"


class UpbitClient:
    def __init__(self, access_key: str | None = None, secret_key: str | None = None, timeout: float = 10):
        self.access_key = access_key
        self.secret_key = secret_key
        self.timeout = timeout
        self.session = requests.Session()

    # -- transport -------------------------------------------------------
    def _headers(self, params: dict | None) -> dict:
        if not (self.access_key and self.secret_key):
            raise RuntimeError("UPBIT_ACCESS_KEY / UPBIT_SECRET_KEY are required for private endpoints")
        payload = {"access_key": self.access_key, "nonce": str(uuid.uuid4())}
        if params:
            query = unquote(urlencode(params, doseq=True)).encode()
            payload["query_hash"] = hashlib.sha512(query).hexdigest()
            payload["query_hash_alg"] = "SHA512"
        return {"Authorization": f"Bearer {make_jwt(payload, self.secret_key)}"}

    def _request(self, method: str, path: str, params: dict | None = None, auth: bool = False):
        global _blocked_until
        if time.time() < _blocked_until:
            raise RuntimeError("Upbit 418: 요청이 너무 많아 잠시 차단됨, 기다리는 중")
        for attempt in range(5):
            headers = self._headers(params) if auth else {}
            if method == "GET":
                r = self.session.get(API + path, params=params, headers=headers, timeout=self.timeout)
            else:
                r = self.session.request(method, API + path, json=params, headers=headers, timeout=self.timeout)
            if r.status_code == 429:  # rate limited
                time.sleep(0.5 * (attempt + 1))
                continue
            if r.status_code == 418:  # temporarily blocked for repeated 429s
                try:
                    wait = float(r.headers.get("Retry-After", BLOCK_SECONDS))
                except ValueError:
                    wait = BLOCK_SECONDS
                _blocked_until = time.time() + wait
                raise RuntimeError(f"Upbit 418: 요청이 너무 많아 {wait:.0f}초 차단됨: {r.text[:200]}")
            if r.status_code >= 400:
                raise RuntimeError(f"Upbit {method} {path} -> {r.status_code}: {r.text[:300]}")
            return r.json()
        raise RuntimeError(f"Upbit {method} {path}: rate limited")

    # -- public ----------------------------------------------------------
    def candles(self, market: str, unit: int, to: str | None = None, count: int = 200) -> list[dict]:
        """Minute candles, newest first. `to` is an exclusive UTC bound like 2024-01-01T00:00:00Z."""
        params = {"market": market, "count": count}
        if to:
            params["to"] = to
        return self._request("GET", f"/candles/minutes/{unit}", params)

    def tickers(self, markets: list[str]) -> dict[str, float]:
        rows = self._request("GET", "/ticker", {"markets": ",".join(markets)})
        return {r["market"]: float(r["trade_price"]) for r in rows}

    def markets(self) -> set[str]:
        return {r["market"] for r in self._request("GET", "/market/all")}

    # -- private ---------------------------------------------------------
    def accounts(self) -> list[dict]:
        return self._request("GET", "/accounts", auth=True)

    # `identifier` is ours and unique forever: an order whose reply was lost can still be looked up by it
    def buy_market(self, market: str, krw: float, identifier: str | None = None, path: str = "/orders") -> dict:
        return self._request("POST", path, {"market": market, "side": "bid", "ord_type": "price",
                                            "price": str(int(krw)), **({"identifier": identifier} if identifier else {})},
                             auth=True)

    def sell_market(self, market: str, volume: float, identifier: str | None = None) -> dict:
        return self._request("POST", "/orders", {"market": market, "side": "ask", "ord_type": "market",
                                                 "volume": str(Decimal(repr(volume)).quantize(Decimal("1e-8"), ROUND_DOWN)),
                                                 **({"identifier": identifier} if identifier else {})}, auth=True)

    def order(self, uuid: str | None = None, identifier: str | None = None) -> dict:
        return self._request("GET", "/order", {"uuid": uuid} if uuid else {"identifier": identifier}, auth=True)
