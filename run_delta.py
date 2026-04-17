"""Watchdog 엔트리포인트 — 크래시 시 자동 재시작"""
import sys
import subprocess
import time
import os
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("watchdog")

BOT_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_core.py")
STOP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".stop_bot")
MAX_CRASHES = 10
CRASH_WINDOW = 300


def main():
    crash_times = []
    restart_count = 0
    proc = None

    # .stop_bot 파일이 있으면 명시적 정지 상태 — 사용자가 수동 삭제해야 재시작
    if os.path.exists(STOP_FILE):
        logger.info("⏹ .stop_bot 파일 존재 — 봇 시작 거부 (수동 삭제 필요)")
        return

    logger.info("🚀 Delta Neutral Bot Watchdog 시작")

    while True:
        if os.path.exists(STOP_FILE):
            logger.info("⏹ 정지 파일 감지, 종료")
            break

        restart_count += 1
        logger.info("봇 시작 (시도 #%d)", restart_count)

        bot_dir = os.path.dirname(os.path.abspath(__file__))
        try:
            proc = subprocess.Popen(
                [sys.executable, "-c",
                 f"import sys; sys.path.insert(0, r'{bot_dir}');"
                 "import asyncio; from bot_core import DeltaNeutralBot; asyncio.run(DeltaNeutralBot().run())"],
                stdout=sys.stdout,
                stderr=sys.stderr,
            )
            exit_code = proc.wait()
        except KeyboardInterrupt:
            logger.info("Ctrl+C 감지, 종료")
            if proc:
                proc.terminate()
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
