# bot_core.py
"""상태머신 기반 델타 뉴트럴 봇 코어

오딧 수정사항:
  C1: 편측 체결 해제 시 재시도+검증
  C2: 청산 실패 시 positions 유지 (성공한 것만 제거)
  C3: 양쪽 개별 가격 조회 후 주문
  C4: 중복 긴급 청산 제거 (메인루프에서 제거)
  C6: 복구 시 Position 객체 재구성
  C7: Stop 레이스 컨디션 방어 (_running 체크)
  I1: 펀딩비용 누적 갱신
  I6: 보유시간 상태에 저장
  I7: 방향(direction) 상태에 저장
  I8: 파일 리소스 누수 수정
  M5: 주간 거래량 리셋
  S1: 진입 3회 재시도
  S2: DUSD 디페그 감시
  S4: 일일 리포트 (09:00 KST)
"""
from __future__ import annotations

import os
import json
import time
import logging
import asyncio
from datetime import datetime, timezone, timedelta

from config import Config
from models import CycleState, BotState, Position, Cycle, FundingSnapshot
from exchanges.standx_client import StandXClient, StandXWSClient
from exchanges.hibachi_client import HibachiClient, HibachiWSClient
from strategy import normalize_funding_to_8h, decide_direction, should_exit_cycle, calc_notional, is_opposite_direction_better
from monitor import MarginLevel, check_margin_level, estimate_sip2_yield
from telegram_ui import TelegramUI

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
ENTRY_MAX_RETRIES = 3
API_FAIL_EMERGENCY_THRESHOLD = 10  # S3: 연속 REST 실패 횟수 → 긴급 청산


class DeltaNeutralBot:
    def __init__(self):
        Config.ensure_dirs()
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )

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

        self.telegram = TelegramUI(Config.TELEGRAM_BOT_TOKEN, Config.TELEGRAM_CHAT_ID)

        self.standx_price: float = 0.0
        self.hibachi_price: float = 0.0
        self.standx_ws = StandXWSClient(
            Config.STANDX_WS_URL, Config.PAIR_STANDX, self._on_standx_price
        )
        self.hibachi_ws = HibachiWSClient(
            Config.HIBACHI_WS_URL, Config.PAIR_HIBACHI, self._on_hibachi_price
        )

        self.state = self._load_state()
        self._positions: dict[str, Position] = {}
        self._current_cycle: Cycle | None = None
        self._cycle_entered_at: float | None = self.state.cycle_entered_at or None
        self._cooldown_until: float | None = self.state.cooldown_until or None
        self._cumulative_funding_cost: float = self.state.cumulative_funding_cost
        self._initial_standx_balance: float = self.state.initial_standx_balance
        self._initial_hibachi_balance: float = self.state.initial_hibachi_balance
        self._running = False
        self._last_warning_time: float = 0.0
        self._last_daily_report: float = 0.0
        self._consecutive_api_failures: int = 0  # S3: 연속 API 실패 추적

    def _load_state(self) -> BotState:
        path = os.path.join(Config.LOG_DIR, "bot_state.json")
        if os.path.exists(path):
            try:
                return BotState.load(path)
            except Exception as e:
                logger.warning("상태 파일 로드 실패, 초기화: %s", e)
        return BotState()

    def _save_state(self):
        # I6+I7+RM-6: 휘발성 상태도 저장
        if self._cycle_entered_at:
            self.state.cycle_entered_at = self._cycle_entered_at
        if self._cooldown_until:
            self.state.cooldown_until = self._cooldown_until
        self.state.cumulative_funding_cost = self._cumulative_funding_cost
        self.state.save(os.path.join(Config.LOG_DIR, "bot_state.json"))

    def _on_standx_price(self, price: float):
        self.standx_price = price

    def _on_hibachi_price(self, price: float):
        self.hibachi_price = price

    @staticmethod
    def _parse_sx_balance(bal: dict) -> float:
        """StandX 잔액 파싱 — equity(총 자산) 우선"""
        for key in ("equity", "balance", "cross_balance", "cross_available"):
            if key in bal:
                return float(bal[key])
        return 0.0

    @staticmethod
    def _parse_hb_balance(bal: dict) -> float:
        """Hibachi 잔액 파싱 — SDK dataclass 변환 후 키 호환"""
        for key in ("available", "balance", "equity", "total"):
            if key in bal:
                return float(bal[key])
        return 0.0

    def _parse_direction(self, direction: str) -> tuple[str, str]:
        standx_side = "BUY" if "standx_long" in direction else "SELL"
        hibachi_side = "SELL" if "hibachi_short" in direction else "BUY"
        return standx_side, hibachi_side

    def _get_worst_margin(self) -> float:
        """양쪽 마진율 중 최소값 반환"""
        ratios = []
        for pos in self._positions.values():
            price = self.standx_price if pos.exchange == "standx" else self.hibachi_price
            if price > 0:
                ratios.append(pos.calc_margin_ratio(price))
        return min(ratios) if ratios else 100.0

    async def _check_margin_safety(self) -> MarginLevel:
        """델타 뉴트럴: 한 쪽 손실이 다른 쪽 이익으로 상쇄되므로
        '실제로 청산 위험한 쪽(손실 쪽)이 거래소 유지마진에 가까워졌을 때'만 위험 판단.
        """
        worst = MarginLevel.NORMAL
        for pos in self._positions.values():
            price = self.standx_price if pos.exchange == "standx" else self.hibachi_price
            if price <= 0:
                continue
            # 손실 중인 쪽만 체크 (이익 중인 쪽은 청산 위험 없음)
            pnl = pos.calc_unrealized_pnl(price)
            if pnl >= 0:
                continue
            ratio = pos.calc_margin_ratio(price)
            level = check_margin_level(ratio, Config.MARGIN_WARNING_PCT, Config.MARGIN_EMERGENCY_PCT)
            if level == MarginLevel.EMERGENCY:
                return MarginLevel.EMERGENCY
            if level == MarginLevel.WARNING:
                worst = MarginLevel.WARNING
        return worst

    async def _execute_enter(self, direction: str, notional: float) -> bool:
        """양쪽 동시 진입 — C3: 개별 가격, S1: 3회 재시도"""
        standx_side, hibachi_side = self._parse_direction(direction)

        for attempt in range(ENTRY_MAX_RETRIES):
            # C3: 양쪽 개별 가격 조회
            try:
                sx_market = await self.standx.get_market_price(Config.PAIR_STANDX)
                sx_price = float(sx_market.get("mark_price", 0))
                hb_price = await self.hibachi.get_mark_price(Config.PAIR_HIBACHI)
            except Exception as e:
                logger.error("가격 조회 실패 (시도 %d/%d): %s", attempt + 1, ENTRY_MAX_RETRIES, e)
                await asyncio.sleep(5)
                continue

            if sx_price <= 0 or hb_price <= 0:
                logger.error("가격 정보 없음 (sx=%.2f, hb=%.2f)", sx_price, hb_price)
                await asyncio.sleep(5)
                continue

            sx_quantity = notional / sx_price
            hb_quantity = notional / hb_price

            # 진입 슬리피지 0.4% (체결률 향상 — 0.2%는 30초 내 미체결 빈발)
            if standx_side == "BUY":
                sx_order_price = sx_price * 1.004
            else:
                sx_order_price = sx_price * 0.996
            # StandX ETH-USD tick = 0.1
            sx_order_price = round(sx_order_price / 0.1) * 0.1
            sx_order_price = round(sx_order_price, 1)

            if hibachi_side == "BUY":
                hb_order_price = round(hb_price * 1.004, 2)
            else:
                hb_order_price = round(hb_price * 0.996, 2)

            # RC-3: 주문 응답 검증 — 거부 시 즉시 재시도
            try:
                sx_resp = await self.standx.place_limit_order(
                    Config.PAIR_STANDX, standx_side, sx_order_price, round(sx_quantity, 3)
                )
                hb_resp = await self.hibachi.place_limit_order(
                    Config.PAIR_HIBACHI, hibachi_side, hb_order_price, round(hb_quantity, 6)
                )
                # 예외 없이 통과 = 주문 제출 성공 (응답이 빈 dict일 수 있음 — 정상)
            except Exception as e:
                logger.error("주문 제출 실패 (시도 %d/%d): %s", attempt + 1, ENTRY_MAX_RETRIES, e)
                await self.telegram.send_alert(f"⚠️ 주문 제출 실패 (시도 {attempt + 1}/{ENTRY_MAX_RETRIES}): {e}")
                await asyncio.sleep(5)
                continue

            await asyncio.sleep(30)

            standx_positions = await self.standx.get_positions()
            hibachi_positions = await self.hibachi.get_positions()
            has_standx = any(p.get("symbol") == Config.PAIR_STANDX for p in standx_positions)
            has_hibachi = any(p.get("symbol") == Config.PAIR_HIBACHI for p in hibachi_positions)

            if has_standx and has_hibachi:
                margin = notional / Config.LEVERAGE
                self._positions["standx"] = Position(
                    exchange="standx", symbol=Config.PAIR_STANDX,
                    side="LONG" if standx_side == "BUY" else "SHORT",
                    notional=notional, entry_price=sx_price,
                    leverage=Config.LEVERAGE, margin=margin,
                )
                self._positions["hibachi"] = Position(
                    exchange="hibachi", symbol=Config.PAIR_HIBACHI,
                    side="LONG" if hibachi_side == "BUY" else "SHORT",
                    notional=notional, entry_price=hb_price,
                    leverage=Config.LEVERAGE, margin=margin,
                )
                return True

            # NEW-3+RI-7: 미체결 주문 취소 (유령 주문 방지)
            if not has_standx:
                try:
                    await self.standx.cancel_all_orders(Config.PAIR_STANDX)
                except Exception:
                    pass
            if not has_hibachi:
                try:
                    await self.hibachi.cancel_all_orders()
                except Exception:
                    pass

            # C1: 편측 체결 — 공격적 슬리피지로 해제 + 검증
            if has_standx and not has_hibachi:
                logger.warning("편측 체결: StandX만 (시도 %d/%d)", attempt + 1, ENTRY_MAX_RETRIES)
                await self.standx.close_position(
                    Config.PAIR_STANDX, standx_side, round(sx_quantity, 3), slippage_pct=0.01
                )
                await asyncio.sleep(5)
                # 해제 검증
                remaining = await self.standx.get_positions()
                still_open = any(p.get("symbol") == Config.PAIR_STANDX for p in remaining)
                if still_open:
                    await self.telegram.send_alert(
                        "🚨 편측 해제 실패! StandX 포지션이 여전히 열려있음. 수동 확인 필요!"
                    )
                    return False
                await self.telegram.send_alert(f"⚠️ 편측 체결 해제 완료 (시도 {attempt + 1})")

            elif has_hibachi and not has_standx:
                logger.warning("편측 체결: Hibachi만 (시도 %d/%d)", attempt + 1, ENTRY_MAX_RETRIES)
                await self.hibachi.close_position(
                    Config.PAIR_HIBACHI, hibachi_side, round(hb_quantity, 6), slippage_pct=0.01
                )
                await asyncio.sleep(5)
                remaining = await self.hibachi.get_positions()
                still_open = any(p.get("symbol") == Config.PAIR_HIBACHI for p in remaining)
                if still_open:
                    await self.telegram.send_alert(
                        "🚨 편측 해제 실패! Hibachi 포지션이 여전히 열려있음. 수동 확인 필요!"
                    )
                    return False
                await self.telegram.send_alert(f"⚠️ 편측 체결 해제 완료 (시도 {attempt + 1})")

            await asyncio.sleep(5)

        await self.telegram.send_alert(f"🚨 진입 {ENTRY_MAX_RETRIES}회 실패, IDLE 복귀")
        return False

    async def _execute_exit(self) -> bool:
        """양쪽 동시 청산 — RC-5: 실제 포지션 수량 + NEW-4: 청산 검증"""
        failed = []
        succeeded = []

        # RC-5: 실제 포지션 크기를 거래소에서 조회
        actual_sizes = {}
        try:
            sx_positions = await self.standx.get_positions()
            for p in sx_positions:
                if p.get("symbol") == Config.PAIR_STANDX:
                    actual_sizes["standx"] = abs(float(p.get("qty", p.get("position_size", p.get("size", 0)))))
            hb_positions = await self.hibachi.get_positions()
            for p in hb_positions:
                if p.get("symbol") == Config.PAIR_HIBACHI:
                    actual_sizes["hibachi"] = abs(float(p.get("quantity", p.get("size", p.get("position_size", 0)))))
        except Exception as e:
            logger.warning("청산 전 포지션 조회 실패, 저장값 사용: %s", e)

        for key, pos in list(self._positions.items()):
            try:
                # RC-5: 실제 수량 우선, 없으면 계산값 사용
                quantity = actual_sizes.get(key, pos.notional / pos.entry_price)
                if pos.exchange == "standx":
                    side = "BUY" if pos.side == "LONG" else "SELL"
                    await self.standx.close_position(
                        Config.PAIR_STANDX, side, round(quantity, 3), slippage_pct=0.005
                    )
                else:
                    side = "BUY" if pos.side == "LONG" else "SELL"
                    await self.hibachi.close_position(
                        Config.PAIR_HIBACHI, side, round(quantity, 6), slippage_pct=0.005
                    )
                succeeded.append(key)
            except Exception as e:
                logger.error("%s 청산 실패: %s", key, e)
                await self.telegram.send_alert(f"🚨 {key} 청산 실패: {e}\n수동 확인 필요!")
                failed.append(key)

        # NEW-4: 청산 검증 — 실제로 닫혔는지 확인
        if succeeded:
            await asyncio.sleep(5)
            sx_positions = await self.standx.get_positions()
            hb_positions = await self.hibachi.get_positions()
            sx_still = any(p.get("symbol") == Config.PAIR_STANDX for p in sx_positions)
            hb_still = any(p.get("symbol") == Config.PAIR_HIBACHI for p in hb_positions)

            if "standx" in succeeded and sx_still:
                logger.error("StandX 청산 주문 제출됐으나 포지션 여전히 존재")
                succeeded.remove("standx")
                failed.append("standx")
            if "hibachi" in succeeded and hb_still:
                logger.error("Hibachi 청산 주문 제출됐으나 포지션 여전히 존재")
                succeeded.remove("hibachi")
                failed.append("hibachi")

        # C2: 확인된 것만 제거
        for key in succeeded:
            del self._positions[key]

        if failed:
            logger.error("부분 청산 실패: %s — 재시도 대기", failed)
            return False
        return True

    def _log_cycle(self, cycle: Cycle):
        path = os.path.join(Config.LOG_DIR, "cycles.jsonl")
        with open(path, "a") as f:
            f.write(cycle.to_jsonl() + "\n")

    def _check_weekly_reset(self):
        """M5: 매주 월요일 00:00 UTC에 주간 거래량 리셋"""
        now = time.time()
        if self.state.weekly_volume_reset_at == 0:
            self.state.weekly_volume_reset_at = now
            return
        elapsed = now - self.state.weekly_volume_reset_at
        if elapsed > 7 * 24 * 3600:
            self.state.weekly_hibachi_volume = 0.0
            self.state.weekly_volume_reset_at = now
            logger.info("주간 거래량 리셋")

    async def _send_daily_report(self):
        """S4: 일일 리포트 (09:00 KST)"""
        now_kst = datetime.now(KST)
        if now_kst.hour != 9 or now_kst.minute > 5:
            return
        if time.time() - self._last_daily_report < 3600:
            return
        self._last_daily_report = time.time()

        sip2 = estimate_sip2_yield(
            self.state.standx_balance, self._initial_standx_balance,
            self.state.cumulative_funding, self.state.cumulative_fees, 0.0
        )
        text = (
            f"📊 <b>일일 리포트</b> ({now_kst.strftime('%Y-%m-%d')})\n"
            f"상태: {self.state.cycle_state}\n"
            f"StandX: ${self.state.standx_balance:,.2f}\n"
            f"Hibachi: ${self.state.hibachi_balance:,.2f}\n"
            f"누적 펀딩수익: ${self.state.cumulative_funding:,.2f}\n"
            f"SIP-2 추정: ${sip2:,.2f}\n"
            f"주간 거래량: ${self.state.weekly_hibachi_volume:,.0f} / $100,000"
        )
        await self.telegram.send_alert(text)

    async def _check_dusd_depeg(self):
        """S2: DUSD 디페그 > 1% 시 경고 (5분 쿨다운)"""
        if time.time() - self._last_warning_time < 300:
            return
        try:
            sx_market = await self.standx.get_market_price(Config.PAIR_STANDX)
            index_price = float(sx_market.get("index_price", 0))
            mark_price = float(sx_market.get("mark_price", 0))
            if index_price > 0 and mark_price > 0:
                depeg = abs(mark_price - index_price) / index_price
                if depeg > 0.01:
                    self._last_warning_time = time.time()
                    await self.telegram.send_alert(
                        f"⚠️ DUSD 디페그 경고: {depeg:.2%}\n"
                        f"Mark: ${mark_price:,.2f} / Index: ${index_price:,.2f}"
                    )
        except Exception:
            pass

    async def _run_state_machine(self):
        # C7: Stop 레이스 방어
        if not self._running:
            return

        state = self.state.cycle_state

        if state == CycleState.IDLE:
            if self._cooldown_until and time.time() < self._cooldown_until:
                return
            self._cooldown_until = None
            self.state.cooldown_until = 0.0
            self.state.cycle_state = CycleState.ANALYZE
            self._save_state()

        elif state == CycleState.ANALYZE:
            try:
                sx_data = await self.standx.get_funding_rate(Config.PAIR_STANDX)
                sx_rate = float(sx_data.get("funding_rate", 0))
                hb_rate = await self.hibachi.get_funding_rate(Config.PAIR_HIBACHI)

                sx_8h = normalize_funding_to_8h(sx_rate, period_hours=1)
                hb_8h = normalize_funding_to_8h(hb_rate, period_hours=8)

                direction = decide_direction(sx_8h, hb_8h)

                sx_bal = await self.standx.get_balance()
                hb_bal = await self.hibachi.get_balance()
                sx_available = self._parse_sx_balance(sx_bal)
                hb_available = self._parse_hb_balance(hb_bal)

                self.state.standx_balance = sx_available
                self.state.hibachi_balance = hb_available
                self._initial_standx_balance = sx_available
                self._initial_hibachi_balance = hb_available
                self.state.initial_standx_balance = sx_available
                self.state.initial_hibachi_balance = hb_available

                notional = calc_notional(sx_available, hb_available, Config.LEVERAGE)

                logger.info("분석 완료: 방향=%s, 노셔널=$%.2f", direction, notional)

                # C7: 진입 전 _running 재확인
                if not self._running:
                    self.state.cycle_state = CycleState.IDLE
                    self._save_state()
                    return

                self.state.current_cycle_id += 1
                self.state.current_direction = direction
                self.state.cycle_state = CycleState.ENTER
                self._save_state()

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
                    self.state.weekly_hibachi_volume += notional
                    fee = notional * 0.0001
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

            margin_level = await self._check_margin_safety()
            if margin_level == MarginLevel.EMERGENCY:
                logger.warning("🚨 긴급 청산!")
                await self.telegram.send_alert("🚨 마진율 위험! 양쪽 긴급 청산 실행!")
                success = await self._execute_exit()
                if success:
                    self.state.cycle_state = CycleState.COOLDOWN
                    self._cooldown_until = time.time() + Config.COOLDOWN_HOURS * 3600
                else:
                    await self.telegram.send_alert("🚨 긴급 청산 부분 실패! 수동 확인 필요!")
                self._save_state()
                return

            if margin_level == MarginLevel.WARNING:
                if time.time() - self._last_warning_time > 1800:  # 30분 쿨다운
                    self._last_warning_time = time.time()
                    worst = self._get_worst_margin()
                    await self.telegram.send_alert(f"⚠️ 마진율 경고: {worst:.1f}%")

            worst_margin = self._get_worst_margin()
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
            success = await self._execute_exit()

            if not success:
                # C2: 부분 실패 시 EXIT 유지, 다음 틱에 재시도
                self._save_state()
                return

            if self._current_cycle:
                self._current_cycle.exited_at = time.time()
                sx_bal = await self.standx.get_balance()
                hb_bal = await self.hibachi.get_balance()
                self._current_cycle.standx_balance_after = self._parse_sx_balance(sx_bal)
                self._current_cycle.hibachi_balance_after = self._parse_hb_balance(hb_bal)
                self.state.standx_balance = self._current_cycle.standx_balance_after
                self.state.hibachi_balance = self._current_cycle.hibachi_balance_after
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

            self._cycle_entered_at = None
            self.state.cycle_entered_at = 0.0
            self.state.current_direction = ""
            self.state.cycle_state = CycleState.COOLDOWN
            self._cooldown_until = time.time() + Config.COOLDOWN_HOURS * 3600
            self._save_state()

        elif state == CycleState.COOLDOWN:
            if self._cooldown_until and time.time() >= self._cooldown_until:
                self._cooldown_until = None
                self.state.cooldown_until = 0.0
                self.state.cycle_state = CycleState.IDLE
                self._save_state()
                logger.info("쿨다운 종료, IDLE 복귀")

    async def _recovery_check(self):
        """봇 재시작 시 기존 포지션 복구 — C6: Position 객체 재구성"""
        sx_positions = await self.standx.get_positions()
        hb_positions = await self.hibachi.get_positions()

        sx_pos = next((p for p in sx_positions if p.get("symbol") == Config.PAIR_STANDX), None)
        hb_pos = next((p for p in hb_positions if p.get("symbol") == Config.PAIR_HIBACHI), None)

        if sx_pos and hb_pos:
            logger.info("양쪽 포지션 감지 → HOLD 복구")

            # C6: Position 객체 재구성
            # StandX: qty 양수=LONG, 음수=SHORT
            sx_qty_raw = float(sx_pos.get("qty", sx_pos.get("position_size", sx_pos.get("size", 0))))
            sx_size = abs(sx_qty_raw)
            sx_entry = float(sx_pos.get("entry_price", sx_pos.get("avg_price", 0)))
            sx_side = "LONG" if sx_qty_raw > 0 else "SHORT"
            sx_notional = sx_size * sx_entry if sx_entry > 0 else 0

            # Hibachi SDK 필드: quantity, openPrice, direction(Long/Short)
            hb_size = abs(float(hb_pos.get("quantity", hb_pos.get("size", hb_pos.get("position_size", 0)))))
            hb_entry = float(hb_pos.get("openPrice", hb_pos.get("entry_price", hb_pos.get("avg_price", 0))))
            hb_side_raw = hb_pos.get("direction", hb_pos.get("side", "")).upper()
            hb_side = "LONG" if hb_side_raw in ("LONG", "BUY") else "SHORT"
            hb_notional = hb_size * hb_entry if hb_entry > 0 else 0

            notional = max(sx_notional, hb_notional) or 30000.0

            self._positions["standx"] = Position(
                exchange="standx", symbol=Config.PAIR_STANDX,
                side=sx_side, notional=notional,
                entry_price=sx_entry or 1800.0,
                leverage=Config.LEVERAGE,
                margin=notional / Config.LEVERAGE,
            )
            self._positions["hibachi"] = Position(
                exchange="hibachi", symbol=Config.PAIR_HIBACHI,
                side=hb_side, notional=notional,
                entry_price=hb_entry or 1800.0,
                leverage=Config.LEVERAGE,
                margin=notional / Config.LEVERAGE,
            )

            self.state.cycle_state = CycleState.HOLD
            # I6: 저장된 진입시각 복구 (없으면 현재시각)
            if self.state.cycle_entered_at > 0:
                self._cycle_entered_at = self.state.cycle_entered_at
            else:
                self._cycle_entered_at = time.time()

            # _current_cycle 재구성 (저장된 정보로)
            direction_recovered = self.state.current_direction or "standx_short_hibachi_long"
            self._current_cycle = Cycle(
                cycle_id=self.state.current_cycle_id or 1,
                direction=direction_recovered,
                notional=notional,
                entered_at=self._cycle_entered_at,
            )

            await self.telegram.send_alert(
                f"🔁 봇 재시작: 포지션 복구 완료\n"
                f"StandX: {sx_side} ${notional:,.0f}\n"
                f"Hibachi: {hb_side} ${notional:,.0f}\n"
                f"방향: {self.state.current_direction or '미확인'}"
            )

        elif sx_pos or hb_pos:
            side = "StandX" if sx_pos else "Hibachi"
            logger.warning("편측 포지션 감지: %s", side)
            await self.telegram.send_alert(
                f"⚠️ 봇 재시작: {side}에만 포지션 존재! 수동 확인 필요"
            )
        else:
            # NEW-5: ENTER/ANALYZE 상태에서 크래시 후 포지션 없음 → IDLE 복귀
            if self.state.cycle_state not in (CycleState.IDLE, CycleState.COOLDOWN):
                logger.info("포지션 없음 + 비정상 상태(%s) → IDLE 복귀", self.state.cycle_state)
                self.state.cycle_state = CycleState.IDLE

        self._save_state()

    def _register_telegram_callbacks(self):
        async def on_status(cb):
            text = (
                f"📊 <b>Status</b>\n"
                f"상태: {self.state.cycle_state}\n"
                f"StandX: ${self.state.standx_balance:,.2f} (ETH ${self.standx_price:,.2f})\n"
                f"Hibachi: ${self.state.hibachi_balance:,.2f} (ETH ${self.hibachi_price:,.2f})\n"
                f"마진율: {self._get_worst_margin():.1f}%\n"
                f"주간 거래량: ${self.state.weekly_hibachi_volume:,.0f} / $100,000"
            )
            if self._cycle_entered_at:
                hold_h = (time.time() - self._cycle_entered_at) / 3600
                text += f"\n보유: {hold_h:.1f}시간"
            await self.telegram.send_main_menu(text)

        async def on_history(cb):
            path = os.path.join(Config.LOG_DIR, "cycles.jsonl")
            if not os.path.exists(path):
                await self.telegram.send_alert("📋 아직 완료된 사이클 없음")
                return
            # I8: 리소스 누수 수정
            with open(path) as f:
                lines = f.readlines()[-5:]
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
                await self.telegram.send_alert(
                    f"현재 상태({self.state.cycle_state})에서는 리밸런싱 불가"
                )

        async def on_stop(cb):
            await self.telegram.send_alert("⏹ 봇 종료 중... 포지션 청산")
            self._running = False  # C7: 먼저 플래그 설정
            if self._positions:
                success = await self._execute_exit()
                if not success:
                    await self.telegram.send_alert("🚨 Stop 청산 부분 실패! 수동 확인 필요!")
            self._save_state()
            # Watchdog 영구 정지 (재시작 방지)
            stop_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".stop_bot")
            with open(stop_file, "w") as f:
                f.write(str(time.time()))

        from telegram_ui import BTN_STATUS, BTN_HISTORY, BTN_FUNDING, BTN_REBALANCE, BTN_STOP
        self.telegram.register_callback(BTN_STATUS, on_status)
        self.telegram.register_callback(BTN_HISTORY, on_history)
        self.telegram.register_callback(BTN_FUNDING, on_funding)
        self.telegram.register_callback(BTN_REBALANCE, on_rebalance)
        self.telegram.register_callback(BTN_STOP, on_stop)

    async def run(self):
        self._running = True
        self._register_telegram_callbacks()

        await self.telegram.send_main_menu(
            f"🚀 Delta Neutral Bot 시작\n상태: {self.state.cycle_state}"
        )

        # RM-3: 시작 시 레버리지 설정
        try:
            await self.standx.change_leverage(Config.PAIR_STANDX, Config.LEVERAGE)
            logger.info("StandX 레버리지 %dx 설정 완료", Config.LEVERAGE)
        except Exception as e:
            logger.warning("StandX 레버리지 설정 실패 (수동 확인 필요): %s", e)

        await self._recovery_check()

        # WS 비활성화 — 양쪽 SDK WS 모두 불안정 (ping timeout, SDK 내부 에러)
        # REST 폴링으로 충분 (5초 간격)
        ws_tasks = []

        last_balance_check = 0
        last_funding_check = 0
        last_price_check = 0

        try:
            while self._running:
                now = time.time()

                await self.telegram.poll_updates()

                # C7: 폴링 후 다시 확인
                if not self._running:
                    break

                # REST 가격 폴링 (5초마다)
                if now - last_price_check > 5:
                    last_price_check = now
                    try:
                        sx_market = await self.standx.get_market_price(Config.PAIR_STANDX)
                        self.standx_price = float(sx_market.get("mark_price", 0))
                        self.hibachi_price = await self.hibachi.get_mark_price(Config.PAIR_HIBACHI)
                    except Exception:
                        pass

                await self._run_state_machine()

                # C4: 중복 마진 체크 제거 — _run_state_machine 내 HOLD에서 처리

                # M5: 주간 거래량 리셋 체크
                self._check_weekly_reset()

                # S4: 일일 리포트
                await self._send_daily_report()

                # S2: DUSD 디페그 감시
                if self.state.cycle_state == CycleState.HOLD:
                    await self._check_dusd_depeg()

                # 잔액 체크 (5분) + S3: 연속 실패 시 긴급 청산
                if now - last_balance_check > Config.POLL_BALANCE_SECONDS:
                    last_balance_check = now
                    try:
                        sx_bal = await self.standx.get_balance()
                        hb_bal = await self.hibachi.get_balance()
                        self.state.standx_balance = self._parse_sx_balance(sx_bal)
                        self.state.hibachi_balance = self._parse_hb_balance(hb_bal)
                        self._consecutive_api_failures = 0
                        self._save_state()
                    except Exception as e:
                        self._consecutive_api_failures += 1
                        logger.error("잔액 조회 실패 (%d연속): %s", self._consecutive_api_failures, e)
                        if (self._consecutive_api_failures >= API_FAIL_EMERGENCY_THRESHOLD
                                and self.state.cycle_state == CycleState.HOLD
                                and self._positions):
                            await self.telegram.send_alert(
                                f"🚨 API {self._consecutive_api_failures}회 연속 실패! "
                                f"포지션 감시 불가 — 긴급 청산 실행!"
                            )
                            exit_ok = await self._execute_exit()
                            # RC-4: 긴급 청산 후 상태 정리
                            self._current_cycle = None
                            self._cycle_entered_at = None
                            self.state.cycle_entered_at = 0.0
                            self.state.current_direction = ""
                            self._cumulative_funding_cost = 0.0
                            if exit_ok:
                                self.state.cycle_state = CycleState.COOLDOWN
                                self._cooldown_until = now + Config.COOLDOWN_HOURS * 3600
                            else:
                                # 부분 실패 시 EXIT 상태로 재시도 유도
                                self.state.cycle_state = CycleState.EXIT
                                await self.telegram.send_alert("🚨 긴급 청산 부분 실패! EXIT 상태에서 재시도합니다")
                            self._save_state()

                # 펀딩레이트 체크 (1시간) + I1: 누적 비용 갱신
                if now - last_funding_check > Config.POLL_FUNDING_SECONDS:
                    last_funding_check = now
                    try:
                        sx_data = await self.standx.get_funding_rate(Config.PAIR_STANDX)
                        sx_rate = float(sx_data.get("funding_rate", 0))
                        hb_rate = await self.hibachi.get_funding_rate(Config.PAIR_HIBACHI)

                        # I1+NEW-2: HOLD 상태 펀딩비용 누적 (노셔널 대비 비율)
                        if self.state.cycle_state == CycleState.HOLD and self._positions:
                            direction = self.state.current_direction
                            sx_1h = sx_rate
                            hb_1h = hb_rate / 8  # 8H→1H 정규화

                            # 롱이 양수일 때 지불, 숏이 양수일 때 수취
                            if "standx_long" in direction:
                                # StandX Long: 양수면 비용 / Hibachi Short: 양수면 수익
                                net_per_hour = sx_1h - hb_1h
                            else:
                                net_per_hour = hb_1h - sx_1h

                            # 누적: 양수=비용, 음수=수익 (자연 누적, clamp 없음)
                            self._cumulative_funding_cost += net_per_hour

                            # 달러 기반 누적 (일일 리포트용): 음수 net_per_hour는 수익이므로 양수 누적
                            notional = self._current_cycle.notional if self._current_cycle else (
                                next(iter(self._positions.values())).notional if self._positions else 28500.0
                            )
                            self.state.cumulative_funding += -net_per_hour * notional

                            # S5: 반대 방향이 현저히 유리하면 전환 트리거
                            sx_8h = normalize_funding_to_8h(sx_rate, period_hours=1)
                            hb_8h = normalize_funding_to_8h(hb_rate, period_hours=8)
                            if is_opposite_direction_better(direction, sx_8h, hb_8h):
                                hold_hours = (time.time() - self._cycle_entered_at) / 3600 if self._cycle_entered_at else 0
                                if hold_hours >= Config.MIN_HOLD_HOURS:
                                    logger.info("S5: 반대 방향 유리 → EXIT 트리거")
                                    await self.telegram.send_alert("🔄 반대 방향이 유리해짐 → 전환 준비")
                                    self.state.cycle_state = CycleState.EXIT
                                    self._save_state()

                        path = os.path.join(Config.LOG_DIR, "funding_history.jsonl")
                        with open(path, "a") as f:
                            f.write(json.dumps({"sx": sx_rate, "hb": hb_rate, "ts": now}) + "\n")
                    except Exception as e:
                        logger.error("펀딩레이트 조회 실패: %s", e)

                await asyncio.sleep(5)

        finally:
            await self.standx.close()
            await self.hibachi.close()
            await self.telegram.close()
