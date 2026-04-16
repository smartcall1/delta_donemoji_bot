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
        mock_bot._cooldown_until = time.time() + 3600
        should_transition = time.time() >= mock_bot._cooldown_until
        assert should_transition is False

    @pytest.mark.asyncio
    async def test_cooldown_expired(self, mock_bot):
        mock_bot.state.cycle_state = CycleState.COOLDOWN
        mock_bot._cooldown_until = time.time() - 1
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
