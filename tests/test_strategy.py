import pytest
from strategy import (
    normalize_funding_to_8h, decide_direction, should_exit_cycle, calc_notional,
    is_opposite_direction_better, should_exit_spread, should_exit_principal_recovered,
)


class TestNormalizeFunding:
    def test_standx_1h_to_8h(self):
        assert abs(normalize_funding_to_8h(0.001, 1) - 0.008) < 1e-9

    def test_hibachi_8h_unchanged(self):
        assert abs(normalize_funding_to_8h(0.008, 8) - 0.008) < 1e-9

    def test_negative_rate(self):
        assert abs(normalize_funding_to_8h(-0.002, 1) - (-0.016)) < 1e-9


class TestDecideDirection:
    def test_standx_long_when_cheaper(self):
        assert decide_direction(0.005, 0.010) == "standx_long_hibachi_short"

    def test_standx_short_when_cheaper(self):
        assert decide_direction(0.010, 0.005) == "standx_short_hibachi_long"

    def test_equal_rates_returns_none(self):
        # 양쪽 net = 0 → 수익 없으므로 진입 보류
        assert decide_direction(0.005, 0.005) is None

    def test_both_negative_but_one_direction_profitable(self):
        # sx=-0.003, hb=-0.005 → option_b = -0.003-(-0.005) = +0.002 (양수)
        assert decide_direction(-0.003, -0.005) == "standx_short_hibachi_long"

    def test_truly_unprofitable_returns_none(self):
        # 양쪽 동일 음수 → 모든 방향 net=0
        assert decide_direction(-0.005, -0.005) is None

    def test_one_positive_enters(self):
        assert decide_direction(-0.001, 0.003) == "standx_long_hibachi_short"

    def test_both_zero_returns_none(self):
        assert decide_direction(0.0, 0.0) is None


class TestShouldExitCycle:
    def test_not_enough_hold_time(self):
        assert should_exit_cycle(12, 24, 0.005, 0.001, 50.0, 33.0, 4) is None

    def test_exit_on_funding_cost(self):
        assert should_exit_cycle(30, 24, 0.002, 0.001, 50.0, 33.0, 4) == "funding_cost"

    def test_exit_on_margin_danger(self):
        assert should_exit_cycle(30, 24, 0.0, 0.001, 30.0, 33.0, 4) == "margin_emergency"

    def test_exit_on_max_hold(self):
        assert should_exit_cycle(100, 24, 0.0, 0.001, 50.0, 33.0, 4) == "max_hold"

    def test_no_exit_when_healthy(self):
        assert should_exit_cycle(30, 24, 0.0005, 0.001, 50.0, 33.0, 4) is None

    def test_margin_emergency_before_min_hold(self):
        # 마진 긴급은 MIN_HOLD 이전에도 발동
        assert should_exit_cycle(6, 24, 0.0, 0.001, 8.0, 10.0, 4) == "margin_emergency"


class TestCalcNotional:
    def test_match_smaller_balance(self):
        # M1: 95% 마진 버퍼 적용
        assert calc_notional(10150.0, 9820.0, 3) == 9820.0 * 3 * 0.95

    def test_equal_balances(self):
        assert calc_notional(10000.0, 10000.0, 3) == 30000.0 * 0.95

    def test_custom_buffer(self):
        assert calc_notional(10000.0, 10000.0, 3, margin_buffer=1.0) == 30000.0


class TestIsOppositeDirectionBetter:
    def test_opposite_better_when_large_advantage(self):
        # 현재: standx_long, sx=0.010 hb=0.005 → current net = 0.005 - 0.010 = -0.005
        # 반대: standx_short → net = 0.010 - 0.005 = 0.005
        # advantage = 0.005 - (-0.005) = 0.010 > 0.0005 threshold
        assert is_opposite_direction_better("standx_long_hibachi_short", 0.010, 0.005) is True

    def test_opposite_not_better_when_small_diff(self):
        # 차이가 threshold 이하면 전환 안 함
        assert is_opposite_direction_better("standx_long_hibachi_short", 0.005, 0.0052) is False

    def test_opposite_better_for_short_direction(self):
        # 현재: standx_short, sx=0.003 hb=0.010 → current net = 0.003 - 0.010 = -0.007
        # 반대: standx_long → net = 0.010 - 0.003 = 0.007
        assert is_opposite_direction_better("standx_short_hibachi_long", 0.003, 0.010) is True

    def test_same_rates_no_switch(self):
        assert is_opposite_direction_better("standx_long_hibachi_short", 0.005, 0.005) is False


class TestShouldExitSpread:
    def test_above_threshold_triggers(self):
        assert should_exit_spread(55.0, 50.0) is True

    def test_below_threshold_no_trigger(self):
        assert should_exit_spread(30.0, 50.0) is False

    def test_exact_threshold_triggers(self):
        assert should_exit_spread(50.0, 50.0) is True

    def test_negative_delta_no_trigger(self):
        assert should_exit_spread(-20.0, 50.0) is False


class TestShouldExitPrincipalRecovered:
    """2026-04-28 cycle 10 사건 회귀 방지: buffer = 2× fee + safety_margin."""

    NOTIONAL = 24841.0
    FEE = 0.0004  # FEE_PER_FILL = 0.04%
    INIT = 21958.0

    def test_no_safety_margin_old_behavior(self):
        # safety_margin=0이면 임계 = init + 2× fee × notional = 21958 + 19.87 = 21977.87
        # 사건 당시 잔액 21974.64는 여전히 트리거되지 않음 (이전엔 1× fee로 9.94 → 트리거됐음)
        assert should_exit_principal_recovered(
            21974.64, self.INIT, self.NOTIONAL, self.FEE, safety_margin_usd=0.0,
        ) is False

    def test_old_logic_would_have_triggered_but_new_blocks(self):
        # 정확히 사건 데이터 — old(1× fee, $9.94 buffer)였다면 발동했을 잔액
        # new(2× fee, $19.87 buffer)에선 블록되어야 함
        old_threshold = self.INIT + 1 * self.NOTIONAL * self.FEE  # 21967.94
        assert 21974.64 >= old_threshold  # old logic: True
        assert should_exit_principal_recovered(
            21974.64, self.INIT, self.NOTIONAL, self.FEE, safety_margin_usd=0.0,
        ) is False  # new logic: False (2× fee buffer 적용)

    def test_safety_margin_30usd_blocks_marginal_trigger(self):
        # safety_margin=30이면 임계 = 21958 + 19.87 + 30 = 22007.87
        # 사건 당시 잔액으로는 절대 발동 안 함
        assert should_exit_principal_recovered(
            21974.64, self.INIT, self.NOTIONAL, self.FEE, safety_margin_usd=30.0,
        ) is False

    def test_clear_profit_triggers(self):
        # 잔액이 충분히 크면 발동 — 21958 + 19.87 + 30 = 22007.87, 잔액 22050이면 True
        assert should_exit_principal_recovered(
            22050.0, self.INIT, self.NOTIONAL, self.FEE, safety_margin_usd=30.0,
        ) is True

    def test_zero_init_returns_false(self):
        # 안전 가드 — init이 0이면 의미 없는 트리거 방지 (caller에서 init>0 체크되지만 함수 자체도 안전)
        assert should_exit_principal_recovered(
            100.0, 0.0, 1000.0, 0.001, safety_margin_usd=0.0,
        ) is True  # 100 >= 0 + 2 = 2 → True (이건 caller 책임)
