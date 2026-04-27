"""환경변수 기반 설정 로딩"""
import os
from dotenv import load_dotenv

_ROOT = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_ROOT, ".env"))


class Config:
    # StandX
    STANDX_JWT_TOKEN: str = os.getenv("STANDX_JWT_TOKEN", "")
    STANDX_PRIVATE_KEY: str = os.getenv("STANDX_PRIVATE_KEY", "")
    STANDX_BASE_URL: str = "https://perps.standx.com"
    STANDX_WS_URL: str = "wss://perps.standx.com/ws-stream/v1"

    # Hibachi
    HIBACHI_API_KEY: str = os.getenv("HIBACHI_API_KEY", "")
    HIBACHI_PRIVATE_KEY: str = os.getenv("HIBACHI_PRIVATE_KEY", "")
    HIBACHI_PUBLIC_KEY: str = os.getenv("HIBACHI_PUBLIC_KEY", "")
    HIBACHI_ACCOUNT_ID: str = os.getenv("HIBACHI_ACCOUNT_ID", "")
    HIBACHI_API_URL: str = "https://api.hibachi.xyz"
    HIBACHI_DATA_URL: str = "https://data-api.hibachi.xyz"
    HIBACHI_WS_URL: str = "wss://data-api.hibachi.xyz/ws/market"

    # 전략
    LEVERAGE: int = int(os.getenv("LEVERAGE", "3"))
    PAIR_STANDX: str = os.getenv("PAIR_STANDX", "ETH-USD")
    PAIR_HIBACHI: str = os.getenv("PAIR_HIBACHI", "ETH/USDT-P")
    MIN_HOLD_HOURS: int = int(os.getenv("MIN_HOLD_HOURS", "24"))
    MAX_HOLD_DAYS: int = int(os.getenv("MAX_HOLD_DAYS", "4"))
    COOLDOWN_HOURS: int = int(os.getenv("COOLDOWN_HOURS", "3"))
    FUNDING_COST_THRESHOLD: float = float(os.getenv("FUNDING_COST_THRESHOLD", "0.001"))
    SPREAD_EXIT_THRESHOLD: float = float(os.getenv("SPREAD_EXIT_THRESHOLD", "50"))
    # 3x 레버리지 기준: 시작 33%, 청산 ~5% (Hibachi 4.67%, StandX 1.25%)
    MARGIN_WARNING_PCT: float = float(os.getenv("MARGIN_WARNING_PCT", "15"))
    MARGIN_EMERGENCY_PCT: float = float(os.getenv("MARGIN_EMERGENCY_PCT", "10"))

    STANDX_MAINTENANCE_MARGIN: float = 0.0125
    HIBACHI_MAINTENANCE_MARGIN: float = 0.0467

    # 거래 수수료
    STANDX_TAKER_FEE: float = 0.0004    # 0.04%
    HIBACHI_TAKER_FEE: float = 0.00045  # 0.045% (Tier 1, 14일 누적 < $5M)
    HIBACHI_MAKER_FEE: float = 0.0      # Hibachi maker 무료
    # 구식: 양쪽 taker 합 (참고용)
    LEGACY_BOTH_TAKER_FEE: float = STANDX_TAKER_FEE + HIBACHI_TAKER_FEE  # 0.085%
    # XEMM 실행 모드 — Hibachi maker(0%) + StandX taker(0.04%). entry/exit 1회 비용.
    FEE_PER_FILL: float = STANDX_TAKER_FEE + HIBACHI_MAKER_FEE  # 0.04%

    # XEMM 실행 파라미터
    MAKER_FILL_TIMEOUT_SECONDS: int = int(os.getenv("MAKER_FILL_TIMEOUT_SECONDS", "60"))
    MAKER_RETRY_LIMIT: int = int(os.getenv("MAKER_RETRY_LIMIT", "5"))

    # 텔레그램
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

    # 폴링
    POLL_BALANCE_SECONDS: int = int(os.getenv("POLL_BALANCE_SECONDS", "300"))
    POLL_FUNDING_SECONDS: int = int(os.getenv("POLL_FUNDING_SECONDS", "3600"))

    # 로그
    LOG_DIR: str = os.path.join(_ROOT, "logs")

    @classmethod
    def ensure_dirs(cls):
        os.makedirs(cls.LOG_DIR, exist_ok=True)

    @classmethod
    def validate(cls) -> list[str]:
        errors = []
        if not cls.STANDX_JWT_TOKEN:
            errors.append("STANDX_JWT_TOKEN 미설정")
        if not cls.STANDX_PRIVATE_KEY:
            errors.append("STANDX_PRIVATE_KEY 미설정")
        if not cls.HIBACHI_API_KEY:
            errors.append("HIBACHI_API_KEY 미설정")
        if not cls.HIBACHI_PRIVATE_KEY:
            errors.append("HIBACHI_PRIVATE_KEY 미설정")
        if not cls.TELEGRAM_BOT_TOKEN:
            errors.append("TELEGRAM_BOT_TOKEN 미설정")
        if not cls.TELEGRAM_CHAT_ID:
            errors.append("TELEGRAM_CHAT_ID 미설정")
        return errors
