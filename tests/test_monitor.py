import pytest
from monitor import MarginLevel, check_margin_level, estimate_sip2_yield


class TestCheckMarginLevel:
    def test_normal(self):
        assert check_margin_level(60.0, 50.0, 33.0) == MarginLevel.NORMAL

    def test_warning(self):
        assert check_margin_level(45.0, 50.0, 33.0) == MarginLevel.WARNING

    def test_emergency(self):
        assert check_margin_level(30.0, 50.0, 33.0) == MarginLevel.EMERGENCY

    def test_boundary_warning(self):
        assert check_margin_level(50.0, 50.0, 33.0) == MarginLevel.WARNING

    def test_boundary_emergency(self):
        assert check_margin_level(33.0, 50.0, 33.0) == MarginLevel.EMERGENCY


class TestEstimateSip2Yield:
    def test_basic_estimation(self):
        result = estimate_sip2_yield(10200.0, 10000.0, 150.0, -30.0, 0.0)
        assert abs(result - 80.0) < 0.01

    def test_negative_yield_clamped(self):
        result = estimate_sip2_yield(10000.0, 10100.0, 0.0, -10.0, 0.0)
        assert result == 0.0
