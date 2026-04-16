import pytest
from strategy import normalize_funding_to_8h, decide_direction, should_exit_cycle, calc_notional, is_opposite_direction_better


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

    def test_equal_rates_default(self):
        assert decide_direction(0.005, 0.005) == "standx_long_hibachi_short"


class TestShouldExitCycle:
    def test_not_enough_hold_time(self):
        assert should_exit_cycle(12, 24, 0.005, 0.001, 50.0, 33.0, 4) is False

    def test_exit_on_funding_cost(self):
        assert should_exit_cycle(30, 24, 0.002, 0.001, 50.0, 33.0, 4) is True

    def test_exit_on_margin_danger(self):
        assert should_exit_cycle(30, 24, 0.0, 0.001, 30.0, 33.0, 4) is True

    def test_exit_on_max_hold(self):
        assert should_exit_cycle(100, 24, 0.0, 0.001, 50.0, 33.0, 4) is True

    def test_no_exit_when_healthy(self):
        assert should_exit_cycle(30, 24, 0.0005, 0.001, 50.0, 33.0, 4) is False


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
