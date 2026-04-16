# Delta Neutral Bot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** StandX × Hibachi 델타 뉴트럴 봇 — ETH 반대 포지션으로 SIP-2 yield + Hibachi 포인트/Vault 수익 파밍

**Architecture:** 상태머신 기반 사이클 관리 (IDLE→ANALYZE→ENTER→HOLD→EXIT→COOLDOWN). WebSocket으로 실시간 가격 감시, REST로 주문/잔액/펀딩레이트 조회. 기존 Polymarket 봇의 Config/Watchdog/Telegram 패턴 답습.

**Tech Stack:** Python 3.10+, asyncio, aiohttp, websockets, hibachi-xyz SDK, python-dotenv, pytest

**Spec:** `docs/superpowers/specs/2026-04-16-delta-neutral-bot-design.md`

---

## File Structure

```
delta_neutral_bot/
├── config.py                  # .env 기반 설정 로딩
├── models.py                  # 데이터 클래스 (CycleState, Position, FundingSnapshot)
├── run_bot.py                 # Watchdog 엔트리포인트 (자동 재시작)
├── bot_core.py                # 상태머신, 사이클 관리, 메인 루프
├── exchanges/
│   ├── __init__.py
│   ├── standx_client.py       # StandX REST + WS 클라이언트 (JWT + ed25519)
│   └── hibachi_client.py      # Hibachi SDK 래퍼 (REST + WS)
├── strategy.py                # 펀딩레이트 비교, 방향 결정, 전환 판단
├── monitor.py                 # 마진율 감시, DUSD 디페그 감시
├── telegram_ui.py             # 텔레그램 알림 + 버튼 UI
├── tests/
│   ├── __init__.py
│   ├── test_strategy.py       # 펀딩레이트 비교, 방향 결정 테스트
│   ├── test_monitor.py        # 마진율 계산, 경고 레벨 테스트
│   ├── test_models.py         # 데이터 모델 테스트
│   ├── test_bot_core.py       # 상태머신 전환 테스트
│   ├── test_standx_client.py  # StandX API 모킹 테스트
│   └── test_hibachi_client.py # Hibachi API 모킹 테스트
├── logs/                      # 런타임 로그 디렉토리
├── requirements.txt
├── .env.example
└── .gitignore
```

---

## Task 1: 프로젝트 초기화 및 의존성

**Files:**
- Create: `requirements.txt`
- Create: `.env.example`
- Create: `.gitignore`
- Create: `tests/__init__.py`
- Create: `exchanges/__init__.py`

- [ ] **Step 1: requirements.txt 생성**

```txt
python-dotenv>=1.0.0
aiohttp>=3.9.0
websockets>=12.0
hibachi-xyz>=0.1.14
pynacl>=1.5.0
pytest>=8.0.0
pytest-asyncio>=0.23.0
aioresponses>=0.7.6
```

- [ ] **Step 2: .env.example 생성**

```env
# StandX
STANDX_JWT_TOKEN=
STANDX_PRIVATE_KEY=

# Hibachi
HIBACHI_API_KEY=
HIBACHI_PRIVATE_KEY=
HIBACHI_PUBLIC_KEY=
HIBACHI_ACCOUNT_ID=

# 전략
LEVERAGE=3
PAIR_STANDX=ETH-USD
PAIR_HIBACHI=ETH/USDT-P
MIN_HOLD_HOURS=24
MAX_HOLD_DAYS=4
COOLDOWN_HOURS=3
FUNDING_COST_THRESHOLD=0.001
MARGIN_WARNING_PCT=50
MARGIN_EMERGENCY_PCT=33

# 텔레그램
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# 폴링
POLL_BALANCE_SECONDS=300
POLL_FUNDING_SECONDS=3600
```

- [ ] **Step 3: .gitignore 생성**

```
.env
__pycache__/
*.pyc
logs/
.pytest_cache/
*.egg-info/
venv/
```

- [ ] **Step 4: 빈 __init__.py 파일 생성**

`tests/__init__.py`와 `exchanges/__init__.py`에 빈 파일 생성.

- [ ] **Step 5: 의존성 설치 확인**

Run: `pip install -r requirements.txt`
Expected: 모든 패키지 설치 성공

- [ ] **Step 6: 커밋**

```bash
git add requirements.txt .env.example .gitignore tests/__init__.py exchanges/__init__.py
git commit -m "chore: 프로젝트 초기화 — 의존성, env 템플릿, gitignore"
```

---

## Task 2: 데이터 모델 (models.py)

**Files:**
- Create: `models.py`
- Create: `tests/test_models.py`

- [ ] **Step 1: 테스트 작성**

```python
# tests/test_models.py
import pytest
from models import CycleState, BotState, Position, Cycle, FundingSnapshot


def test_cycle_state_values():
    assert CycleState.IDLE == "IDLE"
    assert CycleState.ANALYZE == "ANALYZE"
    assert CycleState.ENTER == "ENTER"
    assert CycleState.HOLD == "HOLD"
    assert CycleState.EXIT == "EXIT"
    assert CycleState.COOLDOWN == "COOLDOWN"


def test_position_creation():
    pos = Position(
        exchange="standx",
        symbol="ETH-USD",
        side="LONG",
        notional=30000.0,
        entry_price=1800.0,
        leverage=3,
        margin=10000.0,
    )
    assert pos.exchange == "standx"
    assert pos.side == "LONG"
    assert pos.notional == 30000.0


def test_position_unrealized_pnl():
    pos = Position(
        exchange="standx",
        symbol="ETH-USD",
        side="LONG",
        notional=30000.0,
        entry_price=1800.0,
        leverage=3,
        margin=10000.0,
    )
    # 가격이 1900으로 올랐을 때 (LONG이니 이익)
    pnl = pos.calc_unrealized_pnl(1900.0)
    # (1900 - 1800) / 1800 * 30000 = 1666.67
    assert abs(pnl - 1666.67) < 1.0

    # 가격이 1700으로 내렸을 때 (LONG이니 손실)
    pnl = pos.calc_unrealized_pnl(1700.0)
    # (1700 - 1800) / 1800 * 30000 = -1666.67
    assert abs(pnl - (-1666.67)) < 1.0


def test_position_unrealized_pnl_short():
    pos = Position(
        exchange="hibachi",
        symbol="ETH/USDT-P",
        side="SHORT",
        notional=30000.0,
        entry_price=1800.0,
        leverage=3,
        margin=10000.0,
    )
    # 가격이 1700으로 내렸을 때 (SHORT이니 이익)
    pnl = pos.calc_unrealized_pnl(1700.0)
    assert abs(pnl - 1666.67) < 1.0


def test_position_margin_ratio():
    pos = Position(
        exchange="standx",
        symbol="ETH-USD",
        side="LONG",
        notional=30000.0,
        entry_price=1800.0,
        leverage=3,
        margin=10000.0,
    )
    # 마진 10000, 노셔널 30000 → 33.3%
    ratio = pos.calc_margin_ratio(1800.0)
    assert abs(ratio - 33.33) < 0.1

    # 가격 15% 하락 → margin = 10000 - 5000 = 5000, ratio = 5000/30000*100 ≈ 16.7%
    # 정확: notional at current = 30000 * (1530/1800) = 25500
    # pnl = (1530-1800)/1800 * 30000 = -4500
    # effective_margin = 10000 - 4500 = 5500
    # ratio = 5500 / 25500 * 100 ≈ 21.6%
    ratio = pos.calc_margin_ratio(1530.0)
    assert ratio > 20.0


def test_cycle_creation():
    cycle = Cycle(cycle_id=1, direction="standx_long_hibachi_short", notional=30000.0)
    assert cycle.cycle_id == 1
    assert cycle.net_funding == 0.0
    assert cycle.fees_paid == 0.0
    assert cycle.exited_at is None


def test_funding_snapshot():
    snap = FundingSnapshot(
        standx_rate=0.001,
        hibachi_rate=0.008,
        timestamp=1713300000.0,
    )
    assert snap.standx_rate == 0.001
    assert snap.hibachi_rate == 0.008


def test_bot_state_serialization():
    state = BotState(
        cycle_state=CycleState.HOLD,
        current_cycle_id=3,
        standx_balance=10150.0,
        hibachi_balance=9820.0,
    )
    d = state.to_dict()
    assert d["cycle_state"] == "HOLD"
    assert d["current_cycle_id"] == 3

    restored = BotState.from_dict(d)
    assert restored.cycle_state == CycleState.HOLD
    assert restored.standx_balance == 10150.0
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `python -m pytest tests/test_models.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'models'`

- [ ] **Step 3: models.py 구현**

```python
# models.py
"""데이터 모델 — Position, Cycle, FundingSnapshot, BotState"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from enum import StrEnum


class CycleState(StrEnum):
    IDLE = "IDLE"
    ANALYZE = "ANALYZE"
    ENTER = "ENTER"
    HOLD = "HOLD"
    EXIT = "EXIT"
    COOLDOWN = "COOLDOWN"


@dataclass
class Position:
    exchange: str          # "standx" | "hibachi"
    symbol: str            # "ETH-USD" | "ETH/USDT-P"
    side: str              # "LONG" | "SHORT"
    notional: float        # 노셔널 (USD)
    entry_price: float     # 진입가
    leverage: int          # 레버리지 배수
    margin: float          # 투입 마진
    opened_at: float = field(default_factory=time.time)

    def calc_unrealized_pnl(self, current_price: float) -> float:
        price_change = (current_price - self.entry_price) / self.entry_price
        if self.side == "SHORT":
            price_change = -price_change
        return price_change * self.notional

    def calc_margin_ratio(self, current_price: float) -> float:
        pnl = self.calc_unrealized_pnl(current_price)
        effective_margin = self.margin + pnl
        current_notional = self.notional * (current_price / self.entry_price)
        if current_notional == 0:
            return 0.0
        return (effective_margin / current_notional) * 100


@dataclass
class Cycle:
    cycle_id: int
    direction: str              # "standx_long_hibachi_short" | "standx_short_hibachi_long"
    notional: float
    entered_at: float = field(default_factory=time.time)
    exited_at: float | None = None
    standx_funding_total: float = 0.0
    hibachi_funding_total: float = 0.0
    net_funding: float = 0.0
    fees_paid: float = 0.0
    sip2_yield_estimate: float = 0.0
    standx_balance_after: float = 0.0
    hibachi_balance_after: float = 0.0

    def to_jsonl(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


@dataclass
class FundingSnapshot:
    standx_rate: float      # StandX 1시간 펀딩레이트
    hibachi_rate: float      # Hibachi 8시간 펀딩레이트
    timestamp: float


@dataclass
class BotState:
    cycle_state: CycleState = CycleState.IDLE
    current_cycle_id: int = 0
    standx_balance: float = 0.0
    hibachi_balance: float = 0.0
    cumulative_funding: float = 0.0
    cumulative_fees: float = 0.0
    total_sip2_estimate: float = 0.0
    weekly_hibachi_volume: float = 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["cycle_state"] = self.cycle_state.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> BotState:
        d = d.copy()
        d["cycle_state"] = CycleState(d["cycle_state"])
        return cls(**d)

    def save(self, path: str = "logs/bot_state.json"):
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)

    @classmethod
    def load(cls, path: str = "logs/bot_state.json") -> BotState:
        with open(path) as f:
            return cls.from_dict(json.load(f))
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `python -m pytest tests/test_models.py -v`
Expected: 모든 테스트 PASS

- [ ] **Step 5: 커밋**

```bash
git add models.py tests/test_models.py
git commit -m "feat: 데이터 모델 구현 — Position, Cycle, FundingSnapshot, BotState"
```

---

## Task 3: 설정 (config.py)

**Files:**
- Create: `config.py`

- [ ] **Step 1: config.py 구현**

```python
# config.py
"""환경변수 기반 설정 로딩"""
import os
from dotenv import load_dotenv

# 프로젝트 루트에서 .env 로딩
_ROOT = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_ROOT, ".env"))


class Config:
    # StandX
    STANDX_JWT_TOKEN: str = os.getenv("STANDX_JWT_TOKEN", "")
    STANDX_PRIVATE_KEY: str = os.getenv("STANDX_PRIVATE_KEY", "")
    STANDX_BASE_URL: str = "https://perps.standx.com"
    STANDX_WS_URL: str = "wss://perps.standx.com/ws-stream/v1"

    # Hibachi
    HIBACHI_API_KEY: str = os.getenv("HIBACHI_API_KEY", "")
    HIBACHI_PRIVATE_KEY: str = os.getenv("HIBACHI_PRIVATE_KEY", "")
    HIBACHI_PUBLIC_KEY: str = os.getenv("HIBACHI_PUBLIC_KEY", "")
    HIBACHI_ACCOUNT_ID: str = os.getenv("HIBACHI_ACCOUNT_ID", "")
    HIBACHI_API_URL: str = "https://api.hibachi.xyz"
    HIBACHI_DATA_URL: str = "https://data-api.hibachi.xyz"
    HIBACHI_WS_URL: str = "wss://data-api.hibachi.xyz/ws/market"

    # 전략
    LEVERAGE: int = int(os.getenv("LEVERAGE", "3"))
    PAIR_STANDX: str = os.getenv("PAIR_STANDX", "ETH-USD")
    PAIR_HIBACHI: str = os.getenv("PAIR_HIBACHI", "ETH/USDT-P")
    MIN_HOLD_HOURS: int = int(os.getenv("MIN_HOLD_HOURS", "24"))
    MAX_HOLD_DAYS: int = int(os.getenv("MAX_HOLD_DAYS", "4"))
    COOLDOWN_HOURS: int = int(os.getenv("COOLDOWN_HOURS", "3"))
    FUNDING_COST_THRESHOLD: float = float(os.getenv("FUNDING_COST_THRESHOLD", "0.001"))
    MARGIN_WARNING_PCT: float = float(os.getenv("MARGIN_WARNING_PCT", "50"))
    MARGIN_EMERGENCY_PCT: float = float(os.getenv("MARGIN_EMERGENCY_PCT", "33"))

    # 유지마진율 (거래소별)
    STANDX_MAINTENANCE_MARGIN: float = 0.0125   # 1.25%
    HIBACHI_MAINTENANCE_MARGIN: float = 0.0467   # 4.67%

    # 텔레그램
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

    # 폴링
    POLL_BALANCE_SECONDS: int = int(os.getenv("POLL_BALANCE_SECONDS", "300"))
    POLL_FUNDING_SECONDS: int = int(os.getenv("POLL_FUNDING_SECONDS", "3600"))

    # 로그
    LOG_DIR: str = os.path.join(_ROOT, "logs")

    @classmethod
    def ensure_dirs(cls):
        os.makedirs(cls.LOG_DIR, exist_ok=True)

    @classmethod
    def validate(cls) -> list[str]:
        errors = []
        if not cls.STANDX_JWT_TOKEN:
            errors.append("STANDX_JWT_TOKEN 미설정")
        if not cls.STANDX_PRIVATE_KEY:
            errors.append("STANDX_PRIVATE_KEY 미설정")
        if not cls.HIBACHI_API_KEY:
            errors.append("HIBACHI_API_KEY 미설정")
        if not cls.HIBACHI_PRIVATE_KEY:
            errors.append("HIBACHI_PRIVATE_KEY 미설정")
        if not cls.TELEGRAM_BOT_TOKEN:
            errors.append("TELEGRAM_BOT_TOKEN 미설정")
        if not cls.TELEGRAM_CHAT_ID:
            errors.append("TELEGRAM_CHAT_ID 미설정")
        return errors
```

- [ ] **Step 2: 커밋**

```bash
git add config.py
git commit -m "feat: Config 클래스 — .env 기반 설정 로딩"
```

---

## Task 4: StandX 클라이언트 (exchanges/standx_client.py)

**Files:**
- Create: `exchanges/standx_client.py`
- Create: `tests/test_standx_client.py`

- [ ] **Step 1: 테스트 작성**

```python
# tests/test_standx_client.py
import pytest
import json
from unittest.mock import AsyncMock, patch, MagicMock
from exchanges.standx_client import StandXClient


@pytest.fixture
def client():
    return StandXClient(
        base_url="https://perps.standx.com",
        jwt_token="test_jwt",
        private_key_hex="a" * 64,  # 32바이트 hex
    )


@pytest.mark.asyncio
async def test_get_balance(client):
    mock_resp = {"code": 0, "data": {"available_balance": "10000.50", "total_balance": "10500.00"}}
    with patch.object(client, "_request", new_callable=AsyncMock, return_value=mock_resp):
        balance = await client.get_balance()
        assert balance["available_balance"] == "10000.50"


@pytest.mark.asyncio
async def test_get_positions(client):
    mock_resp = {"code": 0, "data": [
        {"symbol": "ETH-USD", "side": "LONG", "position_size": "10.0", "entry_price": "1800.0"}
    ]}
    with patch.object(client, "_request", new_callable=AsyncMock, return_value=mock_resp):
        positions = await client.get_positions()
        assert len(positions) == 1
        assert positions[0]["symbol"] == "ETH-USD"


@pytest.mark.asyncio
async def test_get_funding_rate(client):
    mock_resp = {"code": 0, "data": {"funding_rate": "0.0001", "next_funding_time": 1713300000}}
    with patch.object(client, "_request", new_callable=AsyncMock, return_value=mock_resp):
        rate = await client.get_funding_rate("ETH-USD")
        assert rate["funding_rate"] == "0.0001"


@pytest.mark.asyncio
async def test_get_market_price(client):
    mock_resp = {"code": 0, "data": {"mark_price": "1850.50", "index_price": "1849.80"}}
    with patch.object(client, "_request", new_callable=AsyncMock, return_value=mock_resp):
        price = await client.get_market_price("ETH-USD")
        assert price["mark_price"] == "1850.50"


@pytest.mark.asyncio
async def test_place_limit_order(client):
    mock_resp = {"code": 0, "data": {"order_id": "abc123", "status": "NEW"}}
    with patch.object(client, "_signed_request", new_callable=AsyncMock, return_value=mock_resp):
        result = await client.place_limit_order(
            symbol="ETH-USD", side="BUY", price=1800.0, quantity=10.0
        )
        assert result["order_id"] == "abc123"


@pytest.mark.asyncio
async def test_cancel_order(client):
    mock_resp = {"code": 0, "data": {"order_id": "abc123", "status": "CANCELED"}}
    with patch.object(client, "_signed_request", new_callable=AsyncMock, return_value=mock_resp):
        result = await client.cancel_order("abc123")
        assert result["status"] == "CANCELED"


@pytest.mark.asyncio
async def test_close_position(client):
    mock_resp = {"code": 0, "data": {"order_id": "close123", "status": "FILLED"}}
    with patch.object(client, "_signed_request", new_callable=AsyncMock, return_value=mock_resp):
        result = await client.close_position(symbol="ETH-USD", side="BUY", quantity=10.0)
        assert result["order_id"] == "close123"
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `python -m pytest tests/test_standx_client.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: StandX 클라이언트 구현**

```python
# exchanges/standx_client.py
"""StandX Perps REST + WebSocket 클라이언트"""
from __future__ import annotations

import json
import time
import logging
import asyncio
from typing import Callable

import aiohttp
from nacl.signing import SigningKey
from nacl.encoding import HexEncoder

logger = logging.getLogger(__name__)


class StandXClient:
    """StandX REST API 클라이언트 (JWT + ed25519 서명)"""

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
        await self._ensure_session()
        url = f"{self.base_url}{path}"
        async with self._session.request(method, url, params=params) as resp:
            data = await resp.json()
            if resp.status != 200 or data.get("code") != 0:
                logger.error("StandX API error: %s %s → %s", method, path, data)
            return data

    async def _signed_request(self, method: str, path: str, body: dict) -> dict:
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

    # --- 공개 엔드포인트 ---

    async def get_market_price(self, symbol: str) -> dict:
        resp = await self._request("GET", "/api/query_symbol_market", {"symbol": symbol})
        return resp.get("data", {})

    async def get_funding_rate(self, symbol: str) -> dict:
        resp = await self._request("GET", "/api/query_symbol_market", {"symbol": symbol})
        return resp.get("data", {})

    async def get_funding_history(self, symbol: str, limit: int = 100) -> list:
        resp = await self._request("GET", "/api/query_funding_rates", {"symbol": symbol, "limit": limit})
        return resp.get("data", [])

    # --- 인증 엔드포인트 ---

    async def get_balance(self) -> dict:
        resp = await self._request("GET", "/api/query_balance")
        return resp.get("data", {})

    async def get_positions(self) -> list:
        resp = await self._request("GET", "/api/query_positions")
        return resp.get("data", [])

    async def place_limit_order(self, symbol: str, side: str, price: float, quantity: float) -> dict:
        body = {
            "symbol": symbol,
            "side": side,
            "type": "LIMIT",
            "price": str(price),
            "quantity": str(quantity),
            "time_in_force": "GTC",
        }
        resp = await self._signed_request("POST", "/api/new_order", body)
        return resp.get("data", {})

    async def cancel_order(self, order_id: str) -> dict:
        body = {"order_id": order_id}
        resp = await self._signed_request("POST", "/api/cancel_order", body)
        return resp.get("data", {})

    async def close_position(self, symbol: str, side: str, quantity: float) -> dict:
        """포지션 청산 — 반대 방향 Limit 주문"""
        close_side = "SELL" if side == "BUY" else "BUY"
        market = await self.get_market_price(symbol)
        price = float(market.get("mark_price", 0))
        # 슬리피지 0.1% 적용
        if close_side == "BUY":
            price *= 1.001
        else:
            price *= 0.999
        return await self.place_limit_order(symbol, close_side, round(price, 2), quantity)

    async def change_leverage(self, symbol: str, leverage: int) -> dict:
        body = {"symbol": symbol, "leverage": leverage}
        resp = await self._signed_request("POST", "/api/change_leverage", body)
        return resp.get("data", {})


class StandXWSClient:
    """StandX WebSocket — 실시간 가격 스트림"""

    def __init__(self, ws_url: str, symbol: str, on_price: Callable[[float], None]):
        self.ws_url = ws_url
        self.symbol = symbol
        self.on_price = on_price
        self._running = False
        self._ws = None

    async def connect(self):
        """WebSocket 연결 및 price 채널 구독"""
        import websockets
        self._running = True
        retry_delay = 1
        while self._running:
            try:
                async with websockets.connect(self.ws_url, ping_interval=10) as ws:
                    self._ws = ws
                    retry_delay = 1
                    # price 채널 구독
                    sub_msg = json.dumps({
                        "method": "SUBSCRIBE",
                        "params": [f"price@{self.symbol}"],
                    })
                    await ws.send(sub_msg)
                    logger.info("StandX WS 연결 성공: %s", self.symbol)
                    async for msg in ws:
                        try:
                            data = json.loads(msg)
                            if "mark_price" in str(data):
                                mark_price = float(data.get("data", {}).get("mark_price", 0))
                                if mark_price > 0:
                                    self.on_price(mark_price)
                        except (json.JSONDecodeError, ValueError, KeyError):
                            continue
            except Exception as e:
                if not self._running:
                    break
                logger.warning("StandX WS 연결 끊김, %ds 후 재연결: %s", retry_delay, e)
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 60)

    async def disconnect(self):
        self._running = False
        if self._ws:
            await self._ws.close()
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `python -m pytest tests/test_standx_client.py -v`
Expected: 모든 테스트 PASS

- [ ] **Step 5: 커밋**

```bash
git add exchanges/standx_client.py tests/test_standx_client.py
git commit -m "feat: StandX REST+WS 클라이언트 — JWT/ed25519 인증, 주문, 포지션 조회"
```

---

## Task 5: Hibachi 클라이언트 (exchanges/hibachi_client.py)

**Files:**
- Create: `exchanges/hibachi_client.py`
- Create: `tests/test_hibachi_client.py`

- [ ] **Step 1: 테스트 작성**

```python
# tests/test_hibachi_client.py
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from exchanges.hibachi_client import HibachiClient


@pytest.fixture
def client():
    return HibachiClient(
        api_key="test_key",
        private_key="test_private",
        public_key="test_public",
        account_id="test_account",
    )


@pytest.mark.asyncio
async def test_get_balance(client):
    mock_balance = {"available": "10000.00", "total": "10500.00"}
    with patch.object(client, "_get_balance_raw", new_callable=AsyncMock, return_value=mock_balance):
        balance = await client.get_balance()
        assert balance["available"] == "10000.00"


@pytest.mark.asyncio
async def test_get_positions(client):
    mock_positions = [{"symbol": "ETH/USDT-P", "side": "SHORT", "size": "10.0"}]
    with patch.object(client, "_get_positions_raw", new_callable=AsyncMock, return_value=mock_positions):
        positions = await client.get_positions()
        assert len(positions) == 1
        assert positions[0]["side"] == "SHORT"


@pytest.mark.asyncio
async def test_get_mark_price(client):
    mock_prices = {"mark_price": "1850.50"}
    with patch.object(client, "_get_prices_raw", new_callable=AsyncMock, return_value=mock_prices):
        price = await client.get_mark_price("ETH/USDT-P")
        assert price == 1850.50


@pytest.mark.asyncio
async def test_get_funding_rate(client):
    mock_stats = {"funding_rate": "0.0002", "next_funding_time": 1713300000}
    with patch.object(client, "_get_stats_raw", new_callable=AsyncMock, return_value=mock_stats):
        rate = await client.get_funding_rate("ETH/USDT-P")
        assert rate == 0.0002


@pytest.mark.asyncio
async def test_place_limit_order_post_only(client):
    mock_result = {"order_id": "hib123", "status": "OPEN"}
    with patch.object(client, "_place_order_raw", new_callable=AsyncMock, return_value=mock_result):
        result = await client.place_limit_order(
            symbol="ETH/USDT-P", side="SELL", price=1850.0, size=10.0
        )
        assert result["order_id"] == "hib123"
        # post_only 플래그가 전달되었는지 확인
        client._place_order_raw.assert_called_once()
        call_kwargs = client._place_order_raw.call_args
        assert call_kwargs[1].get("post_only") is True or "PostOnly" in str(call_kwargs)
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `python -m pytest tests/test_hibachi_client.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Hibachi 클라이언트 구현**

```python
# exchanges/hibachi_client.py
"""Hibachi SDK 래퍼 — REST + WS 마켓 데이터"""
from __future__ import annotations

import os
import logging
import asyncio
from typing import Callable

logger = logging.getLogger(__name__)

# Hibachi SDK 환경변수 설정 (SDK 초기화 전에 필요)
def _setup_env(api_key: str, private_key: str, public_key: str, account_id: str):
    os.environ["HIBACHI_API_ENDPOINT_PRODUCTION"] = "https://api.hibachi.xyz"
    os.environ["HIBACHI_DATA_API_ENDPOINT_PRODUCTION"] = "https://data-api.hibachi.xyz"
    os.environ["HIBACHI_API_KEY_PRODUCTION"] = api_key
    os.environ["HIBACHI_PRIVATE_KEY_PRODUCTION"] = private_key
    os.environ["HIBACHI_PUBLIC_KEY_PRODUCTION"] = public_key
    os.environ["HIBACHI_ACCOUNT_ID_PRODUCTION"] = account_id


class HibachiClient:
    """Hibachi REST 클라이언트 — 공식 SDK 래핑"""

    def __init__(self, api_key: str, private_key: str, public_key: str, account_id: str):
        _setup_env(api_key, private_key, public_key, account_id)
        self._rest = None
        self._initialized = False

    def _ensure_sdk(self):
        if not self._initialized:
            from hibachi.rest import HibachiRestClient
            self._rest = HibachiRestClient()
            self._initialized = True

    # --- 내부 SDK 호출 (테스트 시 모킹 대상) ---

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

    # --- 공개 인터페이스 ---

    async def get_balance(self) -> dict:
        return await self._get_balance_raw()

    async def get_positions(self) -> list:
        return await self._get_positions_raw()

    async def get_mark_price(self, symbol: str) -> float:
        data = await self._get_prices_raw(symbol)
        return float(data.get("mark_price", 0))

    async def get_funding_rate(self, symbol: str) -> float:
        data = await self._get_stats_raw(symbol)
        return float(data.get("funding_rate", 0))

    async def place_limit_order(self, symbol: str, side: str, price: float, size: float) -> dict:
        return await self._place_order_raw(
            symbol=symbol,
            side=side,
            price=price,
            size=size,
            post_only=True,  # Maker 0% 수수료
        )

    async def cancel_order(self, order_id: str) -> dict:
        return await self._cancel_order_raw(order_id)

    async def close_position(self, symbol: str, side: str, size: float) -> dict:
        """포지션 청산 — 반대 방향 PostOnly Limit"""
        close_side = "SELL" if side == "BUY" else "BUY"
        price = await self.get_mark_price(symbol)
        # 슬리피지 0.1%
        if close_side == "BUY":
            price *= 1.001
        else:
            price *= 0.999
        return await self.place_limit_order(symbol, close_side, round(price, 2), size)

    async def close(self):
        pass  # SDK는 명시적 close 불필요


class HibachiWSClient:
    """Hibachi WebSocket — 실시간 MARK_PRICE 스트림"""

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
                logger.info("Hibachi WS 연결 성공: %s", self.symbol)
                retry_delay = 1

                # SDK의 WS는 블로킹일 수 있으므로 스레드에서 실행
                await asyncio.to_thread(ws.start)
            except Exception as e:
                if not self._running:
                    break
                logger.warning("Hibachi WS 끊김, %ds 후 재연결: %s", retry_delay, e)
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 60)

    async def disconnect(self):
        self._running = False
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `python -m pytest tests/test_hibachi_client.py -v`
Expected: 모든 테스트 PASS

- [ ] **Step 5: 커밋**

```bash
git add exchanges/hibachi_client.py tests/test_hibachi_client.py
git commit -m "feat: Hibachi SDK 래퍼 — REST+WS, PostOnly Limit, 펀딩레이트 조회"
```

---

## Task 6: 전략 모듈 (strategy.py)

**Files:**
- Create: `strategy.py`
- Create: `tests/test_strategy.py`

- [ ] **Step 1: 테스트 작성**

```python
# tests/test_strategy.py
import pytest
from strategy import (
    normalize_funding_to_8h,
    decide_direction,
    should_exit_cycle,
    calc_notional,
)


class TestNormalizeFunding:
    def test_standx_1h_to_8h(self):
        # StandX 1시간 rate를 8시간으로 정규화
        result = normalize_funding_to_8h(0.001, period_hours=1)
        assert abs(result - 0.008) < 1e-9

    def test_hibachi_8h_unchanged(self):
        result = normalize_funding_to_8h(0.008, period_hours=8)
        assert abs(result - 0.008) < 1e-9

    def test_negative_rate(self):
        result = normalize_funding_to_8h(-0.002, period_hours=1)
        assert abs(result - (-0.016)) < 1e-9


class TestDecideDirection:
    def test_standx_long_when_cheaper(self):
        # StandX 롱 비용이 더 낮을 때
        # standx rate 양수 → 롱이 지불, 숏이 수취
        # hibachi rate 양수 → 롱이 지불, 숏이 수취
        direction = decide_direction(
            standx_rate_8h=0.005,   # StandX 롱이 0.5% 지불
            hibachi_rate_8h=0.010,  # Hibachi 롱이 1.0% 지불
        )
        # StandX Long(-0.5%) + Hibachi Short(+1.0%) = +0.5% 수익
        # StandX Short(+0.5%) + Hibachi Long(-1.0%) = -0.5% 비용
        assert direction == "standx_long_hibachi_short"

    def test_standx_short_when_cheaper(self):
        direction = decide_direction(
            standx_rate_8h=0.010,
            hibachi_rate_8h=0.005,
        )
        assert direction == "standx_short_hibachi_long"

    def test_equal_rates_default(self):
        direction = decide_direction(
            standx_rate_8h=0.005,
            hibachi_rate_8h=0.005,
        )
        # 동일하면 StandX Long 기본 (SIP-2는 방향 무관)
        assert direction == "standx_long_hibachi_short"


class TestShouldExitCycle:
    def test_not_enough_hold_time(self):
        result = should_exit_cycle(
            hold_hours=12,
            min_hold_hours=24,
            cumulative_funding_cost=0.005,
            funding_threshold=0.001,
            margin_ratio_min=50.0,
            margin_emergency_pct=33.0,
            max_hold_days=4,
        )
        assert result is False

    def test_exit_on_funding_cost(self):
        result = should_exit_cycle(
            hold_hours=30,
            min_hold_hours=24,
            cumulative_funding_cost=0.002,
            funding_threshold=0.001,
            margin_ratio_min=50.0,
            margin_emergency_pct=33.0,
            max_hold_days=4,
        )
        assert result is True

    def test_exit_on_margin_danger(self):
        result = should_exit_cycle(
            hold_hours=30,
            min_hold_hours=24,
            cumulative_funding_cost=0.0,
            funding_threshold=0.001,
            margin_ratio_min=30.0,
            margin_emergency_pct=33.0,
            max_hold_days=4,
        )
        assert result is True

    def test_exit_on_max_hold(self):
        result = should_exit_cycle(
            hold_hours=100,
            min_hold_hours=24,
            cumulative_funding_cost=0.0,
            funding_threshold=0.001,
            margin_ratio_min=50.0,
            margin_emergency_pct=33.0,
            max_hold_days=4,
        )
        assert result is True

    def test_no_exit_when_healthy(self):
        result = should_exit_cycle(
            hold_hours=30,
            min_hold_hours=24,
            cumulative_funding_cost=0.0005,
            funding_threshold=0.001,
            margin_ratio_min=50.0,
            margin_emergency_pct=33.0,
            max_hold_days=4,
        )
        assert result is False


class TestCalcNotional:
    def test_match_smaller_balance(self):
        notional = calc_notional(
            standx_balance=10150.0,
            hibachi_balance=9820.0,
            leverage=3,
        )
        assert notional == 9820.0 * 3

    def test_equal_balances(self):
        notional = calc_notional(10000.0, 10000.0, 3)
        assert notional == 30000.0
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `python -m pytest tests/test_strategy.py -v`
Expected: FAIL

- [ ] **Step 3: strategy.py 구현**

```python
# strategy.py
"""펀딩레이트 비교, 방향 결정, 전환 판단"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def normalize_funding_to_8h(rate: float, period_hours: int) -> float:
    """펀딩레이트를 8시간 기준으로 정규화"""
    return rate * (8 / period_hours)


def decide_direction(standx_rate_8h: float, hibachi_rate_8h: float) -> str:
    """펀딩레이트 비교 → 유리한 방향 결정.

    양수 펀딩레이트 = 롱이 숏에게 지불.
    옵션A: StandX Long(-standx) + Hibachi Short(+hibachi) → net = hibachi - standx
    옵션B: StandX Short(+standx) + Hibachi Long(-hibachi) → net = standx - hibachi
    net이 큰 쪽 (수익이 많거나 비용이 적은 쪽) 선택.
    """
    option_a = hibachi_rate_8h - standx_rate_8h   # StandX Long + Hibachi Short
    option_b = standx_rate_8h - hibachi_rate_8h   # StandX Short + Hibachi Long

    if option_a >= option_b:
        logger.info("방향 결정: StandX LONG + Hibachi SHORT (net=%.6f)", option_a)
        return "standx_long_hibachi_short"
    else:
        logger.info("방향 결정: StandX SHORT + Hibachi LONG (net=%.6f)", option_b)
        return "standx_short_hibachi_long"


def should_exit_cycle(
    hold_hours: float,
    min_hold_hours: int,
    cumulative_funding_cost: float,
    funding_threshold: float,
    margin_ratio_min: float,
    margin_emergency_pct: float,
    max_hold_days: int,
) -> bool:
    """전환 조건 평가 — 최소 보유시간 경과 후에만 EXIT 허용"""
    if hold_hours < min_hold_hours:
        return False

    # 마진 위험 (보유시간 무관하게 즉시 EXIT... 는 monitor에서 처리)
    if margin_ratio_min <= margin_emergency_pct:
        logger.warning("마진율 위험: %.1f%% <= %.1f%%", margin_ratio_min, margin_emergency_pct)
        return True

    # 누적 펀딩비용 초과
    if cumulative_funding_cost > funding_threshold:
        logger.info("누적 펀딩비용 초과: %.6f > %.6f", cumulative_funding_cost, funding_threshold)
        return True

    # 최대 보유일 초과 (주 2회 사이클 유지)
    if hold_hours > max_hold_days * 24:
        logger.info("최대 보유기간 초과: %.1f시간 > %d일", hold_hours, max_hold_days)
        return True

    return False


def calc_notional(standx_balance: float, hibachi_balance: float, leverage: int) -> float:
    """작은 쪽 잔액에 맞춰 노셔널 계산"""
    return min(standx_balance, hibachi_balance) * leverage
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `python -m pytest tests/test_strategy.py -v`
Expected: 모든 테스트 PASS

- [ ] **Step 5: 커밋**

```bash
git add strategy.py tests/test_strategy.py
git commit -m "feat: 전략 모듈 — 펀딩레이트 비교, 방향 결정, 전환 판단"
```

---

## Task 7: 모니터 모듈 (monitor.py)

**Files:**
- Create: `monitor.py`
- Create: `tests/test_monitor.py`

- [ ] **Step 1: 테스트 작성**

```python
# tests/test_monitor.py
import pytest
from monitor import MarginLevel, check_margin_level, estimate_sip2_yield


class TestCheckMarginLevel:
    def test_normal(self):
        level = check_margin_level(margin_ratio=60.0, warning_pct=50.0, emergency_pct=33.0)
        assert level == MarginLevel.NORMAL

    def test_warning(self):
        level = check_margin_level(margin_ratio=45.0, warning_pct=50.0, emergency_pct=33.0)
        assert level == MarginLevel.WARNING

    def test_emergency(self):
        level = check_margin_level(margin_ratio=30.0, warning_pct=50.0, emergency_pct=33.0)
        assert level == MarginLevel.EMERGENCY

    def test_boundary_warning(self):
        level = check_margin_level(margin_ratio=50.0, warning_pct=50.0, emergency_pct=33.0)
        assert level == MarginLevel.WARNING

    def test_boundary_emergency(self):
        level = check_margin_level(margin_ratio=33.0, warning_pct=50.0, emergency_pct=33.0)
        assert level == MarginLevel.EMERGENCY


class TestEstimateSip2Yield:
    def test_basic_estimation(self):
        # 잔액 변동에서 펀딩+수수료를 빼면 SIP-2 추정
        yield_est = estimate_sip2_yield(
            current_balance=10200.0,
            initial_balance=10000.0,
            cumulative_funding=150.0,    # 펀딩 수취
            cumulative_fees=-30.0,       # 수수료 지불
            realized_pnl=0.0,
        )
        # 10200 - 10000 - 150 - (-30) - 0 = 80
        assert abs(yield_est - 80.0) < 0.01

    def test_negative_yield_clamped(self):
        # SIP-2가 음수일 순 없으니 0으로 클램프
        yield_est = estimate_sip2_yield(
            current_balance=10000.0,
            initial_balance=10100.0,
            cumulative_funding=0.0,
            cumulative_fees=-10.0,
            realized_pnl=0.0,
        )
        assert yield_est == 0.0
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `python -m pytest tests/test_monitor.py -v`
Expected: FAIL

- [ ] **Step 3: monitor.py 구현**

```python
# monitor.py
"""마진율 감시, SIP-2 수익 추정"""
from __future__ import annotations

import logging
from enum import StrEnum

logger = logging.getLogger(__name__)


class MarginLevel(StrEnum):
    NORMAL = "NORMAL"
    WARNING = "WARNING"
    EMERGENCY = "EMERGENCY"


def check_margin_level(
    margin_ratio: float,
    warning_pct: float,
    emergency_pct: float,
) -> MarginLevel:
    """마진율 기반 위험 레벨 판단"""
    if margin_ratio <= emergency_pct:
        return MarginLevel.EMERGENCY
    elif margin_ratio <= warning_pct:
        return MarginLevel.WARNING
    return MarginLevel.NORMAL


def estimate_sip2_yield(
    current_balance: float,
    initial_balance: float,
    cumulative_funding: float,
    cumulative_fees: float,
    realized_pnl: float,
) -> float:
    """SIP-2 yield 역산 추정.

    SIP-2 = 현재잔액 - 시작잔액 - 누적펀딩수취 - 누적수수료(음수) - 실현PnL
    음수면 0으로 클램프 (SIP-2가 마이너스일 순 없음).
    """
    balance_change = current_balance - initial_balance
    explained = cumulative_funding + cumulative_fees + realized_pnl
    yield_estimate = balance_change - explained
    return max(yield_estimate, 0.0)
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `python -m pytest tests/test_monitor.py -v`
Expected: 모든 테스트 PASS

- [ ] **Step 5: 커밋**

```bash
git add monitor.py tests/test_monitor.py
git commit -m "feat: 모니터 모듈 — 마진율 위험 레벨, SIP-2 역산 추정"
```

---

## Task 8: 텔레그램 UI (telegram_ui.py)

**Files:**
- Create: `telegram_ui.py`

- [ ] **Step 1: telegram_ui.py 구현**

```python
# telegram_ui.py
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
        """텔레그램 업데이트 폴링 — 콜백 버튼 처리"""
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
```

- [ ] **Step 2: 커밋**

```bash
git add telegram_ui.py
git commit -m "feat: 텔레그램 UI — 버튼 메뉴, 알림, 콜백 폴링"
```

---

## Task 9: 봇 코어 — 상태머신 (bot_core.py)

**Files:**
- Create: `bot_core.py`
- Create: `tests/test_bot_core.py`

- [ ] **Step 1: 상태머신 전환 테스트 작성**

```python
# tests/test_bot_core.py
import pytest
import time
from unittest.mock import AsyncMock, patch, MagicMock
from models import CycleState, BotState
from bot_core import DeltaNeutralBot


@pytest.fixture
def mock_bot():
    bot = DeltaNeutralBot.__new__(DeltaNeutralBot)
    bot.state = BotState(cycle_state=CycleState.IDLE)
    bot.standx = AsyncMock()
    bot.hibachi = AsyncMock()
    bot.telegram = AsyncMock()
    bot.telegram.send_alert = AsyncMock()
    bot.telegram.send_main_menu = AsyncMock()
    bot.standx_price = 1800.0
    bot.hibachi_price = 1800.0
    bot._cycle_entered_at = None
    bot._cooldown_until = None
    bot._positions = {}
    return bot


class TestStateTransitions:
    @pytest.mark.asyncio
    async def test_idle_to_analyze(self, mock_bot):
        assert mock_bot.state.cycle_state == CycleState.IDLE
        mock_bot.state.cycle_state = CycleState.ANALYZE
        assert mock_bot.state.cycle_state == CycleState.ANALYZE

    @pytest.mark.asyncio
    async def test_cooldown_not_expired(self, mock_bot):
        mock_bot.state.cycle_state = CycleState.COOLDOWN
        mock_bot._cooldown_until = time.time() + 3600  # 1시간 후
        # 쿨다운 중이면 IDLE로 전환하면 안 됨
        should_transition = time.time() >= mock_bot._cooldown_until
        assert should_transition is False

    @pytest.mark.asyncio
    async def test_cooldown_expired(self, mock_bot):
        mock_bot.state.cycle_state = CycleState.COOLDOWN
        mock_bot._cooldown_until = time.time() - 1  # 이미 만료
        should_transition = time.time() >= mock_bot._cooldown_until
        assert should_transition is True


class TestDirectionParsing:
    def test_parse_standx_long(self):
        direction = "standx_long_hibachi_short"
        standx_side = "BUY" if "standx_long" in direction else "SELL"
        hibachi_side = "SELL" if "hibachi_short" in direction else "BUY"
        assert standx_side == "BUY"
        assert hibachi_side == "SELL"

    def test_parse_standx_short(self):
        direction = "standx_short_hibachi_long"
        standx_side = "BUY" if "standx_long" in direction else "SELL"
        hibachi_side = "SELL" if "hibachi_short" in direction else "BUY"
        assert standx_side == "SELL"
        assert hibachi_side == "BUY"
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `python -m pytest tests/test_bot_core.py -v`
Expected: FAIL

- [ ] **Step 3: bot_core.py 구현**

```python
# bot_core.py
"""상태머신 기반 델타 뉴트럴 봇 코어"""
from __future__ import annotations

import os
import json
import time
import logging
import asyncio
from datetime import datetime, timezone

from config import Config
from models import CycleState, BotState, Position, Cycle, FundingSnapshot
from exchanges.standx_client import StandXClient, StandXWSClient
from exchanges.hibachi_client import HibachiClient, HibachiWSClient
from strategy import normalize_funding_to_8h, decide_direction, should_exit_cycle, calc_notional
from monitor import MarginLevel, check_margin_level, estimate_sip2_yield
from telegram_ui import TelegramUI

logger = logging.getLogger(__name__)


class DeltaNeutralBot:
    def __init__(self):
        Config.ensure_dirs()

        # 거래소 클라이언트
        self.standx = StandXClient(
            base_url=Config.STANDX_BASE_URL,
            jwt_token=Config.STANDX_JWT_TOKEN,
            private_key_hex=Config.STANDX_PRIVATE_KEY,
        )
        self.hibachi = HibachiClient(
            api_key=Config.HIBACHI_API_KEY,
            private_key=Config.HIBACHI_PRIVATE_KEY,
            public_key=Config.HIBACHI_PUBLIC_KEY,
            account_id=Config.HIBACHI_ACCOUNT_ID,
        )

        # 텔레그램
        self.telegram = TelegramUI(Config.TELEGRAM_BOT_TOKEN, Config.TELEGRAM_CHAT_ID)

        # WebSocket 가격 피드
        self.standx_price: float = 0.0
        self.hibachi_price: float = 0.0
        self.standx_ws = StandXWSClient(
            Config.STANDX_WS_URL, Config.PAIR_STANDX, self._on_standx_price
        )
        self.hibachi_ws = HibachiWSClient(
            Config.HIBACHI_WS_URL, Config.PAIR_HIBACHI, self._on_hibachi_price
        )

        # 상태
        self.state = self._load_state()
        self._positions: dict[str, Position] = {}
        self._current_cycle: Cycle | None = None
        self._cycle_entered_at: float | None = None
        self._cooldown_until: float | None = None
        self._cumulative_funding_cost: float = 0.0
        self._initial_standx_balance: float = 0.0
        self._initial_hibachi_balance: float = 0.0
        self._running = False

    def _load_state(self) -> BotState:
        path = os.path.join(Config.LOG_DIR, "bot_state.json")
        if os.path.exists(path):
            try:
                return BotState.load(path)
            except Exception as e:
                logger.warning("상태 파일 로드 실패, 초기화: %s", e)
        return BotState()

    def _save_state(self):
        self.state.save(os.path.join(Config.LOG_DIR, "bot_state.json"))

    def _on_standx_price(self, price: float):
        self.standx_price = price

    def _on_hibachi_price(self, price: float):
        self.hibachi_price = price

    def _parse_direction(self, direction: str) -> tuple[str, str]:
        standx_side = "BUY" if "standx_long" in direction else "SELL"
        hibachi_side = "SELL" if "hibachi_short" in direction else "BUY"
        return standx_side, hibachi_side

    async def _check_margin_safety(self) -> MarginLevel:
        """양쪽 마진율 중 낮은 쪽 기준으로 위험도 판단"""
        worst = MarginLevel.NORMAL
        for key, pos in self._positions.items():
            price = self.standx_price if pos.exchange == "standx" else self.hibachi_price
            if price <= 0:
                continue
            ratio = pos.calc_margin_ratio(price)
            level = check_margin_level(ratio, Config.MARGIN_WARNING_PCT, Config.MARGIN_EMERGENCY_PCT)
            if level == MarginLevel.EMERGENCY:
                return MarginLevel.EMERGENCY
            if level == MarginLevel.WARNING:
                worst = MarginLevel.WARNING
        return worst

    async def _execute_enter(self, direction: str, notional: float):
        """양쪽 동시 진입"""
        standx_side, hibachi_side = self._parse_direction(direction)
        price = self.standx_price or self.hibachi_price
        if price <= 0:
            logger.error("가격 정보 없음, 진입 불가")
            return False

        quantity = notional / price
        # Limit 주문 양쪽 제출
        try:
            standx_result = await self.standx.place_limit_order(
                Config.PAIR_STANDX, standx_side, price, round(quantity, 4)
            )
            hibachi_result = await self.hibachi.place_limit_order(
                Config.PAIR_HIBACHI, hibachi_side, price, round(quantity, 6)
            )
        except Exception as e:
            logger.error("주문 제출 실패: %s", e)
            await self.telegram.send_alert(f"🚨 주문 제출 실패: {e}")
            return False

        # 체결 확인 (30초 대기)
        await asyncio.sleep(30)

        # TODO: 체결 상태 확인 로직 (양쪽 포지션 조회)
        standx_positions = await self.standx.get_positions()
        hibachi_positions = await self.hibachi.get_positions()

        has_standx = any(p.get("symbol") == Config.PAIR_STANDX for p in standx_positions)
        has_hibachi = any(p.get("symbol") == Config.PAIR_HIBACHI for p in hibachi_positions)

        if has_standx and has_hibachi:
            # 양쪽 체결 성공
            margin = notional / Config.LEVERAGE
            self._positions["standx"] = Position(
                exchange="standx", symbol=Config.PAIR_STANDX,
                side="LONG" if standx_side == "BUY" else "SHORT",
                notional=notional, entry_price=price,
                leverage=Config.LEVERAGE, margin=margin,
            )
            self._positions["hibachi"] = Position(
                exchange="hibachi", symbol=Config.PAIR_HIBACHI,
                side="LONG" if hibachi_side == "BUY" else "SHORT",
                notional=notional, entry_price=price,
                leverage=Config.LEVERAGE, margin=margin,
            )
            return True

        # 편측 체결 — 체결된 쪽 청산
        if has_standx and not has_hibachi:
            logger.warning("편측 체결: StandX만 체결, 청산")
            await self.standx.close_position(Config.PAIR_STANDX, standx_side, round(quantity, 4))
            await self.telegram.send_alert("⚠️ 편측 체결: StandX만 체결됨, 청산 완료")
        elif has_hibachi and not has_standx:
            logger.warning("편측 체결: Hibachi만 체결, 청산")
            await self.hibachi.close_position(Config.PAIR_HIBACHI, hibachi_side, round(quantity, 6))
            await self.telegram.send_alert("⚠️ 편측 체결: Hibachi만 체결됨, 청산 완료")
        return False

    async def _execute_exit(self):
        """양쪽 동시 청산"""
        for key, pos in list(self._positions.items()):
            try:
                quantity = pos.notional / pos.entry_price
                if pos.exchange == "standx":
                    side = "BUY" if pos.side == "LONG" else "SELL"
                    await self.standx.close_position(Config.PAIR_STANDX, side, round(quantity, 4))
                else:
                    side = "BUY" if pos.side == "LONG" else "SELL"
                    await self.hibachi.close_position(Config.PAIR_HIBACHI, side, round(quantity, 6))
            except Exception as e:
                logger.error("%s 청산 실패: %s", key, e)
                await self.telegram.send_alert(f"🚨 {key} 청산 실패: {e}")
        self._positions.clear()

    def _log_cycle(self, cycle: Cycle):
        path = os.path.join(Config.LOG_DIR, "cycles.jsonl")
        with open(path, "a") as f:
            f.write(cycle.to_jsonl() + "\n")

    async def _run_state_machine(self):
        """상태머신 1회 평가"""
        state = self.state.cycle_state

        if state == CycleState.IDLE:
            if self._cooldown_until and time.time() < self._cooldown_until:
                return
            self.state.cycle_state = CycleState.ANALYZE
            self._save_state()

        elif state == CycleState.ANALYZE:
            # 펀딩레이트 조회 및 방향 결정
            try:
                sx_data = await self.standx.get_funding_rate(Config.PAIR_STANDX)
                sx_rate = float(sx_data.get("funding_rate", 0))
                hb_rate = await self.hibachi.get_funding_rate(Config.PAIR_HIBACHI)

                sx_8h = normalize_funding_to_8h(sx_rate, period_hours=1)
                hb_8h = normalize_funding_to_8h(hb_rate, period_hours=8)

                direction = decide_direction(sx_8h, hb_8h)

                # 잔액 조회 → 노셔널 계산
                sx_bal = await self.standx.get_balance()
                hb_bal = await self.hibachi.get_balance()
                sx_available = float(sx_bal.get("available_balance", 0))
                hb_available = float(hb_bal.get("available", 0))

                self.state.standx_balance = sx_available
                self.state.hibachi_balance = hb_available
                self._initial_standx_balance = sx_available
                self._initial_hibachi_balance = hb_available

                notional = calc_notional(sx_available, hb_available, Config.LEVERAGE)

                logger.info("분석 완료: 방향=%s, 노셔널=$%.2f", direction, notional)
                self.state.cycle_state = CycleState.ENTER
                self._save_state()

                # ENTER 즉시 실행
                self.state.current_cycle_id += 1
                success = await self._execute_enter(direction, notional)
                if success:
                    self._cycle_entered_at = time.time()
                    self._cumulative_funding_cost = 0.0
                    self._current_cycle = Cycle(
                        cycle_id=self.state.current_cycle_id,
                        direction=direction,
                        notional=notional,
                    )
                    self.state.cycle_state = CycleState.HOLD
                    # 거래량 추적
                    self.state.weekly_hibachi_volume += notional
                    fee = notional * 0.0001  # StandX maker 0.01%
                    self.state.cumulative_fees += fee
                    await self.telegram.send_alert(
                        f"✅ 사이클 #{self.state.current_cycle_id} 진입\n"
                        f"방향: {direction}\n"
                        f"노셔널: ${notional:,.0f}\n"
                        f"StandX: ${sx_available:,.2f} / Hibachi: ${hb_available:,.2f}"
                    )
                else:
                    self.state.cycle_state = CycleState.IDLE
                self._save_state()

            except Exception as e:
                logger.error("분석/진입 실패: %s", e)
                await self.telegram.send_alert(f"🚨 분석/진입 실패: {e}")
                self.state.cycle_state = CycleState.IDLE
                self._save_state()

        elif state == CycleState.HOLD:
            if not self._cycle_entered_at:
                return
            hold_hours = (time.time() - self._cycle_entered_at) / 3600

            # 마진 안전 체크
            margin_level = await self._check_margin_safety()
            if margin_level == MarginLevel.EMERGENCY:
                logger.warning("🚨 긴급 청산!")
                await self.telegram.send_alert("🚨 마진율 위험! 양쪽 긴급 청산 실행!")
                await self._execute_exit()
                self.state.cycle_state = CycleState.COOLDOWN
                self._cooldown_until = time.time() + Config.COOLDOWN_HOURS * 3600
                self._save_state()
                return
            elif margin_level == MarginLevel.WARNING:
                worst_ratio = min(
                    (p.calc_margin_ratio(self.standx_price if p.exchange == "standx" else self.hibachi_price)
                     for p in self._positions.values() if (self.standx_price if p.exchange == "standx" else self.hibachi_price) > 0),
                    default=100.0,
                )
                await self.telegram.send_alert(f"⚠️ 마진율 경고: {worst_ratio:.1f}%")

            # 전환 판단
            worst_margin = min(
                (p.calc_margin_ratio(self.standx_price if p.exchange == "standx" else self.hibachi_price)
                 for p in self._positions.values() if (self.standx_price if p.exchange == "standx" else self.hibachi_price) > 0),
                default=100.0,
            )
            if should_exit_cycle(
                hold_hours=hold_hours,
                min_hold_hours=Config.MIN_HOLD_HOURS,
                cumulative_funding_cost=self._cumulative_funding_cost,
                funding_threshold=Config.FUNDING_COST_THRESHOLD,
                margin_ratio_min=worst_margin,
                margin_emergency_pct=Config.MARGIN_EMERGENCY_PCT,
                max_hold_days=Config.MAX_HOLD_DAYS,
            ):
                self.state.cycle_state = CycleState.EXIT
                self._save_state()

        elif state == CycleState.EXIT:
            await self._execute_exit()

            # 사이클 기록
            if self._current_cycle:
                self._current_cycle.exited_at = time.time()
                sx_bal = await self.standx.get_balance()
                hb_bal = await self.hibachi.get_balance()
                self._current_cycle.standx_balance_after = float(sx_bal.get("available_balance", 0))
                self._current_cycle.hibachi_balance_after = float(hb_bal.get("available", 0))
                self.state.standx_balance = self._current_cycle.standx_balance_after
                self.state.hibachi_balance = self._current_cycle.hibachi_balance_after
                # 거래량 추적 (청산)
                self.state.weekly_hibachi_volume += self._current_cycle.notional
                fee = self._current_cycle.notional * 0.0001
                self.state.cumulative_fees += fee

                self._log_cycle(self._current_cycle)

                hold_h = (self._current_cycle.exited_at - self._current_cycle.entered_at) / 3600
                await self.telegram.send_alert(
                    f"🔄 사이클 #{self._current_cycle.cycle_id} 청산\n"
                    f"보유: {hold_h:.1f}시간\n"
                    f"StandX: ${self._current_cycle.standx_balance_after:,.2f}\n"
                    f"Hibachi: ${self._current_cycle.hibachi_balance_after:,.2f}"
                )
                self._current_cycle = None

            self.state.cycle_state = CycleState.COOLDOWN
            self._cooldown_until = time.time() + Config.COOLDOWN_HOURS * 3600
            self._save_state()

        elif state == CycleState.COOLDOWN:
            if self._cooldown_until and time.time() >= self._cooldown_until:
                self._cooldown_until = None
                self.state.cycle_state = CycleState.IDLE
                self._save_state()
                logger.info("쿨다운 종료, IDLE 복귀")

    async def _recovery_check(self):
        """봇 재시작 시 기존 포지션 복구"""
        sx_positions = await self.standx.get_positions()
        hb_positions = await self.hibachi.get_positions()

        has_sx = any(p.get("symbol") == Config.PAIR_STANDX for p in sx_positions)
        has_hb = any(p.get("symbol") == Config.PAIR_HIBACHI for p in hb_positions)

        if has_sx and has_hb:
            logger.info("양쪽 포지션 감지 → HOLD 복구")
            self.state.cycle_state = CycleState.HOLD
            if not self._cycle_entered_at:
                self._cycle_entered_at = time.time()
            await self.telegram.send_alert("🔁 봇 재시작: 기존 포지션 감지, HOLD 상태 복구")
        elif has_sx or has_hb:
            side = "StandX" if has_sx else "Hibachi"
            logger.warning("편측 포지션 감지: %s", side)
            await self.telegram.send_alert(f"⚠️ 봇 재시작: {side}에만 포지션 존재! 수동 확인 필요")
        else:
            if self.state.cycle_state not in (CycleState.IDLE, CycleState.COOLDOWN):
                self.state.cycle_state = CycleState.IDLE
        self._save_state()

    def _register_telegram_callbacks(self):
        async def on_status(cb):
            sx_p = self.standx_price
            hb_p = self.hibachi_price
            text = (
                f"📊 <b>Status</b>\n"
                f"상태: {self.state.cycle_state}\n"
                f"StandX: ${self.state.standx_balance:,.2f} (ETH ${sx_p:,.2f})\n"
                f"Hibachi: ${self.state.hibachi_balance:,.2f} (ETH ${hb_p:,.2f})\n"
                f"주간 거래량: ${self.state.weekly_hibachi_volume:,.0f} / $100,000"
            )
            await self.telegram.send_main_menu(text)

        async def on_history(cb):
            path = os.path.join(Config.LOG_DIR, "cycles.jsonl")
            if not os.path.exists(path):
                await self.telegram.send_alert("📋 아직 완료된 사이클 없음")
                return
            lines = open(path).readlines()[-5:]
            text = "📋 <b>최근 사이클</b>\n\n"
            for line in lines:
                c = json.loads(line)
                hold = ((c.get("exited_at", 0) or 0) - c.get("entered_at", 0)) / 3600
                text += (
                    f"#{c['cycle_id']} {c['direction'][:20]}\n"
                    f"  보유: {hold:.1f}h / 노셔널: ${c['notional']:,.0f}\n\n"
                )
            await self.telegram.send_alert(text)

        async def on_funding(cb):
            try:
                sx_data = await self.standx.get_funding_rate(Config.PAIR_STANDX)
                sx_rate = float(sx_data.get("funding_rate", 0))
                hb_rate = await self.hibachi.get_funding_rate(Config.PAIR_HIBACHI)
                text = (
                    f"💰 <b>Funding Rates</b>\n"
                    f"StandX (1H): {sx_rate:.6f}\n"
                    f"Hibachi (8H): {hb_rate:.6f}\n"
                    f"누적 비용: {self._cumulative_funding_cost:.6f}"
                )
            except Exception as e:
                text = f"💰 펀딩레이트 조회 실패: {e}"
            await self.telegram.send_alert(text)

        async def on_rebalance(cb):
            if self.state.cycle_state == CycleState.HOLD:
                self.state.cycle_state = CycleState.EXIT
                self._save_state()
                await self.telegram.send_alert("🔄 수동 리밸런싱 트리거됨")
            else:
                await self.telegram.send_alert(f"현재 상태({self.state.cycle_state})에서는 리밸런싱 불가")

        async def on_stop(cb):
            await self.telegram.send_alert("⏹ 봇 종료 중... 포지션 청산")
            if self._positions:
                await self._execute_exit()
            self._running = False

        self.telegram.register_callback("status", on_status)
        self.telegram.register_callback("history", on_history)
        self.telegram.register_callback("funding", on_funding)
        self.telegram.register_callback("rebalance", on_rebalance)
        self.telegram.register_callback("stop", on_stop)

    async def run(self):
        """메인 루프"""
        self._running = True
        self._register_telegram_callbacks()

        # 시작 알림
        await self.telegram.send_main_menu(
            f"🚀 Delta Neutral Bot 시작\n상태: {self.state.cycle_state}"
        )

        # 포지션 복구
        await self._recovery_check()

        # WebSocket 시작 (백그라운드)
        ws_tasks = [
            asyncio.create_task(self.standx_ws.connect()),
            asyncio.create_task(self.hibachi_ws.connect()),
        ]

        # REST fallback 가격 업데이트
        last_balance_check = 0
        last_funding_check = 0

        try:
            while self._running:
                now = time.time()

                # 텔레그램 폴링
                await self.telegram.poll_updates()

                # REST fallback: 가격 (WS가 안 되면)
                if self.standx_price == 0 or self.hibachi_price == 0:
                    try:
                        sx_market = await self.standx.get_market_price(Config.PAIR_STANDX)
                        self.standx_price = float(sx_market.get("mark_price", 0))
                        self.hibachi_price = await self.hibachi.get_mark_price(Config.PAIR_HIBACHI)
                    except Exception:
                        pass

                # 상태머신 평가
                await self._run_state_machine()

                # 마진 안전 체크 (HOLD 상태에서 WS 가격 기반)
                if self.state.cycle_state == CycleState.HOLD and self._positions:
                    level = await self._check_margin_safety()
                    if level == MarginLevel.EMERGENCY:
                        logger.warning("🚨 WS 가격 기반 긴급 청산!")
                        await self.telegram.send_alert("🚨 실시간 가격 기반 긴급 청산!")
                        await self._execute_exit()
                        self.state.cycle_state = CycleState.COOLDOWN
                        self._cooldown_until = now + Config.COOLDOWN_HOURS * 3600
                        self._save_state()

                # 잔액 체크 (5분)
                if now - last_balance_check > Config.POLL_BALANCE_SECONDS:
                    last_balance_check = now
                    try:
                        sx_bal = await self.standx.get_balance()
                        hb_bal = await self.hibachi.get_balance()
                        self.state.standx_balance = float(sx_bal.get("available_balance", 0))
                        self.state.hibachi_balance = float(hb_bal.get("available", 0))
                        self._save_state()
                    except Exception as e:
                        logger.error("잔액 조회 실패: %s", e)

                # 펀딩레이트 체크 (1시간)
                if now - last_funding_check > Config.POLL_FUNDING_SECONDS:
                    last_funding_check = now
                    try:
                        sx_data = await self.standx.get_funding_rate(Config.PAIR_STANDX)
                        sx_rate = float(sx_data.get("funding_rate", 0))
                        hb_rate = await self.hibachi.get_funding_rate(Config.PAIR_HIBACHI)
                        snap = FundingSnapshot(sx_rate, hb_rate, now)
                        # 로그
                        path = os.path.join(Config.LOG_DIR, "funding_history.jsonl")
                        with open(path, "a") as f:
                            f.write(json.dumps({"sx": sx_rate, "hb": hb_rate, "ts": now}) + "\n")
                    except Exception as e:
                        logger.error("펀딩레이트 조회 실패: %s", e)

                await asyncio.sleep(5)  # 5초 간격 루프

        finally:
            # 정리
            await self.standx_ws.disconnect()
            await self.hibachi_ws.disconnect()
            await self.standx.close()
            await self.hibachi.close()
            await self.telegram.close()
            for t in ws_tasks:
                t.cancel()
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `python -m pytest tests/test_bot_core.py -v`
Expected: 모든 테스트 PASS

- [ ] **Step 5: 커밋**

```bash
git add bot_core.py tests/test_bot_core.py
git commit -m "feat: 봇 코어 — 상태머신, 진입/청산, 마진 감시, 텔레그램 콜백"
```

---

## Task 10: Watchdog 엔트리포인트 (run_bot.py)

**Files:**
- Create: `run_bot.py`

- [ ] **Step 1: run_bot.py 구현**

```python
# run_bot.py
"""Watchdog 엔트리포인트 — 크래시 시 자동 재시작"""
import sys
import subprocess
import time
import os
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("watchdog")

BOT_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_core.py")
STOP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".stop_bot")
MAX_CRASHES = 10
CRASH_WINDOW = 300  # 5분 내 10회 크래시 시 중단


def run_bot_main():
    """bot_core.py의 메인 함수를 asyncio로 실행하는 래퍼 스크립트"""
    import asyncio
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from bot_core import DeltaNeutralBot

    async def main():
        bot = DeltaNeutralBot()
        await bot.run()

    asyncio.run(main())


def main():
    crash_times = []
    restart_count = 0

    # 정지 파일 제거
    if os.path.exists(STOP_FILE):
        os.remove(STOP_FILE)

    logger.info("🚀 Delta Neutral Bot Watchdog 시작")

    while True:
        if os.path.exists(STOP_FILE):
            logger.info("⏹ 정지 파일 감지, 종료")
            break

        restart_count += 1
        logger.info("봇 시작 (시도 #%d)", restart_count)

        try:
            proc = subprocess.Popen(
                [sys.executable, "-c",
                 "import sys; sys.path.insert(0, r'" + os.path.dirname(os.path.abspath(__file__)) + "');"
                 "import asyncio; from bot_core import DeltaNeutralBot; asyncio.run(DeltaNeutralBot().run())"],
                stdout=sys.stdout,
                stderr=sys.stderr,
            )
            exit_code = proc.wait()
        except KeyboardInterrupt:
            logger.info("Ctrl+C 감지, 종료")
            if proc:
                proc.terminate()
            break

        if exit_code == 0:
            logger.info("봇 정상 종료 (exit 0)")
            break

        # 크래시 추적
        now = time.time()
        crash_times.append(now)
        crash_times = [t for t in crash_times if now - t < CRASH_WINDOW]

        if len(crash_times) >= MAX_CRASHES:
            logger.error("💀 %d분 내 %d회 크래시, 영구 종료", CRASH_WINDOW // 60, MAX_CRASHES)
            break

        logger.warning("봇 크래시 (exit %d), 5초 후 재시작...", exit_code)
        time.sleep(5)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 커밋**

```bash
git add run_bot.py
git commit -m "feat: Watchdog 엔트리포인트 — 크래시 자동 재시작, 정지 파일 감지"
```

---

## Task 11: 통합 테스트 및 마무리

**Files:**
- Verify: 전체 프로젝트 구조
- Run: 전체 테스트 스위트

- [ ] **Step 1: 전체 테스트 실행**

Run: `python -m pytest tests/ -v`
Expected: 모든 테스트 PASS

- [ ] **Step 2: 임포트 체인 검증**

Run: `python -c "from config import Config; print('config OK')"`
Run: `python -c "from models import CycleState, BotState; print('models OK')"`
Run: `python -c "from strategy import decide_direction; print('strategy OK')"`
Run: `python -c "from monitor import check_margin_level; print('monitor OK')"`
Expected: 모든 임포트 성공

- [ ] **Step 3: logs 디렉토리 확인**

Run: `python -c "from config import Config; Config.ensure_dirs(); import os; print(os.path.exists(Config.LOG_DIR))"`
Expected: `True`

- [ ] **Step 4: 최종 커밋**

```bash
git add -A
git commit -m "chore: 통합 테스트 통과, 프로젝트 구조 최종 확인"
```

---

## 실행 전 체크리스트

배포 전 David 대감이 직접 확인해야 할 사항:

1. **API 키 설정**: `.env` 파일에 StandX JWT, Hibachi API Key 등 입력
2. **텔레그램 봇 토큰**: BotFather에서 새 봇 생성 후 토큰 입력
3. **StandX 레버리지 설정**: 웹에서 ETH-USD 레버리지 3x로 미리 설정
4. **Hibachi 입금**: 최소 $10,000 USDT 입금 (최소 입금 10 USDT)
5. **StandX 입금**: DUSD $10,000 민팅 후 입금
6. **소액 테스트**: 처음에는 $100 정도로 1 사이클 테스트 실행
