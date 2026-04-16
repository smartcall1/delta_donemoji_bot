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
        exchange="standx", symbol="ETH-USD", side="LONG",
        notional=30000.0, entry_price=1800.0, leverage=3, margin=10000.0,
    )
    assert pos.exchange == "standx"
    assert pos.side == "LONG"
    assert pos.notional == 30000.0


def test_position_unrealized_pnl():
    pos = Position(
        exchange="standx", symbol="ETH-USD", side="LONG",
        notional=30000.0, entry_price=1800.0, leverage=3, margin=10000.0,
    )
    pnl = pos.calc_unrealized_pnl(1900.0)
    assert abs(pnl - 1666.67) < 1.0
    pnl = pos.calc_unrealized_pnl(1700.0)
    assert abs(pnl - (-1666.67)) < 1.0


def test_position_unrealized_pnl_short():
    pos = Position(
        exchange="hibachi", symbol="ETH/USDT-P", side="SHORT",
        notional=30000.0, entry_price=1800.0, leverage=3, margin=10000.0,
    )
    pnl = pos.calc_unrealized_pnl(1700.0)
    assert abs(pnl - 1666.67) < 1.0


def test_position_margin_ratio():
    pos = Position(
        exchange="standx", symbol="ETH-USD", side="LONG",
        notional=30000.0, entry_price=1800.0, leverage=3, margin=10000.0,
    )
    ratio = pos.calc_margin_ratio(1800.0)
    assert abs(ratio - 33.33) < 0.1
    ratio = pos.calc_margin_ratio(1530.0)
    assert ratio > 20.0


def test_cycle_creation():
    cycle = Cycle(cycle_id=1, direction="standx_long_hibachi_short", notional=30000.0)
    assert cycle.cycle_id == 1
    assert cycle.net_funding == 0.0
    assert cycle.exited_at is None


def test_funding_snapshot():
    snap = FundingSnapshot(standx_rate=0.001, hibachi_rate=0.008, timestamp=1713300000.0)
    assert snap.standx_rate == 0.001
    assert snap.hibachi_rate == 0.008


def test_bot_state_serialization():
    state = BotState(
        cycle_state=CycleState.HOLD, current_cycle_id=3,
        standx_balance=10150.0, hibachi_balance=9820.0,
    )
    d = state.to_dict()
    assert d["cycle_state"] == "HOLD"
    assert d["current_cycle_id"] == 3
    restored = BotState.from_dict(d)
    assert restored.cycle_state == CycleState.HOLD
    assert restored.standx_balance == 10150.0


def test_bot_state_atomic_save(tmp_path):
    """C5: 원자적 저장 — 파일이 정상적으로 쓰이는지 검증"""
    path = str(tmp_path / "bot_state.json")
    state = BotState(cycle_state=CycleState.HOLD, current_cycle_id=5)
    state.save(path)
    loaded = BotState.load(path)
    assert loaded.cycle_state == CycleState.HOLD
    assert loaded.current_cycle_id == 5


def test_bot_state_from_dict_forward_compat():
    """M2: 미래 버전 필드가 있어도 크래시 안 함"""
    d = {
        "cycle_state": "IDLE",
        "current_cycle_id": 0,
        "standx_balance": 0.0,
        "hibachi_balance": 0.0,
        "cumulative_funding": 0.0,
        "cumulative_fees": 0.0,
        "total_sip2_estimate": 0.0,
        "weekly_hibachi_volume": 0.0,
        "weekly_volume_reset_at": 0.0,
        "current_direction": "",
        "cycle_entered_at": 0.0,
        "cooldown_until": 0.0,
        "initial_standx_balance": 0.0,
        "initial_hibachi_balance": 0.0,
        "unknown_future_field": 999,
    }
    state = BotState.from_dict(d)
    assert state.cycle_state == CycleState.IDLE


def test_bot_state_new_fields():
    """C6+I6+I7: 복구용 필드가 저장/로드되는지 확인"""
    state = BotState(
        cycle_state=CycleState.HOLD,
        current_direction="standx_long_hibachi_short",
        cycle_entered_at=1713300000.0,
        cooldown_until=0.0,
    )
    d = state.to_dict()
    restored = BotState.from_dict(d)
    assert restored.current_direction == "standx_long_hibachi_short"
    assert restored.cycle_entered_at == 1713300000.0
