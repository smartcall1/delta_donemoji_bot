"""API connection test"""
import asyncio
import logging
import sys
import os

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

from config import Config


async def test_standx():
    print("\n=== StandX 연결 테스트 ===")
    from exchanges.standx_client import StandXClient
    client = StandXClient(Config.STANDX_BASE_URL, Config.STANDX_JWT_TOKEN, Config.STANDX_PRIVATE_KEY)
    try:
        # 1. 가격 조회
        market = await client.get_market_price(Config.PAIR_STANDX)
        price = market.get("mark_price", "N/A")
        funding = market.get("funding_rate", "N/A")
        print(f"  ✅ 가격 조회: ETH mark_price = ${price}")
        print(f"  ✅ 펀딩레이트: {funding}")

        # 2. 잔액 조회
        balance = await client.get_balance()
        available = balance.get("available_balance", "N/A")
        print(f"  ✅ 잔액 조회: available = ${available}")

        # 3. 포지션 조회
        positions = await client.get_positions()
        print(f"  ✅ 포지션 조회: {len(positions)}건")

        print("  🟢 StandX 연결 성공!")
        return True
    except Exception as e:
        print(f"  🔴 StandX 연결 실패: {e}")
        return False
    finally:
        await client.close()


async def test_hibachi():
    print("\n=== Hibachi 연결 테스트 ===")
    from exchanges.hibachi_client import HibachiClient
    client = HibachiClient(Config.HIBACHI_API_KEY, Config.HIBACHI_PRIVATE_KEY,
                           Config.HIBACHI_PUBLIC_KEY, Config.HIBACHI_ACCOUNT_ID)
    try:
        # 1. 가격 조회
        price = await client.get_mark_price(Config.PAIR_HIBACHI)
        print(f"  ✅ 가격 조회: ETH mark_price = ${price:.2f}")

        # 2. 펀딩레이트
        rate = await client.get_funding_rate(Config.PAIR_HIBACHI)
        print(f"  ✅ 펀딩레이트: {rate}")

        # 3. 잔액 조회
        balance = await client.get_balance()
        print(f"  ✅ 잔액 조회: {balance}")

        # 4. 포지션 조회
        positions = await client.get_positions()
        print(f"  ✅ 포지션 조회: {len(positions)}건")

        print("  🟢 Hibachi 연결 성공!")
        return True
    except Exception as e:
        print(f"  🔴 Hibachi 연결 실패: {e}")
        return False
    finally:
        await client.close()


async def test_telegram():
    print("\n=== 텔레그램 연결 테스트 ===")
    from telegram_ui import TelegramUI
    tg = TelegramUI(Config.TELEGRAM_BOT_TOKEN, Config.TELEGRAM_CHAT_ID)
    try:
        msg_id = await tg.send_message("🧪 Delta Neutral Bot 연결 테스트 성공!")
        if msg_id:
            print(f"  ✅ 메시지 전송 성공 (msg_id={msg_id})")
            print("  🟢 텔레그램 연결 성공!")
            return True
        else:
            print("  🔴 텔레그램 전송 실패")
            return False
    except Exception as e:
        print(f"  🔴 텔레그램 연결 실패: {e}")
        return False
    finally:
        await tg.close()


async def main():
    print("=" * 50)
    print("Delta Neutral Bot — 연결 테스트")
    print("=" * 50)

    # .env 검증
    errors = Config.validate()
    if errors:
        print("\n🔴 .env 설정 오류:")
        for e in errors:
            print(f"  - {e}")
        return

    sx = await test_standx()
    hb = await test_hibachi()
    tg = await test_telegram()

    print("\n" + "=" * 50)
    print("결과 요약:")
    print(f"  StandX:   {'🟢 OK' if sx else '🔴 FAIL'}")
    print(f"  Hibachi:  {'🟢 OK' if hb else '🔴 FAIL'}")
    print(f"  Telegram: {'🟢 OK' if tg else '🔴 FAIL'}")

    if sx and hb and tg:
        print("\n✅ 전부 정상! python run_bot.py 실행 가능")
    else:
        print("\n❌ 실패 항목 수정 후 다시 테스트하시오")
    print("=" * 50)


if __name__ == "__main__":
    asyncio.run(main())
