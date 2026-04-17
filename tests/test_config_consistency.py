"""설정 일관성 테스트 — .env.example의 값이 합리적 범위 안인지 검증

회귀 방지: .env.example에 잘못된 임계값이 있어도 단위 테스트는 통과한다.
이 테스트는 사용자가 .env.example을 그대로 복사했을 때 봇이 정상 동작하는지 검증.
"""
import os
import re


def _parse_env_example():
    """.env.example 파싱 → dict"""
    path = os.path.join(os.path.dirname(__file__), "..", ".env.example")
    values = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = re.match(r"^(\w+)=(.*)$", line)
            if m:
                values[m.group(1)] = m.group(2).strip()
    return values


def test_margin_thresholds_below_starting_ratio():
    """3x 레버리지 시작 마진율 33.3% — WARNING/EMERGENCY는 그 아래여야 함"""
    env = _parse_env_example()
    leverage = int(env.get("LEVERAGE", "3"))
    starting_margin_ratio = 100.0 / leverage  # 3x → 33.3%

    warning = float(env.get("MARGIN_WARNING_PCT", "15"))
    emergency = float(env.get("MARGIN_EMERGENCY_PCT", "8"))

    assert warning < starting_margin_ratio, (
        f"MARGIN_WARNING_PCT({warning}%) >= 시작 마진율({starting_margin_ratio:.1f}%) — "
        f"진입 즉시 경고 트리거됨!"
    )
    assert emergency < starting_margin_ratio - 5, (
        f"MARGIN_EMERGENCY_PCT({emergency}%) 시작 마진율에 너무 가까움 — "
        f"가격 작은 변동에도 긴급 청산 트리거됨"
    )
    assert emergency < warning, (
        f"EMERGENCY({emergency}%) >= WARNING({warning}%) — 임계값 순서 오류"
    )


def test_emergency_above_maintenance_margin():
    """EMERGENCY는 거래소 강제 청산(Hibachi 4.67%)보다 위여야 함 (선제 청산)"""
    env = _parse_env_example()
    emergency = float(env.get("MARGIN_EMERGENCY_PCT", "8"))
    HIBACHI_MAINTENANCE = 4.67  # %

    assert emergency > HIBACHI_MAINTENANCE, (
        f"EMERGENCY({emergency}%) <= Hibachi 유지마진({HIBACHI_MAINTENANCE}%) — "
        f"거래소가 먼저 강제 청산함"
    )


def test_min_hold_hours_reasonable():
    """SIP-2 anti-cycling 회피 위해 최소 24시간 권장"""
    env = _parse_env_example()
    min_hold = int(env.get("MIN_HOLD_HOURS", "24"))
    assert min_hold >= 12, f"MIN_HOLD_HOURS({min_hold}) 너무 짧음"


def test_cooldown_hours_reasonable():
    """쿨다운 너무 짧으면 SIP-2 cycling 감지"""
    env = _parse_env_example()
    cooldown = int(env.get("COOLDOWN_HOURS", "3"))
    assert cooldown >= 1, f"COOLDOWN_HOURS({cooldown}) 너무 짧음"


def test_funding_threshold_reasonable():
    """펀딩 비용 threshold 너무 작으면 매번 트리거"""
    env = _parse_env_example()
    threshold = float(env.get("FUNDING_COST_THRESHOLD", "0.001"))
    assert 0.0001 <= threshold <= 0.01, (
        f"FUNDING_COST_THRESHOLD({threshold}) 비합리적 범위"
    )
