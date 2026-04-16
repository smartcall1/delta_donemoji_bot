# Delta Neutral Bot

StandX × Hibachi 델타 뉴트럴 ETH 양빵봇.

## 수익원
- StandX SIP-2 Position Yield
- Hibachi 포인트 → Growi Alpha Vault (APR ~42%)
- 펀딩레이트 차익

## 실행
```bash
cp .env.example .env   # API 키 입력
pip install -r requirements.txt
python test_connection.py   # 연결 확인
python run_delta.py           # 봇 시작
```

## 텔레그램 버튼
📊 Status / 📋 History / 💰 Funding / 🔄 Rebalance / ⏹ Stop

## 설정 (.env)
StandX JWT + Ed25519 키, Hibachi API 키, 텔레그램 봇 토큰 필요.
상세 항목은 `.env.example` 참고.

## Termux
[TERMUX_SETUP.md](TERMUX_SETUP.md) 참고.
