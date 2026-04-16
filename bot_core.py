# bot_core.py
"""상태머신 기반 델타 뉴트럴 봇 코어"""
from __future__ import annotations

import os
import json
import time
import logging
import asyncio
from datetime import datetime, timezone

from config import Config
from models import CycleState, BotState, Position, Cycle, FundingSnapshot
from exchanges.standx_client import StandXClient, StandXWSClient
from exchanges.hibachi_client import HibachiClient, HibachiWSClient
from strategy import normalize_funding_to_8h, decide_direction, should_exit_cycle, calc_notional
from monitor import MarginLevel, check_margin_level, estimate_sip2_yield
from telegram_ui import TelegramUI

logger = logging.getLogger(__name__)


class DeltaNeutralBot:
    def __init__(self):
        Config.ensure_dirs()

        # 거래소 클라이언트
        self.standx = StandXClient(
            base_url=Config.STANDX_BASE_URL,
            jwt_token=Config.STANDX_JWT_TOKEN,
            private_key_hex=Config.STANDX_PRIVATE_KEY,
        )
        self.hibachi = HibachiClient(
            api_key=Config.HIBACHI_API_KEY,
            private_key=Config.HIBACHI_PRIVATE_KEY,
            public_key=Config.HIBACHI_PUBLIC_KEY,
            account_id=Config.HIBACHI_ACCOUNT_ID,
        )

        # 텔레그램
        self.telegram = TelegramUI(Config.TELEGRAM_BOT_TOKEN, Config.TELEGRAM_CHAT_ID)

        # WebSocket 가격 피드
        self.standx_price: float = 0.0
        self.hibachi_price: float = 0.0
        self.standx_ws = StandXWSClient(
            Config.STANDX_WS_URL, Config.PAIR_STANDX, self._on_standx_price
        )
        self.hibachi_ws = HibachiWSClient(
            Config.HIBACHI_WS_URL, Config.PAIR_HIBACHI, self._on_hibachi_price
        )

        # 상태
        self.state = self._load_state()
        self._positions: dict[str, Position] = {}
        self._current_cycle: Cycle | None = None
        self._cycle_entered_at: float | None = None
        self._cooldown_until: float | None = None
        self._cumulative_funding_cost: float = 0.0
        self._initial_standx_balance: float = 0.0
        self._initial_hibachi_balance: float = 0.0
        self._running = False

    def _load_state(self) -> BotState:
        path = os.path.join(Config.LOG_DIR, "bot_state.json")
        if os.path.exists(path):
            try:
                return BotState.load(path)
            except Exception as e:
                logger.warning("상태 파일 로드 실패, 초기화: %s", e)
        return BotState()

    def _save_state(self):
        self.state.save(os.path.join(Config.LOG_DIR, "bot_state.json"))

    def _on_standx_price(self, price: float):
        self.standx_price = price

    def _on_hibachi_price(self, price: float):
        self.hibachi_price = price

    def _parse_direction(self, direction: str) -> tuple[str, str]:
        standx_side = "BUY" if "standx_long" in direction else "SELL"
        hibachi_side = "SELL" if "hibachi_short" in direction else "BUY"
        return standx_side, hibachi_side

    async def _check_margin_safety(self) -> MarginLevel:
        """양쪽 마진율 중 낮은 쪽 기준으로 위험도 판단"""
        worst = MarginLevel.NORMAL
        for key, pos in self._positions.items():
            price = self.standx_price if pos.exchange == "standx" else self.hibachi_price
            if price <= 0:
                continue
            ratio = pos.calc_margin_ratio(price)
            level = check_margin_level(ratio, Config.MARGIN_WARNING_PCT, Config.MARGIN_EMERGENCY_PCT)
            if level == MarginLevel.EMERGENCY:
                return MarginLevel.EMERGENCY
            if level == MarginLevel.WARNING:
                worst = MarginLevel.WARNING
        return worst

    async def _execute_enter(self, direction: str, notional: float):
        """양쪽 동시 진입"""
        standx_side, hibachi_side = self._parse_direction(direction)
        price = self.standx_price or self.hibachi_price
        if price <= 0:
            logger.error("가격 정보 없음, 진입 불가")
            return False

        quantity = notional / price
        # Limit 주문 양쪽 제출
        try:
            standx_result = await self.standx.place_limit_order(
                Config.PAIR_STANDX, standx_side, price, round(quantity, 4)
            )
            hibachi_result = await self.hibachi.place_limit_order(
                Config.PAIR_HIBACHI, hibachi_side, price, round(quantity, 6)
            )
        except Exception as e:
            logger.error("주문 제출 실패: %s", e)
            await self.telegram.send_alert(f"🚨 주문 제출 실패: {e}")
            return False

        # 체결 확인 (30초 대기)
        await asyncio.sleep(30)

        # 체결 상태 확인 (양쪽 포지션 조회)
        standx_positions = await self.standx.get_positions()
        hibachi_positions = await self.hibachi.get_positions()

        has_standx = any(p.get("symbol") == Config.PAIR_STANDX for p in standx_positions)
        has_hibachi = any(p.get("symbol") == Config.PAIR_HIBACHI for p in hibachi_positions)

        if has_standx and has_hibachi:
            # 양쪽 체결 성공
            margin = notional / Config.LEVERAGE
            self._positions["standx"] = Position(
                exchange="standx", symbol=Config.PAIR_STANDX,
                side="LONG" if standx_side == "BUY" else "SHORT",
                notional=notional, entry_price=price,
                leverage=Config.LEVERAGE, margin=margin,
            )
            self._positions["hibachi"] = Position(
                exchange="hibachi", symbol=Config.PAIR_HIBACHI,
                side="LONG" if hibachi_side == "BUY" else "SHORT",
                notional=notional, entry_price=price,
                leverage=Config.LEVERAGE, margin=margin,
            )
            return True

        # 편측 체결 — 체결된 쪽 청산
        if has_standx and not has_hibachi:
            logger.warning("편측 체결: StandX만 체결, 청산")
            await self.standx.close_position(Config.PAIR_STANDX, standx_side, round(quantity, 4))
            await self.telegram.send_alert("⚠️ 편측 체결: StandX만 체결됨, 청산 완료")
        elif has_hibachi and not has_standx:
            logger.warning("편측 체결: Hibachi만 체결, 청산")
            await self.hibachi.close_position(Config.PAIR_HIBACHI, hibachi_side, round(quantity, 6))
            await self.telegram.send_alert("⚠️ 편측 체결: Hibachi만 체결됨, 청산 완료")
        return False

    async def _execute_exit(self):
        """양쪽 동시 청산"""
        for key, pos in list(self._positions.items()):
            try:
                quantity = pos.notional / pos.entry_price
                if pos.exchange == "standx":
                    side = "BUY" if pos.side == "LONG" else "SELL"
                    await self.standx.close_position(Config.PAIR_STANDX, side, round(quantity, 4))
                else:
                    side = "BUY" if pos.side == "LONG" else "SELL"
                    await self.hibachi.close_position(Config.PAIR_HIBACHI, side, round(quantity, 6))
            except Exception as e:
                logger.error("%s 청산 실패: %s", key, e)
                await self.telegram.send_alert(f"🚨 {key} 청산 실패: {e}")
        self._positions.clear()

    def _log_cycle(self, cycle: Cycle):
        path = os.path.join(Config.LOG_DIR, "cycles.jsonl")
        with open(path, "a") as f:
            f.write(cycle.to_jsonl() + "\n")

    async def _run_state_machine(self):
        """상태머신 1회 평가"""
        state = self.state.cycle_state

        if state == CycleState.IDLE:
            if self._cooldown_until and time.time() < self._cooldown_until:
                return
            self.state.cycle_state = CycleState.ANALYZE
            self._save_state()

        elif state == CycleState.ANALYZE:
            # 펀딩레이트 조회 및 방향 결정
            try:
                sx_data = await self.standx.get_funding_rate(Config.PAIR_STANDX)
                sx_rate = float(sx_data.get("funding_rate", 0))
                hb_rate = await self.hibachi.get_funding_rate(Config.PAIR_HIBACHI)

                sx_8h = normalize_funding_to_8h(sx_rate, period_hours=1)
                hb_8h = normalize_funding_to_8h(hb_rate, period_hours=8)

                direction = decide_direction(sx_8h, hb_8h)

                # 잔액 조회 → 노셔널 계산
                sx_bal = await self.standx.get_balance()
                hb_bal = await self.hibachi.get_balance()
                sx_available = float(sx_bal.get("available_balance", 0))
                hb_available = float(hb_bal.get("available", 0))

                self.state.standx_balance = sx_available
                self.state.hibachi_balance = hb_available
                self._initial_standx_balance = sx_available
                self._initial_hibachi_balance = hb_available

                notional = calc_notional(sx_available, hb_available, Config.LEVERAGE)

                logger.info("분석 완료: 방향=%s, 노셔널=$%.2f", direction, notional)
                self.state.cycle_state = CycleState.ENTER
                self._save_state()

                # ENTER 즉시 실행
                self.state.current_cycle_id += 1
                success = await self._execute_enter(direction, notional)
                if success:
                    self._cycle_entered_at = time.time()
                    self._cumulative_funding_cost = 0.0
                    self._current_cycle = Cycle(
                        cycle_id=self.state.current_cycle_id,
                        direction=direction,
                        notional=notional,
                    )
                    self.state.cycle_state = CycleState.HOLD
                    # 거래량 추적
                    self.state.weekly_hibachi_volume += notional
                    fee = notional * 0.0001  # StandX maker 0.01%
                    self.state.cumulative_fees += fee
                    await self.telegram.send_alert(
                        f"✅ 사이클 #{self.state.current_cycle_id} 진입\n"
                        f"방향: {direction}\n"
                        f"노셔널: ${notional:,.0f}\n"
                        f"StandX: ${sx_available:,.2f} / Hibachi: ${hb_available:,.2f}"
                    )
                else:
                    self.state.cycle_state = CycleState.IDLE
                self._save_state()

            except Exception as e:
                logger.error("분석/진입 실패: %s", e)
                await self.telegram.send_alert(f"🚨 분석/진입 실패: {e}")
                self.state.cycle_state = CycleState.IDLE
                self._save_state()

        elif state == CycleState.HOLD:
            if not self._cycle_entered_at:
                return
            hold_hours = (time.time() - self._cycle_entered_at) / 3600

            # 마진 안전 체크
            margin_level = await self._check_margin_safety()
            if margin_level == MarginLevel.EMERGENCY:
                logger.warning("🚨 긴급 청산!")
                await self.telegram.send_alert("🚨 마진율 위험! 양쪽 긴급 청산 실행!")
                await self._execute_exit()
                self.state.cycle_state = CycleState.COOLDOWN
                self._cooldown_until = time.time() + Config.COOLDOWN_HOURS * 3600
                self._save_state()
                return
            elif margin_level == MarginLevel.WARNING:
                worst_ratio = min(
                    (p.calc_margin_ratio(self.standx_price if p.exchange == "standx" else self.hibachi_price)
                     for p in self._positions.values() if (self.standx_price if p.exchange == "standx" else self.hibachi_price) > 0),
                    default=100.0,
                )
                await self.telegram.send_alert(f"⚠️ 마진율 경고: {worst_ratio:.1f}%")

            # 전환 판단
            worst_margin = min(
                (p.calc_margin_ratio(self.standx_price if p.exchange == "standx" else self.hibachi_price)
                 for p in self._positions.values() if (self.standx_price if p.exchange == "standx" else self.hibachi_price) > 0),
                default=100.0,
            )
            if should_exit_cycle(
                hold_hours=hold_hours,
                min_hold_hours=Config.MIN_HOLD_HOURS,
                cumulative_funding_cost=self._cumulative_funding_cost,
                funding_threshold=Config.FUNDING_COST_THRESHOLD,
                margin_ratio_min=worst_margin,
                margin_emergency_pct=Config.MARGIN_EMERGENCY_PCT,
                max_hold_days=Config.MAX_HOLD_DAYS,
            ):
                self.state.cycle_state = CycleState.EXIT
                self._save_state()

        elif state == CycleState.EXIT:
            await self._execute_exit()

            # 사이클 기록
            if self._current_cycle:
                self._current_cycle.exited_at = time.time()
                sx_bal = await self.standx.get_balance()
                hb_bal = await self.hibachi.get_balance()
                self._current_cycle.standx_balance_after = float(sx_bal.get("available_balance", 0))
                self._current_cycle.hibachi_balance_after = float(hb_bal.get("available", 0))
                self.state.standx_balance = self._current_cycle.standx_balance_after
                self.state.hibachi_balance = self._current_cycle.hibachi_balance_after
                # 거래량 추적 (청산)
                self.state.weekly_hibachi_volume += self._current_cycle.notional
                fee = self._current_cycle.notional * 0.0001
                self.state.cumulative_fees += fee

                self._log_cycle(self._current_cycle)

                hold_h = (self._current_cycle.exited_at - self._current_cycle.entered_at) / 3600
                await self.telegram.send_alert(
                    f"🔄 사이클 #{self._current_cycle.cycle_id} 청산\n"
                    f"보유: {hold_h:.1f}시간\n"
                    f"StandX: ${self._current_cycle.standx_balance_after:,.2f}\n"
                    f"Hibachi: ${self._current_cycle.hibachi_balance_after:,.2f}"
                )
                self._current_cycle = None

            self.state.cycle_state = CycleState.COOLDOWN
            self._cooldown_until = time.time() + Config.COOLDOWN_HOURS * 3600
            self._save_state()

        elif state == CycleState.COOLDOWN:
            if self._cooldown_until and time.time() >= self._cooldown_until:
                self._cooldown_until = None
                self.state.cycle_state = CycleState.IDLE
                self._save_state()
                logger.info("쿨다운 종료, IDLE 복귀")

    async def _recovery_check(self):
        """봇 재시작 시 기존 포지션 복구"""
        sx_positions = await self.standx.get_positions()
        hb_positions = await self.hibachi.get_positions()

        has_sx = any(p.get("symbol") == Config.PAIR_STANDX for p in sx_positions)
        has_hb = any(p.get("symbol") == Config.PAIR_HIBACHI for p in hb_positions)

        if has_sx and has_hb:
            logger.info("양쪽 포지션 감지 → HOLD 복구")
            self.state.cycle_state = CycleState.HOLD
            if not self._cycle_entered_at:
                self._cycle_entered_at = time.time()
            await self.telegram.send_alert("🔁 봇 재시작: 기존 포지션 감지, HOLD 상태 복구")
        elif has_sx or has_hb:
            side = "StandX" if has_sx else "Hibachi"
            logger.warning("편측 포지션 감지: %s", side)
            await self.telegram.send_alert(f"⚠️ 봇 재시작: {side}에만 포지션 존재! 수동 확인 필요")
        else:
            if self.state.cycle_state not in (CycleState.IDLE, CycleState.COOLDOWN):
                self.state.cycle_state = CycleState.IDLE
        self._save_state()

    def _register_telegram_callbacks(self):
        async def on_status(cb):
            sx_p = self.standx_price
            hb_p = self.hibachi_price
            text = (
                f"📊 <b>Status</b>\n"
                f"상태: {self.state.cycle_state}\n"
                f"StandX: ${self.state.standx_balance:,.2f} (ETH ${sx_p:,.2f})\n"
                f"Hibachi: ${self.state.hibachi_balance:,.2f} (ETH ${hb_p:,.2f})\n"
                f"주간 거래량: ${self.state.weekly_hibachi_volume:,.0f} / $100,000"
            )
            await self.telegram.send_main_menu(text)

        async def on_history(cb):
            path = os.path.join(Config.LOG_DIR, "cycles.jsonl")
            if not os.path.exists(path):
                await self.telegram.send_alert("📋 아직 완료된 사이클 없음")
                return
            lines = open(path).readlines()[-5:]
            text = "📋 <b>최근 사이클</b>\n\n"
            for line in lines:
                c = json.loads(line)
                hold = ((c.get("exited_at", 0) or 0) - c.get("entered_at", 0)) / 3600
                text += (
                    f"#{c['cycle_id']} {c['direction'][:20]}\n"
                    f"  보유: {hold:.1f}h / 노셔널: ${c['notional']:,.0f}\n\n"
                )
            await self.telegram.send_alert(text)

        async def on_funding(cb):
            try:
                sx_data = await self.standx.get_funding_rate(Config.PAIR_STANDX)
                sx_rate = float(sx_data.get("funding_rate", 0))
                hb_rate = await self.hibachi.get_funding_rate(Config.PAIR_HIBACHI)
                text = (
                    f"💰 <b>Funding Rates</b>\n"
                    f"StandX (1H): {sx_rate:.6f}\n"
                    f"Hibachi (8H): {hb_rate:.6f}\n"
                    f"누적 비용: {self._cumulative_funding_cost:.6f}"
                )
            except Exception as e:
                text = f"💰 펀딩레이트 조회 실패: {e}"
            await self.telegram.send_alert(text)

        async def on_rebalance(cb):
            if self.state.cycle_state == CycleState.HOLD:
                self.state.cycle_state = CycleState.EXIT
                self._save_state()
                await self.telegram.send_alert("🔄 수동 리밸런싱 트리거됨")
            else:
                await self.telegram.send_alert(f"현재 상태({self.state.cycle_state})에서는 리밸런싱 불가")

        async def on_stop(cb):
            await self.telegram.send_alert("⏹ 봇 종료 중... 포지션 청산")
            if self._positions:
                await self._execute_exit()
            self._running = False

        self.telegram.register_callback("status", on_status)
        self.telegram.register_callback("history", on_history)
        self.telegram.register_callback("funding", on_funding)
        self.telegram.register_callback("rebalance", on_rebalance)
        self.telegram.register_callback("stop", on_stop)

    async def run(self):
        """메인 루프"""
        self._running = True
        self._register_telegram_callbacks()

        # 시작 알림
        await self.telegram.send_main_menu(
            f"🚀 Delta Neutral Bot 시작\n상태: {self.state.cycle_state}"
        )

        # 포지션 복구
        await self._recovery_check()

        # WebSocket 시작 (백그라운드)
        ws_tasks = [
            asyncio.create_task(self.standx_ws.connect()),
            asyncio.create_task(self.hibachi_ws.connect()),
        ]

        # REST fallback 가격 업데이트
        last_balance_check = 0
        last_funding_check = 0

        try:
            while self._running:
                now = time.time()

                # 텔레그램 폴링
                await self.telegram.poll_updates()

                # REST fallback: 가격 (WS가 안 되면)
                if self.standx_price == 0 or self.hibachi_price == 0:
                    try:
                        sx_market = await self.standx.get_market_price(Config.PAIR_STANDX)
                        self.standx_price = float(sx_market.get("mark_price", 0))
                        self.hibachi_price = await self.hibachi.get_mark_price(Config.PAIR_HIBACHI)
                    except Exception:
                        pass

                # 상태머신 평가
                await self._run_state_machine()

                # 마진 안전 체크 (HOLD 상태에서 WS 가격 기반)
                if self.state.cycle_state == CycleState.HOLD and self._positions:
                    level = await self._check_margin_safety()
                    if level == MarginLevel.EMERGENCY:
                        logger.warning("🚨 WS 가격 기반 긴급 청산!")
                        await self.telegram.send_alert("🚨 실시간 가격 기반 긴급 청산!")
                        await self._execute_exit()
                        self.state.cycle_state = CycleState.COOLDOWN
                        self._cooldown_until = now + Config.COOLDOWN_HOURS * 3600
                        self._save_state()

                # 잔액 체크 (5분)
                if now - last_balance_check > Config.POLL_BALANCE_SECONDS:
                    last_balance_check = now
                    try:
                        sx_bal = await self.standx.get_balance()
                        hb_bal = await self.hibachi.get_balance()
                        self.state.standx_balance = float(sx_bal.get("available_balance", 0))
                        self.state.hibachi_balance = float(hb_bal.get("available", 0))
                        self._save_state()
                    except Exception as e:
                        logger.error("잔액 조회 실패: %s", e)

                # 펀딩레이트 체크 (1시간)
                if now - last_funding_check > Config.POLL_FUNDING_SECONDS:
                    last_funding_check = now
                    try:
                        sx_data = await self.standx.get_funding_rate(Config.PAIR_STANDX)
                        sx_rate = float(sx_data.get("funding_rate", 0))
                        hb_rate = await self.hibachi.get_funding_rate(Config.PAIR_HIBACHI)
                        snap = FundingSnapshot(sx_rate, hb_rate, now)
                        # 로그
                        path = os.path.join(Config.LOG_DIR, "funding_history.jsonl")
                        with open(path, "a") as f:
                            f.write(json.dumps({"sx": sx_rate, "hb": hb_rate, "ts": now}) + "\n")
                    except Exception as e:
                        logger.error("펀딩레이트 조회 실패: %s", e)

                await asyncio.sleep(5)  # 5초 간격 루프

        finally:
            # 정리
            await self.standx_ws.disconnect()
            await self.hibachi_ws.disconnect()
            await self.standx.close()
            await self.hibachi.close()
            await self.telegram.close()
            for t in ws_tasks:
                t.cancel()
