# Delta Neutral Bot — Termux 설치 가이드

## 1. Termux 기본 설정

```bash
# 패키지 업데이트
pkg update && pkg upgrade -y

# Python + 필수 도구 설치
pkg install python python-pip git openssh -y

# 빌드 도구 (pynacl 컴파일에 필요)
pkg install build-essential libsodium -y
```

## 2. 프로젝트 클론

```bash
# 작업 디렉토리
cd ~
mkdir -p bots && cd bots

# 로컬에서 전송 (PC에서 실행)
# scp -r D:\Codes\delta_neutral_bot\ user@phone_ip:~/bots/

# 또는 USB로 복사 후
cp -r /sdcard/Download/delta_neutral_bot ~/bots/
```

## 3. 의존성 설치

```bash
cd ~/bots/delta_neutral_bot

# 가상환경 생성 (권장)
python -m venv venv
source venv/bin/activate

# 의존성 설치
pip install -r requirements.txt

# pynacl 설치 실패 시 (libsodium 경로 문제)
SODIUM_INSTALL=system pip install pynacl
```

## 4. .env 설정

```bash
# .env.example을 복사해서 편집
cp .env.example .env
nano .env

# 각 항목 입력:
# STANDX_JWT_TOKEN=eyJ...
# STANDX_PRIVATE_KEY=2j...
# HIBACHI_API_KEY=...
# HIBACHI_PRIVATE_KEY=...
# HIBACHI_PUBLIC_KEY=0x...
# HIBACHI_ACCOUNT_ID=28584
# TELEGRAM_BOT_TOKEN=...
# TELEGRAM_CHAT_ID=...
```

## 5. 연결 테스트

```bash
python test_connection.py
```

3개 전부 🟢이면 다음 단계.

## 6. 봇 실행

```bash
# 기본 실행 (터미널 닫으면 종료)
python run_bot.py

# 백그라운드 실행 (터미널 닫아도 유지)
nohup python -u run_bot.py > bot.log 2>&1 &
echo $! > bot.pid

# 로그 실시간 확인
tail -f bot.log
```

## 7. Termux 백그라운드 유지 설정

```bash
# Termux가 Android에 의해 kill 당하지 않게
termux-wake-lock

# 알림바에 Termux 고정 (Android 설정)
# 설정 → 앱 → Termux → 배터리 → 제한 없음
```

## 8. 봇 관리 명령어

```bash
# 로그 확인
tail -f bot.log
tail -100 bot.log

# 봇 상태 확인
cat logs/bot_state.json | python -m json.tool

# 사이클 히스토리
cat logs/cycles.jsonl

# 봇 중지 (안전 — 포지션 청산 후 종료)
# 텔레그램에서 ⏹ Stop 버튼 사용 (권장)

# 봇 강제 중지 (포지션 유지됨, 재시작 시 복구)
kill $(cat bot.pid)

# 봇 재시작
nohup python -u run_bot.py > bot.log 2>&1 &
echo $! > bot.pid
```

## 9. 자동 시작 (Termux:Boot 앱)

```bash
# Termux:Boot 앱 설치 (F-Droid에서)
# 부트 스크립트 생성
mkdir -p ~/.termux/boot
cat > ~/.termux/boot/start_bot.sh << 'EOF'
#!/data/data/com.termux/files/usr/bin/bash
termux-wake-lock
cd ~/bots/delta_neutral_bot
source venv/bin/activate
nohup python -u run_bot.py > bot.log 2>&1 &
echo $! > bot.pid
EOF
chmod +x ~/.termux/boot/start_bot.sh
```

## 10. 트러블슈팅

### pynacl 설치 실패
```bash
pkg install libsodium
SODIUM_INSTALL=system pip install pynacl --no-binary :all:
```

### websockets 연결 안 됨
```bash
# DNS 문제일 수 있음
termux-setup-storage
echo "nameserver 8.8.8.8" > $PREFIX/etc/resolv.conf
```

### 메모리 부족
```bash
# 스왑 파일 생성 (루팅 필요)
# 또는 다른 앱 종료 후 실행
```

### 봇이 자꾸 크래시
```bash
# 로그 확인
tail -50 bot.log

# 상태 파일 확인
cat logs/bot_state.json

# 상태 초기화 (포지션 없을 때만!)
rm logs/bot_state.json
```
