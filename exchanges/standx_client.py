"""StandX Perps REST + WebSocket 클라이언트"""
from __future__ import annotations

import json
import logging
import asyncio
from typing import Callable

import aiohttp
from nacl.signing import SigningKey

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_DELAY = 2


class StandXClient:
    def __init__(self, base_url: str, jwt_token: str, private_key_hex: str):
        self.base_url = base_url.rstrip("/")
        self.jwt_token = jwt_token
        self._signing_key = SigningKey(bytes.fromhex(private_key_hex))
        self._session: aiohttp.ClientSession | None = None

    async def _ensure_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {self.jwt_token}"},
                timeout=aiohttp.ClientTimeout(total=15),
            )

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    def _sign_body(self, body: str) -> str:
        signed = self._signing_key.sign(body.encode())
        return signed.signature.hex()

    async def _request(self, method: str, path: str, params: dict | None = None) -> dict:
        # I5: 재시도 로직
        last_err = None
        for attempt in range(MAX_RETRIES):
            try:
                await self._ensure_session()
                url = f"{self.base_url}{path}"
                async with self._session.request(method, url, params=params) as resp:
                    data = await resp.json()
                    if resp.status != 200 or data.get("code") != 0:
                        logger.error("StandX API error: %s %s → %s", method, path, data)
                    return data
            except Exception as e:
                last_err = e
                logger.warning("StandX API 재시도 %d/%d: %s %s → %s", attempt + 1, MAX_RETRIES, method, path, e)
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_DELAY * (attempt + 1))
        raise last_err

    async def _signed_request(self, method: str, path: str, body: dict) -> dict:
        last_err = None
        for attempt in range(MAX_RETRIES):
            try:
                await self._ensure_session()
                url = f"{self.base_url}{path}"
                body_str = json.dumps(body, separators=(",", ":"))
                signature = self._sign_body(body_str)
                headers = {"x-request-signature": signature, "Content-Type": "application/json"}
                async with self._session.request(method, url, data=body_str, headers=headers) as resp:
                    data = await resp.json()
                    if resp.status != 200 or data.get("code") != 0:
                        logger.error("StandX signed API error: %s %s → %s", method, path, data)
                    return data
            except Exception as e:
                last_err = e
                logger.warning("StandX signed API 재시도 %d/%d: %s", attempt + 1, MAX_RETRIES, e)
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_DELAY * (attempt + 1))
        raise last_err

    async def get_market_price(self, symbol: str) -> dict:
        resp = await self._request("GET", "/api/query_symbol_market", {"symbol": symbol})
        return resp.get("data", {})

    async def get_funding_rate(self, symbol: str) -> dict:
        resp = await self._request("GET", "/api/query_symbol_market", {"symbol": symbol})
        return resp.get("data", {})

    async def get_funding_history(self, symbol: str, limit: int = 100) -> list:
        resp = await self._request("GET", "/api/query_funding_rates", {"symbol": symbol, "limit": limit})
        return resp.get("data", [])

    async def get_balance(self) -> dict:
        resp = await self._request("GET", "/api/query_balance")
        return resp.get("data", {})

    async def get_positions(self) -> list:
        resp = await self._request("GET", "/api/query_positions")
        return resp.get("data", [])

    async def place_limit_order(self, symbol: str, side: str, price: float, quantity: float) -> dict:
        body = {
            "symbol": symbol, "side": side, "type": "LIMIT",
            "price": str(price), "quantity": str(quantity), "time_in_force": "GTC",
        }
        resp = await self._signed_request("POST", "/api/new_order", body)
        return resp.get("data", {})

    async def cancel_order(self, order_id: str) -> dict:
        body = {"order_id": order_id}
        resp = await self._signed_request("POST", "/api/cancel_order", body)
        return resp.get("data", {})

    async def close_position(self, symbol: str, side: str, quantity: float,
                             slippage_pct: float = 0.005) -> dict:
        """포지션 청산. slippage_pct: 기본 0.5% (I2: 0.1%→0.5% 확대)"""
        close_side = "SELL" if side == "BUY" else "BUY"
        market = await self.get_market_price(symbol)
        price = float(market.get("mark_price", 0))
        if close_side == "BUY":
            price *= (1 + slippage_pct)
        else:
            price *= (1 - slippage_pct)
        return await self.place_limit_order(symbol, close_side, round(price, 2), quantity)

    async def change_leverage(self, symbol: str, leverage: int) -> dict:
        body = {"symbol": symbol, "leverage": leverage}
        resp = await self._signed_request("POST", "/api/change_leverage", body)
        return resp.get("data", {})


class StandXWSClient:
    def __init__(self, ws_url: str, symbol: str, on_price: Callable[[float], None]):
        self.ws_url = ws_url
        self.symbol = symbol
        self.on_price = on_price
        self._running = False
        self._ws = None

    async def connect(self):
        import websockets
        self._running = True
        retry_delay = 1
        while self._running:
            try:
                async with websockets.connect(self.ws_url, ping_interval=10) as ws:
                    self._ws = ws
                    retry_delay = 1
                    sub_msg = json.dumps({
                        "method": "SUBSCRIBE",
                        "params": [f"price@{self.symbol}"],
                    })
                    await ws.send(sub_msg)
                    logger.info("StandX WS connected: %s", self.symbol)
                    async for msg in ws:
                        try:
                            data = json.loads(msg)
                            # M3: 정확한 필드 접근 (str 포함 검사 제거)
                            mark = data.get("data", {}).get("mark_price")
                            if mark is not None:
                                mark_price = float(mark)
                                if mark_price > 0:
                                    self.on_price(mark_price)
                        except (json.JSONDecodeError, ValueError, KeyError, TypeError):
                            continue
            except Exception as e:
                if not self._running:
                    break
                logger.warning("StandX WS disconnected, retry in %ds: %s", retry_delay, e)
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 60)

    async def disconnect(self):
        self._running = False
        if self._ws:
            await self._ws.close()
