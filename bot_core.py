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
from strategy import normalize_funding_to_8h, decide_direction, should_exit_cycle, calc_notional, is_opposite_direction_better, should_exit_spread, should_exit_principal_recovered
from monitor import MarginLevel, check_margin_level, estimate_sip2_yield
from telegram_ui import TelegramUI

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
ENTRY_MAX_RETRIES = 3
ENTRY_CHUNKS = 5
EXIT_CHUNKS = 5
CHUNK_RETRY = 2
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
        self._last_hold_log: float = 0.0  # HOLD 상태 주기적 로깅 (30분)
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
        """양쪽 분할 진입 — XEMM 패턴: Hibachi maker → fill 확인 → StandX taker.

        부분 체결 안전 처리 (Bug C fix, 2026-04-27):
        - 각 maker 시도 직전에 hb 잔량 재조회 → 남은 양만 새로 발주 (중복 누적 방지)
        - cancel 후 실측 size로 부분 체결분 정확히 인식 (가중평균 가격 누적)
        - sx 헷지는 hb 실체결량(hb_actual_filled)에 정확히 매칭 — 청크 nominal qty 무시
        - hb 부족분은 taker로 보충 후 sx 매칭 → 청크 종료 시 양측 사이즈 항상 동기화
        """
        standx_side, hibachi_side = self._parse_direction(direction)
        chunk_notional = notional / ENTRY_CHUNKS
        filled_notional = 0.0
        avg_sx_price = 0.0
        avg_hb_price = 0.0

        await self.telegram.send_alert(
            f"📦 XEMM 분할 진입 시작: ${notional:,.0f} ÷ {ENTRY_CHUNKS} = ${chunk_notional:,.0f}/청크"
        )

        for chunk_idx in range(ENTRY_CHUNKS):
            # 가격 조회
            try:
                sx_market = await self.standx.get_market_price(Config.PAIR_STANDX)
                sx_mark = float(sx_market.get("mark_price", 0))
                hb_mark = await self.hibachi.get_mark_price(Config.PAIR_HIBACHI)
            except Exception as e:
                logger.error("가격 조회 실패 (청크 %d): %s", chunk_idx + 1, e)
                await asyncio.sleep(5)
                continue
            if sx_mark <= 0 or hb_mark <= 0:
                logger.error("가격 정보 없음 (sx=%.2f, hb=%.2f)", sx_mark, hb_mark)
                await asyncio.sleep(5)
                continue

            hb_target = round(chunk_notional / hb_mark, 6)
            hb_size_before = await self._get_hibachi_position_size()
            sx_size_before = await self._get_standx_position_size()

            # ── Step 1: Hibachi maker — 부분 체결 누적 인식, 시도마다 잔량 재계산 ──
            hb_value_sum = 0.0  # 가중평균용: Σ(price × qty)
            for maker_attempt in range(Config.MAKER_RETRY_LIMIT):
                # 매 시도마다 잔량 재계산 — 이전 시도 부분 체결분 차감
                cur_hb = await self._get_hibachi_position_size()
                already_in_chunk = cur_hb - hb_size_before
                hb_remaining = round(hb_target - already_in_chunk, 6)
                if hb_remaining <= 0.0001:  # 사실상 다 채워짐
                    logger.info(
                        "Hibachi maker 청크 %d 충분히 체결, 메이커 단계 종료 (%.6f / %.6f)",
                        chunk_idx + 1, already_in_chunk, hb_target,
                    )
                    break

                # 시도마다 3bp씩 양보 (안 cross 보장 + 책 안쪽 깊이 박힘)
                backoff = 0.0003 * (maker_attempt + 1)
                if hibachi_side == "BUY":
                    hb_price = round(hb_mark * (1 - backoff), 2)
                else:
                    hb_price = round(hb_mark * (1 + backoff), 2)

                try:
                    await self.hibachi.place_limit_order(
                        Config.PAIR_HIBACHI, hibachi_side, hb_price, hb_remaining, post_only=True,
                    )
                    logger.info(
                        "Hibachi maker 청크 %d 발주 (시도 %d, %s @ %.2f, 잔량 %.6f)",
                        chunk_idx + 1, maker_attempt + 1, hibachi_side, hb_price, hb_remaining,
                    )
                except Exception as e:
                    logger.warning("Hibachi maker 발주 실패: %s", e)
                    await asyncio.sleep(3)
                    continue

                # fill 대기 — 이번 시도분만 체크 (cur_hb 기준)
                deadline = time.time() + Config.MAKER_FILL_TIMEOUT_SECONDS
                full_attempt_fill = False
                while time.time() < deadline:
                    await asyncio.sleep(5)
                    tick = await self._get_hibachi_position_size()
                    if tick - cur_hb >= hb_remaining * 0.95:
                        full_attempt_fill = True
                        break

                # 미체결 주문 취소 — 이미 채워진 부분은 거래소에 그대로 남음
                try:
                    await self.hibachi.cancel_all_orders()
                except Exception:
                    pass

                # 취소 후 실측 → 이번 시도에서 실제 채워진 양 (부분 체결 포함)
                after_cancel = await self._get_hibachi_position_size()
                attempt_filled = after_cancel - cur_hb
                if attempt_filled > 0.0001:
                    hb_value_sum += hb_price * attempt_filled
                    logger.info(
                        "Hibachi maker 청크 %d 시도 %d 체결: +%.6f @ %.2f (full=%s)",
                        chunk_idx + 1, maker_attempt + 1, attempt_filled, hb_price, full_attempt_fill,
                    )

                if full_attempt_fill:
                    break

                # 다음 시도용 mark 갱신
                try:
                    hb_mark = await self.hibachi.get_mark_price(Config.PAIR_HIBACHI)
                except Exception:
                    pass
                logger.info(
                    "Hibachi maker 부족분 남음, 재시도 (청크 %d 시도 %d/%d)",
                    chunk_idx + 1, maker_attempt + 1, Config.MAKER_RETRY_LIMIT,
                )

            # ── Step 2: maker 단계 종료 — 실측 hb 체결량 ──
            after_maker = await self._get_hibachi_position_size()
            hb_maker_filled = after_maker - hb_size_before

            # ── Step 3: 부족분 taker 보충 ──
            if hb_maker_filled < hb_target * 0.95:
                hb_shortage = round(hb_target - hb_maker_filled, 6)
                logger.warning(
                    "청크 %d: maker 부족 (%.6f/%.6f) → taker 보충 %.6f",
                    chunk_idx + 1, hb_maker_filled, hb_target, hb_shortage,
                )
                await self.telegram.send_alert(
                    f"⚠️ 청크 {chunk_idx + 1} maker 부족 → taker 보충 {hb_shortage:.4f} ETH"
                )
                try:
                    hb_mark_now = await self.hibachi.get_mark_price(Config.PAIR_HIBACHI)
                    hb_taker_price = round(
                        hb_mark_now * (1.004 if hibachi_side == "BUY" else 0.996), 2
                    )
                    await self.hibachi.place_limit_order(
                        Config.PAIR_HIBACHI, hibachi_side, hb_taker_price, hb_shortage,
                    )
                    await asyncio.sleep(8)
                    try:
                        await self.hibachi.cancel_all_orders()
                    except Exception:
                        pass
                    after_taker = await self._get_hibachi_position_size()
                    taker_filled = after_taker - after_maker
                    if taker_filled > 0.0001:
                        hb_value_sum += hb_taker_price * taker_filled
                except Exception as e:
                    logger.error("Hibachi taker 보충 실패: %s", e)

            # ── Step 4: 최종 hb 실체결량 산정 — 이게 sx 헷지 타겟 ──
            final_hb = await self._get_hibachi_position_size()
            hb_actual = final_hb - hb_size_before

            if hb_actual <= 0.001:
                logger.error("청크 %d: Hibachi 진입 0 → 청크 스킵", chunk_idx + 1)
                await self.telegram.send_alert(
                    f"⚠️ 청크 {chunk_idx + 1} Hibachi 0 체결 → 스킵, 다음 청크 진행"
                )
                continue

            hb_chunk_avg = hb_value_sum / hb_actual if hb_actual > 0 else hb_mark

            # ── Step 5: StandX taker — hb 실체결량과 동일 ETH 만큼 매칭 ──
            sx_target = round(hb_actual, 3)
            if sx_target <= 0.001:
                logger.error(
                    "청크 %d: hb 체결량 라운딩 후 sx 타겟 0 (hb=%.6f) → hb 응급 청산",
                    chunk_idx + 1, hb_actual,
                )
                rollback_side = "SELL" if hibachi_side == "BUY" else "BUY"
                try:
                    await self.hibachi.close_position(
                        Config.PAIR_HIBACHI, rollback_side, hb_actual, slippage_pct=0.01,
                    )
                except Exception as e:
                    logger.error("응급 청산 실패: %s", e)
                continue

            sx_done = False
            sx_fill_price = sx_mark
            for sx_attempt in range(Config.MAKER_RETRY_LIMIT * 2):
                cur_sx = await self._get_standx_position_size()
                sx_remaining = round(sx_target - (cur_sx - sx_size_before), 3)
                if sx_remaining <= 0.001:
                    sx_done = True
                    break

                if standx_side == "BUY":
                    sx_order_price = round(sx_mark * 1.004 / 0.1) * 0.1
                else:
                    sx_order_price = round(sx_mark * 0.996 / 0.1) * 0.1
                sx_order_price = round(sx_order_price, 1)

                try:
                    await self.standx.place_limit_order(
                        Config.PAIR_STANDX, standx_side, sx_order_price, sx_remaining,
                    )
                except Exception as e:
                    logger.error("StandX taker 발주 실패 (시도 %d): %s", sx_attempt + 1, e)
                    await asyncio.sleep(5)
                    continue

                await asyncio.sleep(15)
                cur_sx2 = await self._get_standx_position_size()
                if cur_sx2 - sx_size_before >= sx_target * 0.98:
                    logger.info(
                        "StandX taker 청크 %d 매칭 완료: %.3f → %.3f (타겟 %.3f)",
                        chunk_idx + 1, sx_size_before, cur_sx2, sx_target,
                    )
                    sx_done = True
                    sx_fill_price = sx_mark
                    break

                try:
                    await self.standx.cancel_all_orders(Config.PAIR_STANDX)
                except Exception:
                    pass
                try:
                    sx_market = await self.standx.get_market_price(Config.PAIR_STANDX)
                    sx_mark = float(sx_market.get("mark_price", sx_mark))
                except Exception:
                    pass
                logger.warning(
                    "StandX taker 부족분 남음, 재시도 (청크 %d 시도 %d, 잔량 %.3f)",
                    chunk_idx + 1, sx_attempt + 1, sx_remaining,
                )

            if not sx_done:
                # 응급: hb 실체결량 만큼 응급 청산 (편측 노출 차단)
                logger.error(
                    "🚨 청크 %d StandX 매칭 실패 (hb %.6f) → Hibachi 응급 청산",
                    chunk_idx + 1, hb_actual,
                )
                await self.telegram.send_alert(
                    f"🚨 청크 {chunk_idx + 1}: StandX 매칭 실패 → Hibachi {hb_actual:.4f} ETH 응급 청산"
                )
                rollback_side = "SELL" if hibachi_side == "BUY" else "BUY"
                try:
                    await self.hibachi.close_position(
                        Config.PAIR_HIBACHI, rollback_side, hb_actual, slippage_pct=0.01,
                    )
                except Exception as e:
                    logger.error("Hibachi 응급 청산 실패: %s — 수동 개입 필요!", e)
                    await self.telegram.send_alert(
                        f"🚨🚨 Hibachi 응급 청산 실패! 수동 헷지 필요!"
                    )
                break  # 진입 시도 중단

            # 청크 성공 — 실체결량 기준 notional 누적 (양쪽 동일량 보장)
            chunk_filled = hb_actual * hb_chunk_avg
            avg_sx_price = (
                (avg_sx_price * filled_notional + sx_fill_price * chunk_filled)
                / (filled_notional + chunk_filled) if (filled_notional + chunk_filled) > 0 else sx_fill_price
            )
            avg_hb_price = (
                (avg_hb_price * filled_notional + hb_chunk_avg * chunk_filled)
                / (filled_notional + chunk_filled) if (filled_notional + chunk_filled) > 0 else hb_chunk_avg
            )
            filled_notional += chunk_filled
            logger.info(
                "XEMM 청크 %d/%d 체결 완료 (hb=sx=%.6f ETH, $%.0f, 누적 $%.0f/$%.0f)",
                chunk_idx + 1, ENTRY_CHUNKS, hb_actual, chunk_filled, filled_notional, notional,
            )

        if filled_notional <= 0:
            await self.telegram.send_alert(f"🚨 분할 진입 전부 실패, IDLE 복귀")
            return False

        margin = filled_notional / Config.LEVERAGE
        self._positions["standx"] = Position(
            exchange="standx", symbol=Config.PAIR_STANDX,
            side="LONG" if standx_side == "BUY" else "SHORT",
            notional=filled_notional, entry_price=avg_sx_price,
            leverage=Config.LEVERAGE, margin=margin,
        )
        self._positions["hibachi"] = Position(
            exchange="hibachi", symbol=Config.PAIR_HIBACHI,
            side="LONG" if hibachi_side == "BUY" else "SHORT",
            notional=filled_notional, entry_price=avg_hb_price,
            leverage=Config.LEVERAGE, margin=margin,
        )
        await self.telegram.send_alert(
            f"✅ 분할 진입 완료: ${filled_notional:,.0f}/${notional:,.0f} "
            f"({ENTRY_CHUNKS}청크 중 {int(filled_notional / chunk_notional)}개 체결)"
        )

        # T1 스냅샷: 진입 청크 완료 직후 잔고 — 실제 entry cost 측정용
        try:
            sx_bal_t1 = self._parse_sx_balance(await self.standx.get_balance())
            hb_bal_t1 = self._parse_hb_balance(await self.hibachi.get_balance())
            self.state.balance_after_entry_total = sx_bal_t1 + hb_bal_t1
            init_total = self._initial_standx_balance + self._initial_hibachi_balance
            actual_entry_cost = init_total - self.state.balance_after_entry_total
            logger.info(
                "T1 잔고 스냅샷: $%.2f (T0=$%.2f) → 실측 진입 비용 $%.2f",
                self.state.balance_after_entry_total, init_total, actual_entry_cost,
            )
            if self._current_cycle:
                self._current_cycle.balance_t0_total = init_total
                self._current_cycle.balance_t1_total = self.state.balance_after_entry_total
                self._current_cycle.actual_entry_cost = actual_entry_cost
        except Exception as e:
            logger.warning("T1 잔고 스냅샷 실패: %s", e)

        return True

    async def _get_hibachi_position_size(self) -> float:
        """현재 Hibachi 포지션 사이즈 조회. 실패 시 0 반환."""
        try:
            positions = await self.hibachi.get_positions()
            for p in positions:
                if p.get("symbol") == Config.PAIR_HIBACHI:
                    return abs(float(p.get("quantity", p.get("size", p.get("position_size", 0)))))
        except Exception as e:
            logger.warning("Hibachi 포지션 조회 실패: %s", e)
        return 0.0

    async def _get_standx_position_size(self) -> float:
        """현재 StandX 포지션 사이즈 조회. 실패 시 0 반환."""
        try:
            positions = await self.standx.get_positions()
            for p in positions:
                if p.get("symbol") == Config.PAIR_STANDX:
                    return abs(float(p.get("qty", p.get("position_size", p.get("size", 0)))))
        except Exception as e:
            logger.warning("StandX 포지션 조회 실패: %s", e)
        return 0.0

    async def _execute_exit(self) -> bool:
        """양쪽 분할 청산 — XEMM 패턴: Hibachi maker → fill 확인 → StandX taker.

        실패 시나리오 처리:
        - Hibachi maker 미체결 (timeout) → 취소, 가격 1bp씩 양보하며 retry
        - MAKER_RETRY_LIMIT 초과 → 양쪽 taker fallback (구방식)
        - Hibachi 체결 후 StandX 실패 → 응급 StandX taker 무한 retry (편측 노출 차단)
        """
        # ── T2 스냅샷 (기존 보존) ──
        try:
            sx_bal_t2 = self._parse_sx_balance(await self.standx.get_balance())
            hb_bal_t2 = self._parse_hb_balance(await self.hibachi.get_balance())
            self.state.balance_before_exit_total = sx_bal_t2 + hb_bal_t2
            t1_total = self.state.balance_after_entry_total
            actual_hold_change = self.state.balance_before_exit_total - t1_total if t1_total > 0 else 0
            logger.info(
                "T2 잔고 스냅샷: $%.2f (T1=$%.2f) → 실측 HOLD 변동 $%+.2f (펀딩 - 미실현)",
                self.state.balance_before_exit_total, t1_total, actual_hold_change,
            )
            if self._current_cycle:
                self._current_cycle.balance_t2_total = self.state.balance_before_exit_total
                self._current_cycle.actual_hold_change = actual_hold_change
        except Exception as e:
            logger.warning("T2 잔고 스냅샷 실패: %s", e)

        sx_pos = self._positions.get("standx")
        hb_pos = self._positions.get("hibachi")
        if not sx_pos or not hb_pos:
            logger.error("청산 대상 포지션 없음")
            return False

        # 청산 방향
        hb_close_side = "SELL" if hb_pos.side == "LONG" else "BUY"  # 직접 발주용
        sx_open_side = "BUY" if sx_pos.side == "LONG" else "SELL"   # close_position이 내부 flip

        # 시작 포지션 사이즈 — 청크마다 실시간 조회로 갱신
        sx_initial = await self._get_standx_position_size() or (sx_pos.notional / sx_pos.entry_price)
        hb_initial = await self._get_hibachi_position_size() or (hb_pos.notional / hb_pos.entry_price)

        await self.telegram.send_alert(
            f"📦 XEMM 분할 청산 시작: {EXIT_CHUNKS}청크\n"
            f"Hibachi {hb_initial:.6f} (maker) / StandX {sx_initial:.3f} (taker)"
        )

        closed_chunks = 0
        for chunk_idx in range(EXIT_CHUNKS):
            chunks_left = EXIT_CHUNKS - chunk_idx

            # 청크 시작 시 현재 잔량 재조회 — 이게 청산 진행 기준
            hb_at_start = await self._get_hibachi_position_size()
            sx_at_start = await self._get_standx_position_size()
            if hb_at_start <= 0.001 and sx_at_start <= 0.001:
                logger.info("청산 완료 (잔량 0) — 청크 %d 중단", chunk_idx + 1)
                break

            is_last = (chunks_left == 1)
            hb_target_close = (
                round(hb_at_start, 6) if is_last
                else round(hb_at_start / chunks_left, 6)
            )
            if hb_target_close <= 0:
                logger.warning("청크 %d hb 청산량 0, sx만 잔여 시 다음 청크", chunk_idx + 1)
                continue

            # ── Step 1: Hibachi maker — 부분 체결 누적 인식, 시도마다 잔량 재계산 ──
            hb_value_sum = 0.0  # 가중평균용
            for maker_attempt in range(Config.MAKER_RETRY_LIMIT):
                cur_hb = await self._get_hibachi_position_size()
                already_closed_in_chunk = hb_at_start - cur_hb
                hb_to_close = round(hb_target_close - already_closed_in_chunk, 6)
                if hb_to_close <= 0.0001:
                    logger.info(
                        "Hibachi maker 청산 청크 %d 충분히 닫힘 (%.6f / %.6f)",
                        chunk_idx + 1, already_closed_in_chunk, hb_target_close,
                    )
                    break

                try:
                    hb_mark = await self.hibachi.get_mark_price(Config.PAIR_HIBACHI)
                except Exception as e:
                    logger.warning("Hibachi mark 조회 실패: %s", e)
                    await asyncio.sleep(3)
                    continue
                if hb_mark <= 0:
                    await asyncio.sleep(3)
                    continue

                # 시도 횟수 증가에 따라 가격 양보 (3bp → 6bp → 9bp ... 시도 5에 -15bp까지)
                backoff = 0.0003 * (maker_attempt + 1)
                if hb_close_side == "BUY":
                    hb_price = round(hb_mark * (1 - backoff), 2)
                else:
                    hb_price = round(hb_mark * (1 + backoff), 2)

                try:
                    await self.hibachi.place_limit_order(
                        Config.PAIR_HIBACHI, hb_close_side, hb_price, hb_to_close, post_only=True,
                    )
                    logger.info(
                        "Hibachi maker 청산 청크 %d 발주 (시도 %d, %s @ %.2f, 잔량 %.6f)",
                        chunk_idx + 1, maker_attempt + 1, hb_close_side, hb_price, hb_to_close,
                    )
                except Exception as e:
                    logger.warning("Hibachi maker 발주 실패: %s", e)
                    await asyncio.sleep(3)
                    continue

                # fill 대기 — 이번 시도분만 체크 (cur_hb 기준 감소량)
                deadline = time.time() + Config.MAKER_FILL_TIMEOUT_SECONDS
                full_attempt_fill = False
                while time.time() < deadline:
                    await asyncio.sleep(5)
                    tick = await self._get_hibachi_position_size()
                    if cur_hb - tick >= hb_to_close * 0.95:
                        full_attempt_fill = True
                        break

                # 미체결분 cancel — 이미 체결된 부분은 거래소에 반영됨
                try:
                    await self.hibachi.cancel_all_orders()
                except Exception:
                    pass

                # 취소 후 실측 → 이번 시도에서 실제 닫힌 양 (부분 체결 포함)
                after_cancel = await self._get_hibachi_position_size()
                attempt_closed = cur_hb - after_cancel
                if attempt_closed > 0.0001:
                    hb_value_sum += hb_price * attempt_closed
                    logger.info(
                        "Hibachi maker 청산 청크 %d 시도 %d 닫힘: -%.6f @ %.2f (full=%s)",
                        chunk_idx + 1, maker_attempt + 1, attempt_closed, hb_price, full_attempt_fill,
                    )

                if full_attempt_fill:
                    break

                logger.info(
                    "Hibachi maker 부족분 남음, 재시도 (청크 %d 시도 %d/%d, backoff %.2fbp)",
                    chunk_idx + 1, maker_attempt + 1, Config.MAKER_RETRY_LIMIT, backoff * 10000,
                )

            # ── Step 2: maker 종료 → 부족분 taker 보충 ──
            after_maker = await self._get_hibachi_position_size()
            hb_maker_closed = hb_at_start - after_maker

            if hb_maker_closed < hb_target_close * 0.95:
                hb_shortage = round(hb_target_close - hb_maker_closed, 6)
                logger.warning(
                    "청산 청크 %d: maker 부족 (%.6f/%.6f) → taker 보충 %.6f",
                    chunk_idx + 1, hb_maker_closed, hb_target_close, hb_shortage,
                )
                await self.telegram.send_alert(
                    f"⚠️ 청산 청크 {chunk_idx + 1} maker 부족 → taker 보충 {hb_shortage:.4f} ETH"
                )
                try:
                    await self.hibachi.close_position(
                        Config.PAIR_HIBACHI,
                        "BUY" if hb_close_side == "SELL" else "SELL",  # close_position 내부 flip
                        hb_shortage, slippage_pct=0.005,
                    )
                    await asyncio.sleep(8)
                except Exception as e:
                    logger.error("Hibachi taker 보충 실패: %s", e)

            # ── Step 3: 최종 hb 실청산량 → sx 매칭 타겟 ──
            final_hb = await self._get_hibachi_position_size()
            hb_actual_closed = hb_at_start - final_hb

            if hb_actual_closed <= 0.001:
                logger.error("청산 청크 %d: Hibachi 0 청산", chunk_idx + 1)
                await self.telegram.send_alert(
                    f"⚠️ 청산 청크 {chunk_idx + 1} Hibachi 0 → 다음 청크 진행"
                )
                continue

            # ── Step 4: StandX taker — hb 실청산량과 동일 ETH 매칭 ──
            sx_target_close = round(hb_actual_closed, 3)
            if sx_target_close <= 0.001:
                logger.warning(
                    "청산 청크 %d: hb %.6f 청산 후 sx 타겟 라운딩 0", chunk_idx + 1, hb_actual_closed,
                )
                continue

            sx_done = False
            sx_max_attempts = Config.MAKER_RETRY_LIMIT * 2
            for sx_attempt in range(sx_max_attempts):
                cur_sx = await self._get_standx_position_size()
                sx_already_closed = sx_at_start - cur_sx
                sx_to_close = round(sx_target_close - sx_already_closed, 3)
                if sx_to_close <= 0.001:
                    sx_done = True
                    break

                # Slippage escalation — 시도마다 0.5% × (시도+1)로 증가, EMERGENCY_CLOSE_SLIPPAGE_PCT 상한.
                # 동일 가격대 무한 retry → bid-ask spread 1% 이상 시 영구 미체결 방지.
                sx_slippage = min(
                    0.005 * (sx_attempt + 1),
                    Config.EMERGENCY_CLOSE_SLIPPAGE_PCT,
                )
                try:
                    await self.standx.close_position(
                        Config.PAIR_STANDX, sx_open_side, sx_to_close, slippage_pct=sx_slippage,
                    )
                except Exception as e:
                    logger.error("StandX taker 발주 실패 (시도 %d): %s", sx_attempt + 1, e)
                    await asyncio.sleep(5)
                    continue

                await asyncio.sleep(15)
                cur_sx2 = await self._get_standx_position_size()
                if sx_at_start - cur_sx2 >= sx_target_close * 0.98:
                    logger.info(
                        "StandX taker 청산 청크 %d 매칭 완료: %.3f → %.3f (-%.3f, slippage %.2f%%)",
                        chunk_idx + 1, sx_at_start, cur_sx2, sx_target_close, sx_slippage * 100,
                    )
                    sx_done = True
                    break

                try:
                    await self.standx.cancel_all_orders(Config.PAIR_STANDX)
                except Exception:
                    pass
                logger.warning(
                    "StandX taker 부족분 남음, 재시도 (청산 청크 %d 시도 %d, 잔량 %.3f, "
                    "다음 slippage %.2f%%)",
                    chunk_idx + 1, sx_attempt + 1, sx_to_close,
                    min(0.005 * (sx_attempt + 2), Config.EMERGENCY_CLOSE_SLIPPAGE_PCT) * 100,
                )

            if not sx_done:
                # P1 응급 청산 — 일반 retry escalation도 실패한 시점.
                # 편측 노출은 1초마다 가격 리스크가 누적되므로, 최후 수단으로 EMERGENCY_CLOSE_SLIPPAGE_PCT
                # 전체를 한 번에 태워서 cross-spread를 강제로 통과시킨다.
                logger.error(
                    "청산 청크 %d: SX taker escalation %d회 실패 → 응급 시장가 시도 "
                    "(slippage %.2f%%)",
                    chunk_idx + 1, sx_max_attempts, Config.EMERGENCY_CLOSE_SLIPPAGE_PCT * 100,
                )
                await self.telegram.send_alert(
                    f"🚨 청크 {chunk_idx + 1} SX 응급 시장가 시도 "
                    f"(slippage {Config.EMERGENCY_CLOSE_SLIPPAGE_PCT * 100:.1f}%)"
                )
                try:
                    cur_sx_emerg = await self._get_standx_position_size()
                    sx_remaining = round(sx_target_close - (sx_at_start - cur_sx_emerg), 3)
                    if sx_remaining > 0.001:
                        await self.standx.close_position(
                            Config.PAIR_STANDX, sx_open_side, sx_remaining,
                            slippage_pct=Config.EMERGENCY_CLOSE_SLIPPAGE_PCT,
                        )
                        await asyncio.sleep(20)
                        cur_sx_after = await self._get_standx_position_size()
                        if sx_at_start - cur_sx_after >= sx_target_close * 0.98:
                            logger.info(
                                "응급 시장가 성공: 청크 %d SX %.3f → %.3f",
                                chunk_idx + 1, sx_at_start, cur_sx_after,
                            )
                            sx_done = True
                except Exception as e:
                    logger.error("응급 시장가 실패: %s", e)

            if not sx_done:
                logger.error(
                    "🚨 청산 청크 %d: hb %.6f 청산했는데 StandX 응급 시장가까지 실패! 편측 노출!",
                    chunk_idx + 1, hb_actual_closed,
                )
                await self.telegram.send_alert(
                    f"🚨🚨 청산 청크 {chunk_idx + 1} hb {hb_actual_closed:.4f} 청산 → "
                    f"StandX 응급 시장가({Config.EMERGENCY_CLOSE_SLIPPAGE_PCT * 100:.1f}%)도 실패! "
                    f"편측 노출. 수동 개입 필요."
                )
                break

            closed_chunks += 1
            logger.info(
                "XEMM 청산 청크 %d/%d 완료 (hb=sx=%.6f ETH 청산)",
                chunk_idx + 1, EXIT_CHUNKS, hb_actual_closed,
            )

        # ── 최종 검증 ──
        sx_still_size = await self._get_standx_position_size()
        hb_still_size = await self._get_hibachi_position_size()
        sx_still = sx_still_size > 1e-9
        hb_still = hb_still_size > 1e-9

        if not sx_still:
            self._positions.pop("standx", None)
        if not hb_still:
            self._positions.pop("hibachi", None)

        if sx_still or hb_still:
            logger.error(
                "XEMM 청산 후 잔여 포지션: sx=%.3f hb=%.6f", sx_still_size, hb_still_size,
            )
            return False

        await self.telegram.send_alert(f"✅ XEMM 분할 청산 완료 ({closed_chunks}청크)")
        return True

    def _log_cycle(self, cycle: Cycle):
        path = os.path.join(Config.LOG_DIR, "cycles.jsonl")
        with open(path, "a") as f:
            f.write(cycle.to_jsonl() + "\n")

    def _log_spread_snapshot(self, hold_hours: float):
        """HOLD 틱마다 스프레드/델타순합 기록 — 기회적 청산 threshold 분포 분석용."""
        sx_pos = self._positions.get("standx")
        hb_pos = self._positions.get("hibachi")
        if not sx_pos or not hb_pos:
            return
        if self.standx_price <= 0 or self.hibachi_price <= 0:
            return

        sx_pnl = sx_pos.calc_unrealized_pnl(self.standx_price)
        hb_pnl = hb_pos.calc_unrealized_pnl(self.hibachi_price)
        delta_sum = sx_pnl + hb_pnl

        # 두 거래소 간 가격 스프레드 (LONG exchange − SHORT exchange 방향 고정)
        if sx_pos.side == "LONG":
            long_entry, short_entry = sx_pos.entry_price, hb_pos.entry_price
            long_now, short_now = self.standx_price, self.hibachi_price
        else:
            long_entry, short_entry = hb_pos.entry_price, sx_pos.entry_price
            long_now, short_now = self.hibachi_price, self.standx_price

        spread_entry = long_entry - short_entry
        spread_now = long_now - short_now

        record = {
            "ts": time.time(),
            "cycle_id": self.state.current_cycle_id,
            "hold_h": round(hold_hours, 4),
            "sx_px": self.standx_price,
            "hb_px": self.hibachi_price,
            "sx_entry": sx_pos.entry_price,
            "hb_entry": hb_pos.entry_price,
            "sx_pnl": round(sx_pnl, 4),
            "hb_pnl": round(hb_pnl, 4),
            "delta_sum": round(delta_sum, 4),
            "spread_now": round(spread_now, 4),
            "spread_entry": round(spread_entry, 4),
            "spread_delta": round(spread_now - spread_entry, 4),
            "funding_cost": round(self._cumulative_funding_cost, 8),
        }
        path = os.path.join(Config.LOG_DIR, "spread_history.jsonl")
        try:
            with open(path, "a") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning("spread 로깅 실패: %s", e)

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
            # 새 사이클 시작 — 직전 사이클의 failure 카운터 잔재 정리.
            self.state.exit_failure_count = 0
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
                if direction is None:
                    logger.info("양쪽 펀딩 모두 음수 → IDLE 유지, 다음 폴링에서 재분석")
                    self.state.cycle_state = CycleState.IDLE
                    self._save_state()
                    return

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

                logger.info("잔액: StandX=$%.2f, Hibachi=$%.2f → min=$%.2f", sx_available, hb_available, min(sx_available, hb_available))
                logger.info("분석 완료: 방향=%s, 노셔널=$%.2f (min×%dx×0.95)", direction, notional, Config.LEVERAGE)

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
                    self._last_hold_log = 0.0
                    sx_entry = self._positions["standx"].entry_price
                    hb_entry = self._positions["hibachi"].entry_price
                    if "standx_long" in direction:
                        entry_spread = sx_entry - hb_entry
                    else:
                        entry_spread = hb_entry - sx_entry
                    self._current_cycle = Cycle(
                        cycle_id=self.state.current_cycle_id,
                        direction=direction,
                        notional=notional,
                        entry_sx_price=sx_entry,
                        entry_hb_price=hb_entry,
                        entry_spread=entry_spread,
                    )
                    # 백필: _execute_enter 안에서 _current_cycle=None이라 못 박은 T0/T1 (Bug B fix)
                    init_total_for_cycle = self._initial_standx_balance + self._initial_hibachi_balance
                    t1_total_for_cycle = self.state.balance_after_entry_total
                    if init_total_for_cycle > 0 and t1_total_for_cycle > 0:
                        self._current_cycle.balance_t0_total = init_total_for_cycle
                        self._current_cycle.balance_t1_total = t1_total_for_cycle
                        self._current_cycle.actual_entry_cost = init_total_for_cycle - t1_total_for_cycle
                    self.state.cycle_state = CycleState.HOLD
                    self.state.weekly_hibachi_volume += notional
                    fee = notional * Config.FEE_PER_FILL
                    self.state.cumulative_fees += fee
                    await self.telegram.send_alert(
                        f"✅ 사이클 #{self.state.current_cycle_id} 진입\n"
                        f"방향: {direction}\n"
                        f"노셔널: ${notional:,.0f}\n"
                        f"진입 스프레드: ${entry_spread:.2f}\n"
                        f"StandX: ${sx_available:,.2f} @ ${sx_entry:,.2f}\n"
                        f"Hibachi: ${hb_available:,.2f} @ ${hb_entry:,.2f}"
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
                    if self._current_cycle:
                        self._current_cycle.exit_reason = "margin_emergency"
                        self._current_cycle.exited_at = time.time()
                        self._current_cycle.exit_sx_price = self.standx_price
                        self._current_cycle.exit_hb_price = self.hibachi_price
                        try:
                            sx_bal = await self.standx.get_balance()
                            hb_bal = await self.hibachi.get_balance()
                            self._current_cycle.standx_balance_after = self._parse_sx_balance(sx_bal)
                            self._current_cycle.hibachi_balance_after = self._parse_hb_balance(hb_bal)
                            self._current_cycle.balance_t3_total = (
                                self._current_cycle.standx_balance_after
                                + self._current_cycle.hibachi_balance_after
                            )
                            self.state.standx_balance = self._current_cycle.standx_balance_after
                            self.state.hibachi_balance = self._current_cycle.hibachi_balance_after
                        except Exception as e:
                            logger.warning("긴급 청산 후 잔고 조회 실패: %s", e)
                        self._log_cycle(self._current_cycle)
                        self._current_cycle = None
                    self._cycle_entered_at = None
                    self.state.cycle_entered_at = 0.0
                    self.state.current_direction = ""
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

            # 스프레드 분포 기록 (매 틱, spread_history.jsonl)
            self._log_spread_snapshot(hold_hours)

            # HOLD 상태 주기 로깅 (30분마다)
            now = time.time()
            if now - self._last_hold_log > 1800:
                self._last_hold_log = now
                logger.info(
                    "HOLD 상태: 보유=%.1fh, 마진=%.1f%%, 펀딩비용=%.6f, "
                    "SX=$%.2f HB=$%.2f, 레벨=%s",
                    hold_hours, worst_margin, self._cumulative_funding_cost,
                    self.standx_price, self.hibachi_price, margin_level.name,
                )

            # 기회적 청산: 스프레드 MTM이 threshold 이상이면 즉시 EXIT (MIN_HOLD 무관)
            sx_pos = self._positions.get("standx")
            hb_pos = self._positions.get("hibachi")
            if sx_pos and hb_pos and self.standx_price > 0 and self.hibachi_price > 0:
                delta_sum = (sx_pos.calc_unrealized_pnl(self.standx_price)
                             + hb_pos.calc_unrealized_pnl(self.hibachi_price))
                if should_exit_spread(delta_sum, Config.SPREAD_EXIT_THRESHOLD):
                    logger.info(
                        "기회적 청산 EXIT: delta_sum=$%.2f >= $%.0f (보유 %.1fh)",
                        delta_sum, Config.SPREAD_EXIT_THRESHOLD, hold_hours,
                    )
                    if self._current_cycle:
                        self._current_cycle.exit_reason = "spread_opportunity"
                    await self.telegram.send_alert(
                        f"💰 기회적 청산 트리거! spread MTM ${delta_sum:+,.1f}\n"
                        f"⚠️ paper 값 — 진입/HOLD/청산 비용 미차감\n"
                        f"실현 PnL은 청산 완료 메시지에서 확정\n"
                        f"(threshold ${Config.SPREAD_EXIT_THRESHOLD:.0f}, 보유 {hold_hours:.1f}h)"
                    )
                    self.state.cycle_state = CycleState.EXIT
                    self._save_state()
                    return

            # 원금 회수 청산 (MIN_HOLD 무관) — 펀딩 누적 ≥ 진입수수료+spread+청산수수료 시 발동.
            # 2026-04-28 cycle 10 사건 이후: buffer = 2× fee + spread MTM 회귀 마진(safety_margin_usd)
            # 으로 확대. MTM 일시 스파이크에 트리거되어 청산 5~14분 동안 spread 회귀로 손실 발생 방지.
            current_total = self.state.standx_balance + self.state.hibachi_balance
            init_total = self._initial_standx_balance + self._initial_hibachi_balance
            cycle_notional = self._current_cycle.notional if self._current_cycle else 0.0
            if init_total > 0 and cycle_notional > 0 and should_exit_principal_recovered(
                current_total, init_total, cycle_notional, Config.FEE_PER_FILL,
                safety_margin_usd=Config.PRINCIPAL_RECOVERY_SAFETY_MARGIN_USD,
            ):
                if self._current_cycle:
                    self._current_cycle.exit_reason = "principal_recovered"
                fee_buffer = 2 * cycle_notional * Config.FEE_PER_FILL
                safety_margin = Config.PRINCIPAL_RECOVERY_SAFETY_MARGIN_USD
                threshold = init_total + fee_buffer + safety_margin
                await self.telegram.send_alert(
                    f"💰 원금 회수 청산! 잔액 ${current_total:,.2f} ≥ 임계 ${threshold:,.2f}\n"
                    f"(진입 ${init_total:,.2f} + 청산 fee ${fee_buffer:.2f} + spread 마진 ${safety_margin:.2f})\n"
                    f"보유 {hold_hours:.1f}h"
                )
                self.state.cycle_state = CycleState.EXIT
                self._save_state()
                return

            exit_reason = should_exit_cycle(
                hold_hours=hold_hours,
                min_hold_hours=Config.MIN_HOLD_HOURS,
                cumulative_funding_cost=self._cumulative_funding_cost,
                funding_threshold=Config.FUNDING_COST_THRESHOLD,
                margin_ratio_min=worst_margin,
                margin_emergency_pct=Config.MARGIN_EMERGENCY_PCT,
                max_hold_days=Config.MAX_HOLD_DAYS,
            )
            if exit_reason:
                logger.info(
                    "EXIT 트리거(%s): 보유=%.1fh, 펀딩비용=%.6f, 마진=%.1f%%",
                    exit_reason, hold_hours, self._cumulative_funding_cost, worst_margin,
                )
                if self._current_cycle:
                    self._current_cycle.exit_reason = exit_reason
                self.state.cycle_state = CycleState.EXIT
                self._save_state()

        elif state == CycleState.EXIT:
            success = await self._execute_exit()

            if not success:
                # C2: 부분 실패 시 EXIT 유지, 다음 틱에 재시도
                # 단, 연속 N회 실패 시 MANUAL_INTERVENTION으로 전환 — 무한 재호출 방지
                # (2026-04-28 cycle 10 사건: HB 0 / SX 잔여 상태에서 _execute_exit 15회 무의미 재호출).
                self.state.exit_failure_count += 1
                if self.state.exit_failure_count >= Config.MAX_EXIT_FAILURES:
                    logger.error(
                        "EXIT %d회 연속 실패 → MANUAL_INTERVENTION 전환",
                        self.state.exit_failure_count,
                    )
                    self.state.cycle_state = CycleState.MANUAL_INTERVENTION
                    await self.telegram.send_alert(
                        f"⛔ EXIT {self.state.exit_failure_count}회 연속 실패 → 봇 자동 청산 중단.\n"
                        f"수동으로 양쪽 거래소 포지션 정리 후 봇 재시작 필요.\n"
                        f"(재시작 시 _recovery_check가 새 상태 결정)"
                    )
                else:
                    logger.warning(
                        "EXIT 실패 %d/%d, 다음 틱 재시도",
                        self.state.exit_failure_count, Config.MAX_EXIT_FAILURES,
                    )
                self._save_state()
                return

            # 성공했으면 카운터 리셋
            self.state.exit_failure_count = 0

            if not self._current_cycle:
                logger.warning(
                    "EXIT: _current_cycle이 None — state로부터 fallback 재구성 (cycle_id=%d)",
                    self.state.current_cycle_id,
                )
                self._current_cycle = Cycle(
                    cycle_id=self.state.current_cycle_id,
                    direction=self.state.current_direction or "unknown",
                    notional=0.0,
                    entered_at=self._cycle_entered_at or time.time(),
                    exit_reason="recovered_no_context",
                )

            if self._current_cycle:
                self._current_cycle.exited_at = time.time()
                self._current_cycle.exit_sx_price = self.standx_price
                self._current_cycle.exit_hb_price = self.hibachi_price
                if "standx_long" in self._current_cycle.direction:
                    exit_spread = self.hibachi_price - self.standx_price
                else:
                    exit_spread = self.standx_price - self.hibachi_price
                self._current_cycle.exit_spread = exit_spread
                avg_price = (self.standx_price + self.hibachi_price) / 2 or 1
                self._current_cycle.spread_cost = round(
                    (self._current_cycle.entry_spread + exit_spread)
                    * (self._current_cycle.notional / avg_price),
                    2,
                )
                sx_bal = await self.standx.get_balance()
                hb_bal = await self.hibachi.get_balance()
                self._current_cycle.standx_balance_after = self._parse_sx_balance(sx_bal)
                self._current_cycle.hibachi_balance_after = self._parse_hb_balance(hb_bal)
                self.state.standx_balance = self._current_cycle.standx_balance_after
                self.state.hibachi_balance = self._current_cycle.hibachi_balance_after
                self.state.weekly_hibachi_volume += self._current_cycle.notional
                fee = self._current_cycle.notional * Config.FEE_PER_FILL
                self.state.cumulative_fees += fee

                # T3 스냅샷 + 실제 cycle PnL 계산 (4지점 분해)
                t3_total = (
                    self._current_cycle.standx_balance_after
                    + self._current_cycle.hibachi_balance_after
                )
                self._current_cycle.balance_t3_total = t3_total
                t0 = self._current_cycle.balance_t0_total
                t2 = self._current_cycle.balance_t2_total
                if t0 > 0:
                    self._current_cycle.actual_total_pnl = t3_total - t0
                if t2 > 0:
                    self._current_cycle.actual_exit_cost = t2 - t3_total
                # 추정치와 비교 알림
                if t0 > 0:
                    estimated_pnl = self._current_cycle.net_funding - self._current_cycle.fees_paid
                    actual_pnl = self._current_cycle.actual_total_pnl
                    drift = estimated_pnl - actual_pnl
                    logger.info(
                        "Cycle PnL 비교: 봇 추정 $%+.2f vs 실제 $%+.2f (차이 $%+.2f)",
                        estimated_pnl, actual_pnl, drift,
                    )

                self._log_cycle(self._current_cycle)

                hold_h = (self._current_cycle.exited_at - self._current_cycle.entered_at) / 3600
                reason_label = {
                    "spread_opportunity": "💰 기회적",
                    "funding_cost": "📉 펀딩비용",
                    "max_hold": "⏰ 최대보유",
                    "margin_emergency": "🚨 마진긴급",
                    "direction_switch": "🔄 방향전환",
                }.get(self._current_cycle.exit_reason, "🔄")
                await self.telegram.send_alert(
                    f"{reason_label} 사이클 #{self._current_cycle.cycle_id} 청산\n"
                    f"보유: {hold_h:.1f}시간\n"
                    f"진입 스프레드: ${self._current_cycle.entry_spread:.2f}\n"
                    f"청산 스프레드: ${exit_spread:.2f}\n"
                    f"스프레드 비용: ${self._current_cycle.spread_cost:.2f}\n"
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

        elif state == CycleState.MANUAL_INTERVENTION:
            # 봇은 자동 청산을 시도하지 않고 사용자 개입을 기다린다.
            # 30분마다 한 번 알림을 보내 사용자에게 잊지 않도록 reminder.
            # 사용자가 양쪽 거래소 포지션을 수동 청산하면 자동으로 IDLE 복귀 (포지션 0 감지).
            try:
                sx_size = await self._get_standx_position_size()
                hb_size = await self._get_hibachi_position_size()
            except Exception as e:
                logger.warning("MANUAL: 포지션 조회 실패 (계속 대기): %s", e)
                return

            if sx_size <= 0.001 and hb_size <= 0.001:
                logger.info("MANUAL_INTERVENTION: 양쪽 포지션 0 감지 → IDLE 복귀")
                self._positions.pop("standx", None)
                self._positions.pop("hibachi", None)
                self._cycle_entered_at = None
                self.state.cycle_entered_at = 0.0
                self.state.current_direction = ""
                self.state.exit_failure_count = 0
                if self._current_cycle:
                    self._current_cycle.exit_reason = "manual_intervention"
                    self._current_cycle.exited_at = time.time()
                    self._log_cycle(self._current_cycle)
                    self._current_cycle = None
                self.state.cycle_state = CycleState.IDLE
                self._save_state()
                await self.telegram.send_alert(
                    "✅ 수동 청산 감지 → IDLE 복귀. 다음 분석 사이클부터 자동 거래 재개."
                )
                return

            # 30분 쿨다운 reminder
            if time.time() - self._last_warning_time > 1800:
                self._last_warning_time = time.time()
                await self.telegram.send_alert(
                    f"⛔ 봇 정지 중 (MANUAL_INTERVENTION).\n"
                    f"잔여: SX {sx_size:.3f} / HB {hb_size:.6f}\n"
                    f"양쪽 포지션 모두 청산하시면 자동으로 IDLE 복귀하오."
                )

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

            direction_recovered = self.state.current_direction or "standx_short_hibachi_long"
            if "standx_long" in direction_recovered:
                recovered_spread = sx_entry - hb_entry
            else:
                recovered_spread = hb_entry - sx_entry
            self._current_cycle = Cycle(
                cycle_id=self.state.current_cycle_id or 1,
                direction=direction_recovered,
                notional=notional,
                entered_at=self._cycle_entered_at,
                entry_sx_price=sx_entry,
                entry_hb_price=hb_entry,
                entry_spread=recovered_spread,
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
        def _aggregate_realized():
            """cycles.jsonl에서 realized 사이클만 합산 — Status/Detail 공통 헬퍼"""
            agg = {"count": 0, "pnl": 0.0, "funding": 0.0, "fees": 0.0}
            cycles_path = os.path.join(Config.LOG_DIR, "cycles.jsonl")
            if not os.path.exists(cycles_path):
                return agg
            try:
                with open(cycles_path) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        c = json.loads(line)
                        # actual_total_pnl 필드 있는 사이클만 (cycle 8+)
                        if "actual_total_pnl" in c and c.get("balance_t0_total", 0) > 0:
                            agg["pnl"] += c.get("actual_total_pnl", 0.0)
                            agg["funding"] += c.get("net_funding", 0.0)
                            agg["fees"] += c.get("fees_paid", 0.0)
                            agg["count"] += 1
            except Exception as e:
                logger.warning("cycles.jsonl 누적 합산 실패: %s", e)
            return agg

        async def on_status(cb):
            """🅰️ Glanceable Status — 모바일 한 화면, 한 줄 22자 이내"""
            state = self.state.cycle_state
            lines = []

            # ── 헤더: 상태 + 경과시간 + MIN_HOLD 통과 표시 ──
            if state == CycleState.HOLD and self._cycle_entered_at:
                hold_h = (time.time() - self._cycle_entered_at) / 3600
                if hold_h >= Config.MIN_HOLD_HOURS:
                    badge = "✅"
                else:
                    remain = Config.MIN_HOLD_HOURS - hold_h
                    badge = f"⏰{remain:.1f}h남음"
                lines.append(
                    f"📊 #{self.state.current_cycle_id} HOLD {hold_h:.1f}h {badge}"
                )
            elif state == CycleState.COOLDOWN and self._cooldown_until:
                remaining = max(0.0, (self._cooldown_until - time.time()) / 3600)
                lines.append(f"📊 COOLDOWN {remaining:.1f}h 남음")
            else:
                lines.append(f"📊 {state.value}")

            # ── 방향 표시 (포지션 있을 때만) ──
            sx_pos = self._positions.get("standx") if self._positions else None
            hb_pos = self._positions.get("hibachi") if self._positions else None
            if sx_pos:
                color = "🟢" if sx_pos.side == "LONG" else "🔴"
                lines.append(f"{color} StandX {sx_pos.side}")
            if hb_pos:
                color = "🟢" if hb_pos.side == "LONG" else "🔴"
                lines.append(f"{color} Hibachi {hb_pos.side}")
            if (sx_pos and not hb_pos) or (hb_pos and not sx_pos):
                lines.append("⚠️ 편측 포지션!")

            # ── 손익 ──
            total = self.state.standx_balance + self.state.hibachi_balance
            init_total = self._initial_standx_balance + self._initial_hibachi_balance
            lines.append("")
            lines.append(f"💰 ${total:,.2f}")
            if init_total > 0:
                pnl = total - init_total
                pct = pnl / init_total * 100
                emoji = "🟢" if pnl >= 0 else "🔴"
                lines.append(f"{emoji} ${pnl:+,.2f} ({pct:+.2f}%)")
                lines.append(f"진입 ${init_total:,.2f}")

            # ── 마진 / 펀딩 rate (모니터링) + 사이클 분해 (HOLD 활성 시) ──
            if (sx_pos and hb_pos
                    and self.standx_price > 0 and self.hibachi_price > 0):
                sx_margin = sx_pos.calc_margin_ratio(self.standx_price)
                hb_margin = hb_pos.calc_margin_ratio(self.hibachi_price)
                sx_pnl = sx_pos.calc_unrealized_pnl(self.standx_price)
                hb_pnl = hb_pos.calc_unrealized_pnl(self.hibachi_price)
                spread_mtm = sx_pnl + hb_pnl
                lines.append("")
                lines.append(f"🏦 마진 {sx_margin:.0f}% / {hb_margin:.0f}%")
                lines.append(f"⏳ 펀딩 rate {self._cumulative_funding_cost:+.3f}")
                lines.append(f"   임계 ±{Config.FUNDING_COST_THRESHOLD}")

                # ── 청산 트리거 진행 ──
                # 트리거: total ≥ init_total + (notional × FEE_PER_FILL)
                # 잔고는 이미 MTM equity이므로 spread MTM이 자연스럽게 포함됨.
                # buffer는 청산 시 새로 나갈 수수료 (XEMM: StandX taker 0.04%만).
                notional = (sx_pos.notional if sx_pos else hb_pos.notional)
                if init_total > 0 and notional > 0:
                    realized_cycle = total - init_total
                    close_fee_buffer = notional * Config.FEE_PER_FILL
                    trigger_threshold = init_total + close_fee_buffer
                    gap = trigger_threshold - total

                    real_emoji = "🟢" if realized_cycle >= 0 else "🔴"
                    lines.append("")
                    if gap <= 0:
                        lines.append(f"🎯 청산 트리거 도달! (다음 틱 청산)")
                    else:
                        lines.append(f"🎯 청산 트리거: +${gap:,.2f} 남음")
                        lines.append(f"   (목표 ${trigger_threshold:,.2f} = 진입 + 수수료 ${close_fee_buffer:.2f})")
                    lines.append(
                        f"{real_emoji} 잔고 변화 {realized_cycle:+,.2f} "
                        f"(펀딩 누적 + spread MTM ${spread_mtm:+,.2f} − 진입 수수료)"
                    )

            # ── 누적 realized ──
            agg = _aggregate_realized()
            lines.append("")
            lines.append("━ 누적 (realized) ━")
            if agg["count"] > 0:
                emoji = "🟢" if agg["pnl"] >= 0 else "🔴"
                lines.append(
                    f"{emoji} {agg['count']}건 누적 ${agg['pnl']:+,.2f}"
                )
            else:
                lines.append("🌱 cycle 8부터 누적 시작")
            pct_v = self.state.weekly_hibachi_volume / 1000.0
            lines.append(f"📦 거래량 {pct_v:.0f}% / $100K")

            lines.append("")
            lines.append("<i>🔍 자세히 → Detail 버튼</i>")

            await self.telegram.send_main_menu("\n".join(lines))

        async def on_detail(cb):
            """🔍 Detail — 디버그/검증용 풀 정보. 한 줄 22자 이내 유지"""
            lines = ["🔍 <b>Detail</b>"]

            sx_pos = self._positions.get("standx") if self._positions else None
            hb_pos = self._positions.get("hibachi") if self._positions else None

            if sx_pos:
                lines.append("")
                color = "🟢" if sx_pos.side == "LONG" else "🔴"
                lines.append(f"━ {color} StandX {sx_pos.side} ━")
                lines.append(f"notional ${sx_pos.notional:,.0f}")
                lines.append(f"진입 ${sx_pos.entry_price:,.2f}")
                lines.append(f"잔액 ${self.state.standx_balance:,.2f} (MTM)")
                if self.standx_price > 0:
                    sx_pnl = sx_pos.calc_unrealized_pnl(self.standx_price)
                    sx_margin = sx_pos.calc_margin_ratio(self.standx_price)
                    lines.append(f"미실현 ${sx_pnl:+,.2f}")
                    lines.append("<i>※ 잔액에 이미 반영됨</i>")
                    lines.append(f"현재가 ${self.standx_price:,.2f}")
                    lines.append(f"마진율 {sx_margin:.1f}%")
                else:
                    lines.append("가격 로딩중…")

            if hb_pos:
                lines.append("")
                color = "🟢" if hb_pos.side == "LONG" else "🔴"
                lines.append(f"━ {color} Hibachi {hb_pos.side} ━")
                lines.append(f"notional ${hb_pos.notional:,.0f}")
                lines.append(f"진입 ${hb_pos.entry_price:,.2f}")
                lines.append(f"잔액 ${self.state.hibachi_balance:,.2f} (MTM)")
                if self.hibachi_price > 0:
                    hb_pnl = hb_pos.calc_unrealized_pnl(self.hibachi_price)
                    hb_margin = hb_pos.calc_margin_ratio(self.hibachi_price)
                    lines.append(f"미실현 ${hb_pnl:+,.2f}")
                    lines.append("<i>※ 잔액에 이미 반영됨</i>")
                    lines.append(f"현재가 ${self.hibachi_price:,.2f}")
                    lines.append(f"마진율 {hb_margin:.1f}%")
                else:
                    lines.append("가격 로딩중…")

            if (sx_pos and hb_pos
                    and self.standx_price > 0 and self.hibachi_price > 0):
                sx_pnl = sx_pos.calc_unrealized_pnl(self.standx_price)
                hb_pnl = hb_pos.calc_unrealized_pnl(self.hibachi_price)
                lines.append("")
                lines.append("━ spread / 펀딩 ━")
                lines.append(f"spread MTM ${sx_pnl + hb_pnl:+,.2f}")
                lines.append("<i>※ 거래소간 가격 격차 MTM,</i>")
                lines.append("<i>  ETH 델타 아님</i>")
                lines.append(
                    f"펀딩 rate {self._cumulative_funding_cost:+.6f}"
                )
                lines.append(f"청산 임계 ±{Config.FUNDING_COST_THRESHOLD}")

            if not self._positions:
                lines.append("")
                lines.append("━ 잔고 ━")
                lines.append(f"StandX  ${self.state.standx_balance:,.2f}")
                lines.append(f"Hibachi ${self.state.hibachi_balance:,.2f}")
                total = self.state.standx_balance + self.state.hibachi_balance
                lines.append(f"💼 합계 ${total:,.2f}")
                init_total = (self._initial_standx_balance
                              + self._initial_hibachi_balance)
                if init_total > 0:
                    pnl = total - init_total
                    emoji = "🟢" if pnl >= 0 else "🔴"
                    lines.append(f"{emoji} ${pnl:+,.2f} (진입 대비)")

            # 누적 realized
            agg = _aggregate_realized()
            lines.append("")
            lines.append("━ 누적 (realized) ━")
            if agg["count"] > 0:
                emoji = "🟢" if agg["pnl"] >= 0 else "🔴"
                lines.append(f"완료 사이클 {agg['count']}건")
                lines.append(f"{emoji} 누적 ${agg['pnl']:+,.2f}")
                lines.append(f"  └ 펀딩 ${agg['funding']:+,.2f}")
                lines.append(f"  └ 수수료 -${agg['fees']:,.2f}")
            else:
                lines.append("🌱 cycle 8부터 누적 시작")

            pct_v = self.state.weekly_hibachi_volume / 1000.0
            lines.append("")
            lines.append("📦 주간 거래량")
            lines.append(
                f"${self.state.weekly_hibachi_volume:,.0f} / $100K ({pct_v:.1f}%)"
            )

            await self.telegram.send_alert("\n".join(lines))

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

        async def on_force_exit(cb):
            """🔚 Close Now — 현재 사이클 즉시 청산 + 쿨다운 스킵 → 즉시 다음 사이클 진입"""
            if self.state.cycle_state != CycleState.HOLD:
                await self.telegram.send_alert(
                    f"현재 상태({self.state.cycle_state.value})에서는 강제 청산 불가\n"
                    f"HOLD 상태에서만 가능"
                )
                return
            if not self._positions:
                await self.telegram.send_alert("청산할 포지션 없음")
                return

            await self.telegram.send_alert(
                f"🔚 강제 청산 시작 (사이클 #{self.state.current_cycle_id}, XEMM)"
            )

            if self._current_cycle:
                self._current_cycle.exit_reason = "manual_force"
                self._current_cycle.exited_at = time.time()
                self._current_cycle.exit_sx_price = self.standx_price
                self._current_cycle.exit_hb_price = self.hibachi_price

            success = await self._execute_exit()

            if not success:
                await self.telegram.send_alert("🚨 강제 청산 부분 실패. 수동 확인 필요!")
                return

            if self._current_cycle:
                self._log_cycle(self._current_cycle)
                self._current_cycle = None

            # 쿨다운 스킵 → 즉시 다음 사이클 진입 시도
            self._cooldown_until = None
            self.state.cooldown_until = 0.0
            self.state.cycle_state = CycleState.IDLE
            self._save_state()

            await self.telegram.send_alert(
                f"✅ 사이클 #{self.state.current_cycle_id} 강제 청산 완료\n"
                f"🚀 쿨다운 스킵 → 다음 틱(5초)에 사이클 #{self.state.current_cycle_id + 1} 진입 시도"
            )

        async def on_stop(cb):
            await self.telegram.send_alert("⏹ 봇 종료 중... 포지션 청산")
            self._running = False  # C7: 먼저 플래그 설정
            if self._positions:
                if self._current_cycle:
                    self._current_cycle.exit_reason = "manual_stop"
                    self._current_cycle.exited_at = time.time()
                    self._current_cycle.exit_sx_price = self.standx_price
                    self._current_cycle.exit_hb_price = self.hibachi_price
                success = await self._execute_exit()
                if self._current_cycle:
                    self._log_cycle(self._current_cycle)
                    self._current_cycle = None
                if not success:
                    await self.telegram.send_alert("🚨 Stop 청산 부분 실패! 수동 확인 필요!")
            self._save_state()
            # Watchdog 영구 정지 (재시작 방지)
            stop_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".stop_bot")
            with open(stop_file, "w") as f:
                f.write(str(time.time()))

        from telegram_ui import (
            BTN_STATUS, BTN_DETAIL, BTN_HISTORY,
            BTN_FUNDING, BTN_REBALANCE, BTN_FORCE_EXIT, BTN_STOP,
        )
        self.telegram.register_callback(BTN_STATUS, on_status)
        self.telegram.register_callback(BTN_DETAIL, on_detail)
        self.telegram.register_callback(BTN_HISTORY, on_history)
        self.telegram.register_callback(BTN_FUNDING, on_funding)
        self.telegram.register_callback(BTN_REBALANCE, on_rebalance)
        self.telegram.register_callback(BTN_FORCE_EXIT, on_force_exit)
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

                # REST 가격 폴링 (5초마다) — 양쪽 독립 처리
                if now - last_price_check > 5:
                    last_price_check = now
                    try:
                        sx_market = await self.standx.get_market_price(Config.PAIR_STANDX)
                        self.standx_price = float(sx_market.get("mark_price", 0))
                    except Exception as e:
                        logger.warning("StandX 가격 폴링 실패: %s", e)
                    try:
                        self.hibachi_price = await self.hibachi.get_mark_price(Config.PAIR_HIBACHI)
                    except Exception as e:
                        logger.warning("Hibachi 가격 폴링 실패: %s", e)

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
                            # RC-4: 긴급 청산 후 Cycle 기록 + 상태 정리
                            if self._current_cycle:
                                self._current_cycle.exit_reason = "api_emergency"
                                self._current_cycle.exited_at = time.time()
                                self._current_cycle.exit_sx_price = self.standx_price
                                self._current_cycle.exit_hb_price = self.hibachi_price
                                self._log_cycle(self._current_cycle)
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
                                    if self._current_cycle:
                                        self._current_cycle.exit_reason = "direction_switch"
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
