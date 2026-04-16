"""텔레그램 알림 + 버튼 UI (폴링 방식)"""
from __future__ import annotations

import json
import logging
import asyncio
from typing import Callable

import aiohttp

logger = logging.getLogger(__name__)


class TelegramUI:
    API = "https://api.telegram.org/bot{token}"

    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self.base = self.API.format(token=token)
        self.enabled = bool(token and chat_id)
        self._session: aiohttp.ClientSession | None = None
        self._offset = 0
        self._callbacks: dict[str, Callable] = {}

    async def _ensure_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    def register_callback(self, key: str, handler: Callable):
        self._callbacks[key] = handler

    async def send_message(self, text: str, buttons: list[list[dict]] | None = None) -> int | None:
        if not self.enabled:
            return None
        await self._ensure_session()
        payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"}
        if buttons:
            payload["reply_markup"] = json.dumps({"inline_keyboard": buttons})
        try:
            async with self._session.post(f"{self.base}/sendMessage", json=payload) as resp:
                data = await resp.json()
                if data.get("ok"):
                    return data["result"]["message_id"]
                logger.error("텔레그램 전송 실패: %s", data)
        except Exception as e:
            logger.error("텔레그램 전송 오류: %s", e)
        return None

    async def send_alert(self, text: str):
        await self.send_message(text)

    async def send_main_menu(self, status_text: str):
        buttons = [
            [
                {"text": "📊 Status", "callback_data": "status"},
                {"text": "📋 History", "callback_data": "history"},
                {"text": "💰 Funding", "callback_data": "funding"},
            ],
            [
                {"text": "🔄 Rebalance", "callback_data": "rebalance"},
                {"text": "⏹ Stop", "callback_data": "stop"},
            ],
        ]
        await self.send_message(status_text, buttons)

    async def answer_callback(self, callback_id: str, text: str = ""):
        if not self.enabled:
            return
        await self._ensure_session()
        payload = {"callback_query_id": callback_id, "text": text}
        try:
            await self._session.post(f"{self.base}/answerCallbackQuery", json=payload)
        except Exception:
            pass

    async def poll_updates(self):
        if not self.enabled:
            return
        await self._ensure_session()
        try:
            params = {"offset": self._offset, "timeout": 5}
            async with self._session.get(f"{self.base}/getUpdates", params=params) as resp:
                data = await resp.json()
                if not data.get("ok"):
                    return
                for update in data.get("result", []):
                    self._offset = update["update_id"] + 1
                    cb = update.get("callback_query")
                    if cb:
                        key = cb.get("data", "")
                        handler = self._callbacks.get(key)
                        if handler:
                            await handler(cb)
                        await self.answer_callback(cb["id"])
        except asyncio.TimeoutError:
            pass
        except Exception as e:
            logger.error("텔레그램 폴링 오류: %s", e)
