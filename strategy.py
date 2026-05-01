"""펀딩레이트 비교, 방향 결정, 전환 판단"""
from __future__ import annotations
import logging

logger = logging.getLogger(__name__)


def normalize_funding_to_8h(rate: float, period_hours: int) -> float:
    return rate * (8 / period_hours)


def decide_direction(standx_rate_8h: float, hibachi_rate_8h: float) -> str | None:
    """양수 펀딩 = 롱이 숏에게 지불.
    옵션A: StandX Long + Hibachi Short → net = hibachi - standx
    옵션B: StandX Short + Hibachi Long → net = standx - hibachi
    양쪽 모두 음수면 진입 보류 (None 반환).
    """
    option_a = hibachi_rate_8h - standx_rate_8h
    option_b = standx_rate_8h - hibachi_rate_8h
    best = max(option_a, option_b)
    if best <= 0:
        logger.info("양쪽 순 펀딩 모두 음수 (A=%.6f, B=%.6f) → 진입 보류", option_a, option_b)
        return None
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
) -> str | None:
    """EXIT 사유를 문자열로 반환. None이면 EXIT 안 함.
    마진 긴급은 MIN_HOLD 이전에도 발동."""
    if margin_ratio_min <= margin_emergency_pct:
        return "margin_emergency"
    if hold_hours < min_hold_hours:
        return None
    if cumulative_funding_cost > funding_threshold:
        return "funding_cost"
    if hold_hours > max_hold_days * 24:
        return "max_hold"
    return None


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


def should_exit_principal_recovered(
    current_total: float, init_total: float, notional: float, fee_per_fill: float,
    safety_margin_usd: float = 0.0,
) -> bool:
    """원금 회수 청산 트리거.
    현재 잔액(MTM 포함) ≥ 진입 잔액 + 청산 수수료 + safety_margin_usd.

    buffer 분해:
      - 1 × fee_per_fill × notional: 청산은 HB maker(0%) + SX taker(0.04%) = 0.04%
      - safety_margin_usd: spread MTM이 청산 진행 중 회귀할 수 있는 변동성 마진.
    """
    threshold = init_total + notional * fee_per_fill + safety_margin_usd
    if current_total >= threshold:
        logger.info(
            "원금 회수 청산: total=$%.2f >= threshold=$%.2f "
            "(init=$%.2f + fee=$%.2f + safety=$%.2f, notional=$%.0f)",
            current_total, threshold, init_total,
            notional * fee_per_fill, safety_margin_usd, notional,
        )
        return True
    return False


def should_exit_spread(delta_sum: float, threshold: float) -> bool:
    """스프레드 MTM이 threshold 이상이면 기회적 청산 트리거.
    MIN_HOLD 무관 — 수익 조건만 판단."""
    if delta_sum >= threshold:
        logger.info("기회적 청산 트리거: delta_sum=$%.2f >= threshold=$%.0f", delta_sum, threshold)
        return True
    return False


def calc_notional(standx_balance: float, hibachi_balance: float, leverage: int,
                   margin_buffer: float = 0.95) -> float:
    """작은 쪽 잔액의 95%로 노셔널 계산 (M1: 펀딩비 여유분 확보)"""
    return min(standx_balance, hibachi_balance) * leverage * margin_buffer
