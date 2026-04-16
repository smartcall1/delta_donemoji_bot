"""마진율 감시, SIP-2 수익 추정"""
from __future__ import annotations
import logging
from enum import StrEnum

logger = logging.getLogger(__name__)


class MarginLevel(StrEnum):
    NORMAL = "NORMAL"
    WARNING = "WARNING"
    EMERGENCY = "EMERGENCY"


def check_margin_level(margin_ratio: float, warning_pct: float, emergency_pct: float) -> MarginLevel:
    if margin_ratio <= emergency_pct:
        return MarginLevel.EMERGENCY
    elif margin_ratio <= warning_pct:
        return MarginLevel.WARNING
    return MarginLevel.NORMAL


def estimate_sip2_yield(
    current_balance: float, initial_balance: float,
    cumulative_funding: float, cumulative_fees: float, realized_pnl: float,
) -> float:
    balance_change = current_balance - initial_balance
    explained = cumulative_funding + cumulative_fees + realized_pnl
    return max(balance_change - explained, 0.0)
