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
    mock_balance = {"balance": "10000.00"}
    with patch.object(client, "_get_balance_raw", new_callable=AsyncMock, return_value=mock_balance):
        balance = await client.get_balance()
        assert balance["balance"] == "10000.00"


@pytest.mark.asyncio
async def test_get_positions(client):
    # SDK가 account_info 객체를 반환, positions 속성에 리스트
    mock_info = MagicMock()
    mock_pos = MagicMock()
    mock_pos.__dataclass_fields__ = {}
    mock_info.positions = [mock_pos]
    with patch.object(client, "_get_account_info_raw", new_callable=AsyncMock, return_value=mock_info):
        positions = await client.get_positions()
        assert len(positions) == 1


@pytest.mark.asyncio
async def test_get_mark_price(client):
    mock_prices = MagicMock()
    mock_prices.markPrice = "1850.50"
    with patch.object(client, "_get_prices_raw", new_callable=AsyncMock, return_value=mock_prices):
        price = await client.get_mark_price("ETH/USDT-P")
        assert price == 1850.50


@pytest.mark.asyncio
async def test_get_funding_rate(client):
    mock_prices = MagicMock()
    mock_est = MagicMock()
    mock_est.estimatedFundingRate = "0.000031"
    mock_prices.fundingRateEstimation = mock_est
    with patch.object(client, "_get_prices_raw", new_callable=AsyncMock, return_value=mock_prices):
        rate = await client.get_funding_rate("ETH/USDT-P")
        assert rate == 0.000031


@pytest.mark.asyncio
async def test_place_limit_order(client):
    mock_result = MagicMock()
    mock_result.__dataclass_fields__ = {}
    with patch.object(client, "_place_order_raw", new_callable=AsyncMock, return_value=mock_result):
        result = await client.place_limit_order(
            symbol="ETH/USDT-P", side="SELL", price=1850.0, size=10.0
        )
        client._place_order_raw.assert_called_once_with(
            symbol="ETH/USDT-P", side="SELL", price=1850.0, quantity=10.0, post_only=False,
        )
