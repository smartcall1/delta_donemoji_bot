# Delta Neutral Bot 설계 문서

**작성일**: 2026-04-16  
**플랫폼**: StandX × Hibachi  
**목적**: 델타 뉴트럴 포지션으로 SIP-2 yield + Hibachi 포인트/Vault 수익 파밍

---

## 1. 전략 개요

양쪽 거래소에 ETH 반대 포지션을 열어 시장 노출을 0으로 만들고, 각 플랫폼의 부가 수익만 취하는 구조.

### 수익원 3층

| 층 | 출처 | 메커니즘 |
|---|---|---|
| 1층 | StandX SIP-2 | 포지션 보유 시간 × 노셔널 비례, 프로토콜 수수료 분배 (일일 정산 추정) |
| 2층 | Hibachi 포인트 | 거래 활동 → 매주 월요일 포인트 지급 → Vault 접근 자격 |
| 3층 | Hibachi GAV Vault | 포인트 상위 100~200명 진입 → APR 42% 실수익 |

### 핵심 파라미터

| 항목 | 값 |
|---|---|
| 페어 | ETH (StandX: ETH-USD, Hibachi: ETH/USDT-P) |
| 자금 배치 | 양쪽 $10,000씩 (총 $20,000) |
| 레버리지 | 3x → 노셔널 $30,000 |
| 사이클 보유 기간 | 3~4일 (최소 24시간) |
| 주간 사이클 | ~2회 → 거래량 ~$120K/주 |
| 쿨다운 | 사이클 간 2~4시간 대기 |

---

## 2. 포지션 라이프사이클

### 상태머신

```
[IDLE] → [ANALYZE] → [ENTER] → [HOLD] → [EXIT] → [COOLDOWN] → [IDLE]
```

| 상태 | 동작 | 지속 시간 |
|---|---|---|
| IDLE | 포지션 없음, 진입 대기 | 봇 시작 or COOLDOWN 종료 |
| ANALYZE | 양쪽 펀딩레이트 비교 → 방향 결정 | 수초 |
| ENTER | StandX + Hibachi 동시 Limit 주문 | 체결 대기 최대 5분 |
| HOLD | 포지션 유지, 모니터링 | 3~4일 (최소 24시간) |
| EXIT | 양쪽 동시 청산 | 체결 대기 최대 5분 |
| COOLDOWN | 딜레이 대기 (SIP-2 anti-cycling 회피) | 2~4시간 |

### 방향 결정 로직 (ANALYZE)

```python
# 양쪽 펀딩레이트를 8시간 기준으로 정규화
standx_8h = standx_1h_rate * 8
hibachi_8h = hibachi_8h_rate

# 각 조합의 순펀딩비용 계산 (양수 = 비용, 음수 = 수익)
# option_a: StandX Long + Hibachi Short
# option_b: StandX Short + Hibachi Long
# → 비용이 적거나 수익이 큰 방향 선택
```

StandX 펀딩레이트 정산: 매 1시간  
Hibachi 펀딩레이트 정산: 매 8시간 (00:00, 08:00, 16:00 UTC)

### 전환 조건 (HOLD → EXIT)

매시간 평가. 다음 조건 **모두 AND**:

1. 최소 보유 시간 24시간 경과
2. 다음 중 하나 충족:
   - 누적 순펀딩비용 > 노셔널의 0.1%
   - 반대 방향이 현저히 유리 (차이 > 0.05%/8h)
   - 한쪽 마진율 위험 수준 접근 (유지마진의 200% 이하)
   - 보유 기간 4일 초과 (주 2회 사이클 유지 목적)

### 잔액 불균형 처리

사이클마다 펀딩레이트 수취/지불로 양쪽 잔액이 벌어짐.

**전략: 작은 쪽에 맞추기**

```
다음 사이클 진입 시:
  notional = min(standx_balance, hibachi_balance) × 3
  여유분은 마진 버퍼로 활용
```

자금 이동(리밸런싱) 없음 — Hibachi 입출금 수수료가 높아서 (입금 0.67%, 출금 1.8%) 비효율적.

---

## 3. 편측 체결 방어

양쪽 동시 진입 시 한쪽만 체결되는 위험 대응:

```
1. 양쪽 Limit 주문 제출 (Hibachi Maker 0% 활용)
2. 30초 대기
3. 양쪽 모두 체결 → HOLD 진입
4. 한쪽만 체결 → 체결된 쪽 즉시 청산, 텔레그램 알림
5. 양쪽 모두 미체결 → 가격 재조정 후 재시도 (최대 3회)
6. 3회 실패 → IDLE로 복귀, 텔레그램 알림
```

---

## 4. 수수료 구조

### 거래 수수료

| | StandX | Hibachi (Tier 1) | 합산 |
|---|---|---|---|
| Maker (Limit) | 0.01% | **0%** | 0.01% |
| Taker (Market) | 0.04% | 0.045% | 0.085% |

**전략: 양쪽 모두 Limit 주문 사용 → 왕복 0.02%**

### 사이클당 비용 (노셔널 $30,000 기준)

```
진입 수수료: $30,000 × 0.01% = $3.00
청산 수수료: $30,000 × 0.01% = $3.00
사이클 왕복: $6.00
주간 (2 사이클): $12.00
월간 (8 사이클): $48.00
```

### Hibachi 입출금 수수료

| 유형 | 수수료 |
|---|---|
| 입금 | 0.67% |
| 출금 | 1.8% |

**자금 이동 최소화 필수** — 리밸런싱 1회 비용 = ~$250 ($10K 기준).

---

## 5. 펀딩레이트 상세

| | StandX | Hibachi |
|---|---|---|
| 정산 주기 | **1시간** | **8시간** |
| 기본 IR | 0.00125%/hour | 미공개 |
| 최대 Rate | 4%/hour | 미공개 |
| Premium 재계산 | 5초마다 | 매초 이동평균 |

**정규화**: 비교 시 8시간 기준으로 통일 (StandX 1H rate × 8).

---

## 6. 안전장치

### 마진 기반 선제 청산

| ETH 변동 | 마진율 | 봇 대응 |
|---|---|---|
| 5% | 78% | 정상 |
| 10% | 67% | 정상 |
| 15% | 50% | ⚠️ 텔레그램 경고 |
| 20% | 33% | 🚨 양쪽 자동 청산 |
| 29% | 4.67% | 💀 거래소 강제 청산 (봇이 먼저 닫음) |

### 3x 레버리지 청산 거리

- Hibachi (유지마진 4.67%): ETH ~28.7% 변동 시 청산
- StandX (유지마진 1.25%): ETH ~32.1% 변동 시 청산
- **봇 선제 청산 기준: 20% 변동 (거래소 청산까지 ~9%p 여유)**

### 기타 안전장치

| 상황 | 대응 |
|---|---|
| 한쪽 API 연결 끊김 | 재연결 3회 시도 → 실패 시 양쪽 청산 |
| 편측 체결 실패 | 체결된 쪽 즉시 청산 |
| DUSD 디페그 > 1% | 텔레그램 경고 (수동 판단) |
| 봇 크래시 | Watchdog 자동 재시작, 포지션 상태 복구 |

### 포지션 복구 (봇 재시작 시)

```
봇 시작 → 양쪽 API에서 현재 포지션 조회
  ├─ 양쪽 포지션 있음 → HOLD 상태로 복구
  ├─ 한쪽만 있음 → ⚠️ 경고, 수동 판단 요청
  └─ 양쪽 없음 → IDLE, 신규 사이클 시작
```

---

## 7. 모듈 구조

```
delta_neutral_bot/
├── config.py              # 설정 (.env 기반)
├── run_bot.py             # 엔트리포인트 (Watchdog 자동 재시작)
├── bot_core.py            # 상태머신, 사이클 관리
├── exchanges/
│   ├── standx_client.py   # StandX REST 래퍼 (JWT + ed25519 서명)
│   └── hibachi_client.py  # Hibachi SDK 래퍼 (REST만 사용)
├── strategy.py            # 펀딩레이트 비교, 방향 결정, 전환 판단
├── monitor.py             # 마진율, 잔액, 디페그 감시
├── telegram_ui.py         # 텔레그램 알림 + 버튼 UI
├── models.py              # 데이터 모델 (Position, Cycle, FundingSnapshot)
└── logs/
    ├── cycles.jsonl        # 사이클별 기록
    └── funding_history.jsonl  # 펀딩레이트 이력
```

### 모듈 의존 관계

```
run_bot.py → bot_core.py
bot_core.py → strategy.py, monitor.py, telegram_ui.py
strategy.py → exchanges/standx_client.py, exchanges/hibachi_client.py
monitor.py → exchanges/standx_client.py, exchanges/hibachi_client.py
```

---

## 8. 통신 방식 — REST 폴링 (WebSocket 없음)

Termux 경량화를 위해 WebSocket 상시 연결 없이 REST 폴링만 사용.

| 주기 | 대상 | 목적 |
|---|---|---|
| 매 1분 | 양쪽 마진율 + 현재가 | 청산 방어 (안전 최우선) |
| 매 5분 | 양쪽 잔액, 포지션 상세 | 상태 추적 |
| 매 1시간 | 양쪽 펀딩레이트 | 누적비용 갱신, 전환 판단 |

**예상 메모리**: ~20-30MB (WebSocket 사용 시 ~80-150MB 대비).

---

## 9. 텔레그램 UI

### 버튼 레이아웃

```
📊 Status    📋 History    💰 Funding
🔄 Rebalance  ⏹ Stop
```

| 버튼 | 기능 |
|---|---|
| 📊 Status | 양쪽 잔액, 포지션 방향, 노셔널, 마진율, 현재 상태, 경과시간 |
| 📋 History | 최근 사이클 5건 (방향, 보유기간, 순펀딩수익, 수수료) |
| 💰 Funding | 현재 양쪽 펀딩레이트, 누적 순펀딩비용, 다음 정산까지 남은 시간 |
| 🔄 Rebalance | 수동 사이클 전환 트리거 |
| ⏹ Stop | 양쪽 포지션 청산 후 봇 종료 |

### 자동 알림

- **사이클 진입/청산**: 방향, 노셔널, 잔액 표시
- **일일 리포트** (09:00 KST): 양쪽 잔액, 순펀딩수익, SIP-2 추정(역산), Hibachi 주간 거래량 진척
- **경고**: 마진율 50% 이하, 펀딩레이트 역전 감지
- **긴급**: 한쪽 청산 위험 시 양쪽 자동 청산 실행 알림

### SIP-2 수익 표시

직접 추적 불가 → 역산 추정치로 표시:

```
SIP-2 추정 = 현재잔액 - 시작잔액 - 누적펀딩수취 + 누적수수료지불 + 실현PnL
```

"추정" 라벨 명시.

---

## 10. 설정 (.env)

```env
# StandX
STANDX_JWT_TOKEN=
STANDX_PRIVATE_KEY=        # ed25519 서명용

# Hibachi
HIBACHI_API_KEY=
HIBACHI_PRIVATE_KEY=
HIBACHI_PUBLIC_KEY=
HIBACHI_ACCOUNT_ID=

# 전략
LEVERAGE=3
PAIR_STANDX=ETH-USD
PAIR_HIBACHI=ETH/USDT-P
MIN_HOLD_HOURS=24
MAX_HOLD_DAYS=4
COOLDOWN_HOURS=3
FUNDING_COST_THRESHOLD=0.001    # 노셔널의 0.1%
MARGIN_WARNING_PCT=50           # 마진율 경고 기준
MARGIN_EMERGENCY_PCT=33         # 자동 청산 기준

# 텔레그램
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# 일반
POLL_MARGIN_SECONDS=60
POLL_BALANCE_SECONDS=300
POLL_FUNDING_SECONDS=3600
```

---

## 11. SIP-2 주의사항 (미공개 파라미터)

SIP-2는 핵심 파라미터를 의도적으로 비공개:

| 파라미터 | 봇 대응 |
|---|---|
| Min Hold Time | 보수적으로 24시간 이상 유지 |
| Max Rewardable Leverage | 3x 사용 (안전권 추정) |
| Account/Market Cap | 초과 여부 확인 불가, $30K 노셔널은 소규모라 안전 추정 |
| Cooldown | 사이즈 증가 후 쿨다운 존재, 한 번에 풀사이즈 진입 |
| Anti-cycling | 3~4일 보유 + 2~4시간 쿨다운으로 안전권 확보 |
| Clawback | 비정상 패턴 시 보상 회수 가능, 정상 보유만 하면 OK |

**권고**: 소액 테스트 사이클 1~2회 진행 후, 실제 SIP-2 yield 적립을 확인한 뒤 정상 운용 전환.

---

## 12. Hibachi Vault 자격 요건

- 주간 거래량 $100K+ 필요 (상위 100~200명)
- 주 2회 사이클 × $60K = $120K/주 목표
- 포인트는 Volume 외에 OI, PnL, Drawdown, Fees 종합 평가
- Allocation은 1주일 후 만료, 매주 순환
- Hibachi OI가 작아 상위 진입 가능성 높음 (David 판단)

---

## 13. 리스크 요약

| 리스크 | 심각도 | 대응 |
|---|---|---|
| 펀딩레이트 순비용 | 중 | 매시간 누적 추적, threshold 초과 시 전환 |
| DUSD 디페그 | 높 | 실시간 가격 감시, 1% 초과 시 경고 |
| 한쪽 청산 | 높 | 1분 마진 체크, 20% 변동 시 선제 양쪽 청산 |
| SIP-2 yield 0 | 중 | 파라미터 미공개 리스크, 소액 테스트 선행 |
| Vault 자격 미달 | 중 | 주간 거래량 모니터링, 부족 시 알림 |
| 편측 체결 | 중 | 30초 대기, 실패 시 체결쪽 즉시 청산 |
| API 장애 | 중 | 재연결 3회, 실패 시 양쪽 청산 |
