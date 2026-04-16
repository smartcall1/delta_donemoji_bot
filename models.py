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
