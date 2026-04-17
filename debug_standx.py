"""StandX 주문 디버그 — 봇과 똑같은 로직으로 SHORT 주문 시도"""
import sys, asyncio, os, json
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, '.')
from config import Config
from exchanges.standx_client import StandXClient


async def main():
    c = StandXClient(Config.STANDX_BASE_URL, Config.STANDX_JWT_TOKEN, Config.STANDX_PRIVATE_KEY)

    # 봇과 동일하게 가격 계산
    market = await c.get_market_price('ETH-USD')
    sx_price = float(market.get('mark_price', 0))
    print(f'Mark price: {sx_price}')

    # SHORT 슬리피지 0.4%
    sx_order_price = sx_price * 0.996
    sx_order_price = round(sx_order_price / 0.1) * 0.1
    sx_order_price = round(sx_order_price, 1)
    print(f'Order price (SHORT @ -0.4%): {sx_order_price}')
    print(f'String form: "{str(sx_order_price)}"')

    # 실제 주문 (소량 0.005 ETH = ~$11)
    body = {
        'symbol': 'ETH-USD',
        'side': 'sell',
        'order_type': 'limit',
        'price': str(sx_order_price),
        'qty': '0.005',
        'time_in_force': 'gtc',
        'reduce_only': False,
    }
    print(f'\nBody: {json.dumps(body, indent=2)}')

    raw = await c._signed_request('POST', '/api/new_order', body)
    print(f'\nResponse: {json.dumps(raw, indent=2)}')

    # 잠시 대기
    await asyncio.sleep(5)

    # 포지션/주문 확인
    positions = await c.get_positions()
    print(f'\nPositions: {len(positions)}')
    for p in positions:
        print(f'  qty={p.get("qty")} side={p.get("side")} entry={p.get("entry_price")}')

    # 주문 확인
    try:
        orders_resp = await c._request('GET', '/api/query_open_orders', {'symbol': 'ETH-USD'})
        orders = orders_resp.get('data', [])
        print(f'\nOpen orders: {len(orders)}')
        for o in orders:
            print(f'  {json.dumps(o, indent=2)}')
    except Exception as e:
        print(f'Orders query failed: {e}')

    await c.close()


asyncio.run(main())
