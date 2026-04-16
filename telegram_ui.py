"""텔레그램 알림 + 전역 키보드 UI (폴링 방식)"""
from __future__ import annotations

import json
import logging
import asyncio
from typing import Callable

import aiohttp

logger = logging.getLogger(__name__)

# 전역 하단 고정 버튼 텍스트
BTN_STATUS = "📊 Status"
BTN_HISTORY = "📋 History"
BTN_FUNDING = "💰 Funding"
BTN_REBALANCE = "🔄 Rebalance"
BTN_STOP = "⏹ Stop"

KEYBOARD = {
    "keyboard": [
        [BTN_STATUS, BTN_HISTORY, BTN_FUNDING],
        [BTN_REBALANCE, BTN_STOP],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}


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
        """버튼 텍스트 → 핸들러 등록"""
        self._callbacks[key] = handler

    async def send_message(self, text: str, with_keyboard: bool = True) -> int | None:
        if not self.enabled:
            return None
        await self._ensure_session()
        payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"}
        if with_keyboard:
            payload["reply_markup"] = json.dumps(KEYBOARD)
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
        await self.send_message(status_text)

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
                    # 텍스트 메시지로 버튼 입력 감지
                    msg = update.get("message")
                    if msg and msg.get("text"):
                        # N-3: 등록된 chat_id만 허용
                        msg_chat = str(msg.get("chat", {}).get("id", ""))
                        if msg_chat != self.chat_id:
                            continue
                        text = msg["text"].strip()
                        handler = self._callbacks.get(text)
                        if handler:
                            await handler(msg)
        except asyncio.TimeoutError:
            pass
        except Exception as e:
            logger.error("텔레그램 폴링 오류: %s", e)
