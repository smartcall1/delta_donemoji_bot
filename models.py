"""데이터 모델 — Position, Cycle, FundingSnapshot, BotState"""
from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass, field, fields, asdict
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
    exchange: str
    symbol: str
    side: str
    notional: float
    entry_price: float
    leverage: int
    margin: float
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
    direction: str
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
    entry_sx_price: float = 0.0
    entry_hb_price: float = 0.0
    exit_sx_price: float = 0.0
    exit_hb_price: float = 0.0
    entry_spread: float = 0.0
    exit_spread: float = 0.0
    spread_cost: float = 0.0
    exit_reason: str = ""
    # 4지점 잔고 스냅샷 — 실제 수수료/PnL 측정용
    # T0=진입 직전 / T1=진입 직후 / T2=청산 직전 / T3=청산 직후
    balance_t0_total: float = 0.0  # 진입 전 잔고 합 (StandX + Hibachi)
    balance_t1_total: float = 0.0  # 진입 청크 완료 직후
    balance_t2_total: float = 0.0  # EXIT 시작 직전
    balance_t3_total: float = 0.0  # 청산 완료 직후 (= standx_balance_after + hibachi_balance_after)
    actual_entry_cost: float = 0.0  # T0 - T1 (실제 진입 수수료 + 슬리피지)
    actual_hold_change: float = 0.0  # T2 - T1 (HOLD 기간 실제 변동 = 펀딩 + 미실현)
    actual_exit_cost: float = 0.0  # T2 - T3 (실제 청산 수수료 + 슬리피지)
    actual_total_pnl: float = 0.0  # T3 - T0 (실제 cycle 총 손익)

    def to_jsonl(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


@dataclass
class FundingSnapshot:
    standx_rate: float
    hibachi_rate: float
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
    weekly_volume_reset_at: float = 0.0
    # C6+I6+I7: 크래시 복구용 — 방향, 진입시각, 쿨다운 저장
    current_direction: str = ""
    cycle_entered_at: float = 0.0
    cooldown_until: float = 0.0
    initial_standx_balance: float = 0.0
    initial_hibachi_balance: float = 0.0
    cumulative_funding_cost: float = 0.0  # RM-6: 펀딩비용 누적 (크래시 복구용)
    # 4지점 잔고 측정용 (진짜 수수료 추적)
    balance_after_entry_total: float = 0.0  # T1: 진입 청크 완료 직후 잔고 합
    balance_before_exit_total: float = 0.0  # T2: 청산 시작 직전 잔고 합

    def to_dict(self) -> dict:
        d = asdict(self)
        d["cycle_state"] = self.cycle_state.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> BotState:
        d = d.copy()
        d["cycle_state"] = CycleState(d["cycle_state"])
        # M2: 미래 버전 필드 무시 (forward compat)
        valid = {f.name for f in fields(cls)}
        d = {k: v for k, v in d.items() if k in valid}
        return cls(**d)

    def save(self, path: str = "logs/bot_state.json"):
        # C5: 원자적 쓰기 — 크래시 시 파일 손상 방지
        dir_ = os.path.dirname(os.path.abspath(path))
        os.makedirs(dir_, exist_ok=True)
        fd, tmp = tempfile.mkstemp(suffix=".tmp", dir=dir_)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self.to_dict(), f, indent=2)
            os.replace(tmp, path)
        except Exception:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

    @classmethod
    def load(cls, path: str = "logs/bot_state.json") -> BotState:
        with open(path) as f:
            return cls.from_dict(json.load(f))
