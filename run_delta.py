"""Watchdog 엔트리포인트 — 크래시 시 자동 재시작 + 로그 자동 저장"""
import sys
import subprocess
import time
import os
import logging
from logging.handlers import RotatingFileHandler

BOT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BOT_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "bot.log")
STOP_FILE = os.path.join(BOT_DIR, ".stop_bot")
MAX_CRASHES = 10
CRASH_WINDOW = 300

# 콘솔 + 파일 동시 출력 (10MB 단위 로테이션, 최대 5개 파일 = 50MB)
formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
file_handler = RotatingFileHandler(LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
file_handler.setFormatter(formatter)
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(formatter)

root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.addHandler(file_handler)
root_logger.addHandler(console_handler)

logger = logging.getLogger("watchdog")


def main():
    crash_times = []
    restart_count = 0
    proc = None

    if os.path.exists(STOP_FILE):
        logger.info("⏹ .stop_bot 파일 존재 — 봇 시작 거부 (수동 삭제 필요)")
        return

    logger.info("🚀 Delta Neutral Bot Watchdog 시작 (로그: %s)", LOG_FILE)

    while True:
        if os.path.exists(STOP_FILE):
            logger.info("⏹ 정지 파일 감지, 종료")
            break

        restart_count += 1
        logger.info("봇 시작 (시도 #%d)", restart_count)

        try:
            # 자식 프로세스도 같은 로그 파일에 기록 (stdout 리디렉션)
            with open(LOG_FILE, "a", encoding="utf-8") as log_fp:
                proc = subprocess.Popen(
                    [sys.executable, "-u", "-c",
                     f"import sys; sys.path.insert(0, r'{BOT_DIR}');"
                     "import asyncio; from bot_core import DeltaNeutralBot; asyncio.run(DeltaNeutralBot().run())"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    bufsize=1,
                    text=True,
                )
                # 자식 출력을 콘솔과 파일에 동시 기록
                for line in proc.stdout:
                    sys.stdout.write(line)
                    sys.stdout.flush()
                    log_fp.write(line)
                    log_fp.flush()
                exit_code = proc.wait()
        except KeyboardInterrupt:
            logger.info("Ctrl+C 감지, 봇 종료 중...")
            if proc:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
            break

        if exit_code == 0:
            logger.info("봇 정상 종료 (exit 0)")
            break

        now = time.time()
        crash_times.append(now)
        crash_times = [t for t in crash_times if now - t < CRASH_WINDOW]

        if len(crash_times) >= MAX_CRASHES:
            logger.error("💀 %d분 내 %d회 크래시, 영구 종료", CRASH_WINDOW // 60, MAX_CRASHES)
            break

        logger.warning("봇 크래시 (exit %d), 5초 후 재시작...", exit_code)
        time.sleep(5)


if __name__ == "__main__":
    main()
