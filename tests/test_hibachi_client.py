import pytest
from unittest.mock import AsyncMock, patch
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
        # RC-2: post_only 기본값 False (체결 우선)
        client._place_order_raw.assert_called_once_with(
            symbol="ETH/USDT-P", side="SELL", price=1850.0, size=10.0, post_only=False,
        )
