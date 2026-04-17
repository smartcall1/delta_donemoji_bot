"""StandX Perps REST + WebSocket 클라이언트"""
from __future__ import annotations

import base64
import json
import logging
import asyncio
import time
import uuid
from typing import Callable

import aiohttp
import base58
from nacl.signing import SigningKey

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_DELAY = 2


class StandXClient:
    def __init__(self, base_url: str, jwt_token: str, private_key_hex: str):
        self.base_url = base_url.rstrip("/")
        self.jwt_token = jwt_token
        # 키 포맷 자동 감지: hex(64자) / base64(=포함) / base58
        try:
            key_bytes = bytes.fromhex(private_key_hex)
        except ValueError:
            try:
                key_bytes = base58.b58decode(private_key_hex)
            except Exception:
                key_bytes = base64.b64decode(private_key_hex)
        if len(key_bytes) > 32:
            key_bytes = key_bytes[:32]
        self._signing_key = SigningKey(key_bytes)
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

    def _sign(self, message: str) -> str:
        """Ed25519 서명 → base64"""
        signed = self._signing_key.sign(message.encode("utf-8"))
        return base64.b64encode(signed.signature).decode()

    async def _request(self, method: str, path: str, params: dict | None = None) -> dict:
        # I5: 재시도 로직
        last_err = None
        for attempt in range(MAX_RETRIES):
            try:
                await self._ensure_session()
                url = f"{self.base_url}{path}"
                async with self._session.request(method, url, params=params) as resp:
                    data = await resp.json()
                    if resp.status != 200:
                        logger.error("StandX API error: %s %s → %d %s", method, path, resp.status, data)
                    # 응답이 {"code":0,"data":...} 형식이면 감싸고, 아니면 그대로 반환
                    if isinstance(data, dict) and "code" in data:
                        return data
                    return {"code": 0, "data": data}
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
                sig_id = str(uuid.uuid4())
                timestamp = str(int(time.time() * 1000))
                body_str = json.dumps(body, separators=(",", ":"))
                # 메인 서명: "v1,{uuid},{timestamp},{body}"
                full_msg = f"v1,{sig_id},{timestamp},{body_str}"
                signature = self._sign(full_msg)
                # 바디 단독 서명
                body_sig = self._sign(body_str)
                headers = {
                    "X-Request-Sign-Version": "v1",
                    "X-Request-Id": sig_id,
                    "X-Request-Timestamp": timestamp,
                    "X-Request-Signature": signature,
                    "X-Body-Signature": body_sig,
                    "Content-Type": "application/json",
                }
                async with self._session.request(method, url, data=body_str, headers=headers) as resp:
                    # 응답이 JSON이 아닐 수 있음 (422 등)
                    text = await resp.text()
                    try:
                        data = json.loads(text)
                    except json.JSONDecodeError:
                        if resp.status != 200:
                            logger.error("StandX signed API error: %s %s → %d %s", method, path, resp.status, text[:200])
                            raise Exception(f"StandX {resp.status}: {text[:200]}")
                        data = {"raw": text}
                    if resp.status != 200:
                        logger.error("StandX signed API error: %s %s → %d %s", method, path, resp.status, data)
                    if isinstance(data, dict) and "code" in data:
                        return data
                    return {"code": 0, "data": data}
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
        raw = resp.get("data", [])
        # qty=0인 빈 포지션 껍데기 필터링 (StandX가 레버리지 설정 시 자동 생성)
        return [p for p in raw if float(p.get("qty", 0)) != 0]

    async def place_limit_order(self, symbol: str, side: str, price: float, quantity: float,
                               reduce_only: bool = False) -> dict:
        body = {
            "symbol": symbol,
            "side": side.lower(),
            "order_type": "limit",
            "price": str(price),
            "qty": str(quantity),
            "time_in_force": "gtc",
            "reduce_only": reduce_only,
        }
        resp = await self._signed_request("POST", "/api/new_order", body)
        return resp.get("data", {})

    async def cancel_order(self, order_id: str) -> dict:
        body = {"order_id": order_id}
        resp = await self._signed_request("POST", "/api/cancel_order", body)
        return resp.get("data", {})

    async def cancel_all_orders(self, symbol: str) -> dict:
        """RI-7: 심볼 전체 미체결 주문 취소"""
        body = {"symbol": symbol}
        resp = await self._signed_request("POST", "/api/cancel_all_orders", body)
        return resp.get("data", {})

    async def close_position(self, symbol: str, side: str, quantity: float,
                             slippage_pct: float = 0.005) -> dict:
        """포지션 청산. slippage_pct: 기본 0.5%"""
        close_side = "SELL" if side == "BUY" else "BUY"
        market = await self.get_market_price(symbol)
        price = float(market.get("mark_price", 0))
        if close_side == "BUY":
            price *= (1 + slippage_pct)
        else:
            price *= (1 - slippage_pct)
        # ETH-USD tick size = 0.1
        price = round(price / 0.1) * 0.1
        return await self.place_limit_order(symbol, close_side, round(price, 1), quantity,
                                            reduce_only=True)

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
