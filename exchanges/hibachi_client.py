"""Hibachi SDK 래퍼 — REST + WS 마켓 데이터"""
from __future__ import annotations

import os
import logging
import asyncio
from typing import Callable

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_DELAY = 2


def _setup_env(api_key: str, private_key: str, public_key: str, account_id: str):
    os.environ["HIBACHI_API_ENDPOINT_PRODUCTION"] = "https://api.hibachi.xyz"
    os.environ["HIBACHI_DATA_API_ENDPOINT_PRODUCTION"] = "https://data-api.hibachi.xyz"
    os.environ["HIBACHI_API_KEY_PRODUCTION"] = api_key
    os.environ["HIBACHI_PRIVATE_KEY_PRODUCTION"] = private_key
    os.environ["HIBACHI_PUBLIC_KEY_PRODUCTION"] = public_key
    os.environ["HIBACHI_ACCOUNT_ID_PRODUCTION"] = account_id


async def _retry(fn, *args, **kwargs):
    """I5: SDK 호출 재시도 래퍼"""
    last_err = None
    for attempt in range(MAX_RETRIES):
        try:
            return await fn(*args, **kwargs)
        except Exception as e:
            last_err = e
            logger.warning("Hibachi API 재시도 %d/%d: %s", attempt + 1, MAX_RETRIES, e)
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(RETRY_DELAY * (attempt + 1))
    raise last_err


class HibachiClient:
    def __init__(self, api_key: str, private_key: str, public_key: str, account_id: str):
        _setup_env(api_key, private_key, public_key, account_id)
        self._rest = None
        self._initialized = False

    def _ensure_sdk(self):
        if not self._initialized:
            from hibachi.rest import HibachiRestClient
            self._rest = HibachiRestClient()
            self._initialized = True

    async def _get_balance_raw(self) -> dict:
        self._ensure_sdk()
        return await asyncio.to_thread(self._rest.get_capital_balance)

    async def _get_positions_raw(self) -> list:
        self._ensure_sdk()
        info = await asyncio.to_thread(self._rest.get_account_info)
        return info.get("positions", [])

    async def _get_prices_raw(self, symbol: str) -> dict:
        self._ensure_sdk()
        return await asyncio.to_thread(self._rest.get_prices, symbol)

    async def _get_stats_raw(self, symbol: str) -> dict:
        self._ensure_sdk()
        return await asyncio.to_thread(self._rest.get_stats, symbol)

    async def _place_order_raw(self, **kwargs) -> dict:
        self._ensure_sdk()
        return await asyncio.to_thread(self._rest.place_limit_order, **kwargs)

    async def _cancel_order_raw(self, order_id: str) -> dict:
        self._ensure_sdk()
        return await asyncio.to_thread(self._rest.cancel_order, order_id=order_id)

    async def get_balance(self) -> dict:
        return await _retry(self._get_balance_raw)

    async def get_positions(self) -> list:
        return await _retry(self._get_positions_raw)

    async def get_mark_price(self, symbol: str) -> float:
        data = await _retry(self._get_prices_raw, symbol)
        return float(data.get("mark_price", 0))

    async def get_funding_rate(self, symbol: str) -> float:
        data = await _retry(self._get_stats_raw, symbol)
        return float(data.get("funding_rate", 0))

    async def place_limit_order(self, symbol: str, side: str, price: float, size: float) -> dict:
        return await _retry(
            self._place_order_raw,
            symbol=symbol, side=side, price=price, size=size, post_only=True,
        )

    async def cancel_order(self, order_id: str) -> dict:
        return await _retry(self._cancel_order_raw, order_id)

    async def close_position(self, symbol: str, side: str, size: float,
                             slippage_pct: float = 0.005) -> dict:
        """포지션 청산. slippage_pct: 기본 0.5% (I2: 0.1%→0.5% 확대)"""
        close_side = "SELL" if side == "BUY" else "BUY"
        price = await self.get_mark_price(symbol)
        if close_side == "BUY":
            price *= (1 + slippage_pct)
        else:
            price *= (1 - slippage_pct)
        return await self.place_limit_order(symbol, close_side, round(price, 2), size)

    async def close(self):
        pass


class HibachiWSClient:
    def __init__(self, ws_url: str, symbol: str, on_price: Callable[[float], None]):
        self.ws_url = ws_url
        self.symbol = symbol
        self.on_price = on_price
        self._running = False

    async def connect(self):
        self._running = True
        retry_delay = 1
        while self._running:
            try:
                from hibachi.ws import HibachiWSMarketClient
                ws = HibachiWSMarketClient()

                def _on_mark_price(data):
                    try:
                        if data.get("symbol") == self.symbol:
                            mark = float(data.get("mark_price", 0))
                            if mark > 0:
                                self.on_price(mark)
                    except (ValueError, KeyError):
                        pass

                ws.on("mark_price", _on_mark_price)
                ws.subscribe("MARK_PRICE", symbols=[self.symbol])
                logger.info("Hibachi WS connected: %s", self.symbol)
                retry_delay = 1
                await asyncio.to_thread(ws.start)
            except Exception as e:
                if not self._running:
                    break
                logger.warning("Hibachi WS disconnected, retry in %ds: %s", retry_delay, e)
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 60)

    async def disconnect(self):
        self._running = False
