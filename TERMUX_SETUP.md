# Delta Neutral Bot — Termux 설치 가이드

## 1. Termux 기본 설정

```bash
pkg update && pkg upgrade -y

# Python + 빌드 도구
pkg install python python-pip git -y

# C 확장 컴파일에 필요 (pynacl, aiohttp)
pkg install clang make libffi openssl pkg-config libsodium -y
```

## 2. 프로젝트 다운로드

```bash
cd ~
git clone https://github.com/smartcall1/delta_donemoji_bot.git
cd delta_donemoji_bot
```

## 3. 의존성 설치

```bash
# pynacl — libsodium 시스템 라이브러리 사용 (컴파일 우회)
SODIUM_INSTALL=system pip install pynacl --no-cache-dir

# aiohttp — C 확장 빌드 (시간 좀 걸림)
pip install aiohttp --no-cache-dir

# 나머지 한번에
pip install -r requirements.txt --no-cache-dir
```

### 설치 실패 시

```bash
# pynacl 여전히 실패하면
pip install pynacl --no-binary :all: --no-cache-dir

# aiohttp 빌드 실패하면 (multidict/yarl 문제)
pip install multidict --no-binary :all:
pip install yarl --no-binary :all:
pip install aiohttp --no-binary :all:
```

## 4. .env 설정

```bash
cp .env.example .env
nano .env
# StandX JWT + Private Key, Hibachi API Key, 텔레그램 토큰 입력
```

## 5. 연결 테스트

```bash
python test_connection.py
# StandX 🟢, Hibachi 🟢, Telegram 🟢 확인
```

## 6. 봇 실행

```bash
# 백그라운드 실행
termux-wake-lock
nohup python -u run_bot.py > bot.log 2>&1 &
echo $! > bot.pid

# 로그 확인
tail -f bot.log
```

## 7. 봇 관리

```bash
# 중지 (텔레그램 ⏹ Stop 버튼 권장)
# 강제 중지
kill $(cat bot.pid)

# 재시작
nohup python -u run_bot.py > bot.log 2>&1 &
echo $! > bot.pid

# 상태 확인
cat logs/bot_state.json | python -m json.tool
```

## 8. 자동 시작 (Termux:Boot)

F-Droid에서 Termux:Boot 설치 후:

```bash
mkdir -p ~/.termux/boot
cat > ~/.termux/boot/start_bot.sh << 'EOF'
#!/data/data/com.termux/files/usr/bin/bash
termux-wake-lock
cd ~/delta_donemoji_bot
nohup python -u run_bot.py > bot.log 2>&1 &
echo $! > bot.pid
EOF
chmod +x ~/.termux/boot/start_bot.sh
```

Android 설정 → 앱 → Termux → 배터리 → 제한 없음

## 9. 업데이트

```bash
cd ~/delta_donemoji_bot
git pull
# .env는 gitignore라 유지됨
```
