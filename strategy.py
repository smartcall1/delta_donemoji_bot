"""펀딩레이트 비교, 방향 결정, 전환 판단"""
from __future__ import annotations
import logging

logger = logging.getLogger(__name__)


def normalize_funding_to_8h(rate: float, period_hours: int) -> float:
    return rate * (8 / period_hours)


def decide_direction(standx_rate_8h: float, hibachi_rate_8h: float) -> str:
    """양수 펀딩 = 롱이 숏에게 지불.
    옵션A: StandX Long + Hibachi Short → net = hibachi - standx
    옵션B: StandX Short + Hibachi Long → net = standx - hibachi
    """
    option_a = hibachi_rate_8h - standx_rate_8h
    option_b = standx_rate_8h - hibachi_rate_8h
    if option_a >= option_b:
        logger.info("방향: StandX LONG + Hibachi SHORT (net=%.6f)", option_a)
        return "standx_long_hibachi_short"
    else:
        logger.info("방향: StandX SHORT + Hibachi LONG (net=%.6f)", option_b)
        return "standx_short_hibachi_long"


def should_exit_cycle(
    hold_hours: float, min_hold_hours: int, cumulative_funding_cost: float,
    funding_threshold: float, margin_ratio_min: float, margin_emergency_pct: float,
    max_hold_days: int,
) -> bool:
    if hold_hours < min_hold_hours:
        return False
    if margin_ratio_min <= margin_emergency_pct:
        return True
    if cumulative_funding_cost > funding_threshold:
        return True
    if hold_hours > max_hold_days * 24:
        return True
    return False


def is_opposite_direction_better(
    current_direction: str,
    standx_rate_8h: float,
    hibachi_rate_8h: float,
    advantage_threshold: float = 0.0005,
) -> bool:
    """S5: 반대 방향이 현저히 유리한지 판단 (차이 > threshold)"""
    option_a = hibachi_rate_8h - standx_rate_8h  # standx_long_hibachi_short
    option_b = standx_rate_8h - hibachi_rate_8h  # standx_short_hibachi_long

    if "standx_long" in current_direction:
        current_net = option_a
        opposite_net = option_b
    else:
        current_net = option_b
        opposite_net = option_a

    advantage = opposite_net - current_net
    if advantage > advantage_threshold:
        logger.info("반대 방향 유리: 현재=%.6f, 반대=%.6f, 차이=%.6f", current_net, opposite_net, advantage)
        return True
    return False


def calc_notional(standx_balance: float, hibachi_balance: float, leverage: int,
                   margin_buffer: float = 0.95) -> float:
    """작은 쪽 잔액의 95%로 노셔널 계산 (M1: 펀딩비 여유분 확보)"""
    return min(standx_balance, hibachi_balance) * leverage * margin_buffer
