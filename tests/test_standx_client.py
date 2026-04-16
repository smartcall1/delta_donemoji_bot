import pytest
from unittest.mock import AsyncMock, patch
from exchanges.standx_client import StandXClient


@pytest.fixture
def client():
    return StandXClient(
        base_url="https://perps.standx.com",
        jwt_token="test_jwt",
        private_key_hex="a" * 64,
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
        {"symbol": "ETH-USD", "side": "LONG", "qty": "10.0", "entry_price": "1800.0"}
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
        result = await client.place_limit_order(symbol="ETH-USD", side="BUY", price=1800.0, quantity=10.0)
        assert result["order_id"] == "abc123"


@pytest.mark.asyncio
async def test_cancel_order(client):
    mock_resp = {"code": 0, "data": {"order_id": "abc123", "status": "CANCELED"}}
    with patch.object(client, "_signed_request", new_callable=AsyncMock, return_value=mock_resp):
        result = await client.cancel_order("abc123")
        assert result["status"] == "CANCELED"


@pytest.mark.asyncio
async def test_close_position(client):
    mock_market = {"code": 0, "data": {"mark_price": "1850.00"}}
    mock_order = {"code": 0, "data": {"order_id": "close123", "status": "FILLED"}}
    with patch.object(client, "_request", new_callable=AsyncMock, return_value=mock_market):
        with patch.object(client, "_signed_request", new_callable=AsyncMock, return_value=mock_order):
            result = await client.close_position(symbol="ETH-USD", side="BUY", quantity=10.0)
            assert result["order_id"] == "close123"
