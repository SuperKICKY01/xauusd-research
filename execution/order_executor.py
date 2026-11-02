"""
execution/order_executor.py — MT5 Order Placement Engine (market | limit)

Subscribes to TRADE_DECISION from the event bus. The execution path is selected
by CFG.execution.mode:

  • "market" (LIVE DEFAULT) — sends MARKET deals at the current price through a
    slippage gate (entry anchored to the OB MID). Publishes TRADE_EXECUTED +
    POSITION_OPENED on fill (the SAME payload PendingOrderManager emits, so
    BreakEvenMonitor / Notifier / TradeLogger are untouched). The pending-limit
    system did NOT pass its backtest deploy gate (req 12, see
    lessons/2026-05-29-pending-limit-order-system.md), so live runs this path.

  • "limit" — places PENDING LIMIT orders at the OB proximal edge and publishes
    ORDER_PENDING (locks the OB, hands lifecycle tracking to PendingOrderManager).
    Dormant but fully intact for a future re-test. ⚠️ Pair with
    signal_builder.ENTRY_AT_PROXIMAL=True.

For approved=False: silently skips (RiskManager already published TRADE_REJECTED).

Why limit orders (not market):
  Entry rests at the OB proximal edge — the price the retrace touches first.
  A resting limit fills at that exact price (no entry slippage), so there is
  NO slippage-rejection gate on this path (cf. the old market flow). The
  slippage *buffer* still lives in RiskManager lot sizing for the EXIT/SL.

Dual-TP strategy (unchanged split, now as two pending legs at the SAME price):
    Leg 1: lot/2 → TP1   (early partial profit)
    Leg 2: lot/2 → TP2   (runner)
  If halving falls below volume_min, a single SOLO leg is placed at TP2.

Expiration (req 3):
  ORDER_TIME_SPECIFIED, expiring at the END of the Nth H1 candle from
  placement (CFG.pending.expiry_h1_candles; default 1 = end of next H1, ≤2h).
  Computed in SERVER time from the latest tick, floored to the H1 boundary.

Side validation (req 8):
  BUY_LIMIT must rest BELOW the ask; SELL_LIMIT ABOVE the bid. Wrong side →
  SKIP + log (NO market fallback — limit-only by design).

Filling mode (req 9):
  FOK → IOC → RETURN ladder, probed from symbol_info.filling_mode; advances
  on retcode 10030. Pending orders normally land on RETURN.

Retries (req 11):
  10004 (requote) / 10015 (invalid price) → re-validate side on a fresh tick,
  retry while still valid, else SKIP. Bounded by CFG.pending.max_place_retries.

CRITICAL: every mt5.* call is wrapped in asyncio.to_thread() — no blocking.
TRADE_EXECUTED is published by PendingOrderManager ON FILL, not here.
"""

import asyncio
import logging
import time
from datetime import datetime
from typing import Optional

import MetaTrader5 as mt5

from config import CFG
from core.event_bus import bus, Topics, request_shutdown, send_alert
from agents.risk_manager import TradeDecision
from execution.lot_calculator import refine_lot, split_lot

logger = logging.getLogger(__name__)

_RETRY_DELAY  = 1.0                        # seconds between retry attempts
ORDER_MAGIC   = 20250505                   # Public — recovery filter for BreakEvenMonitor / PendingOrderManager
ORDER_COMMENT = "xau_agent"                # Public — leg suffixes: _tp1, _tp2

# Backwards-compat aliases (older code paths may still reference the underscored names)
_ORDER_MAGIC   = ORDER_MAGIC
_ORDER_COMMENT = ORDER_COMMENT

# ENUM_SYMBOL_FILLING_MODE bit flags. The MetaTrader5 Python package does NOT
# expose SYMBOL_FILLING_FOK / SYMBOL_FILLING_IOC as constants, so we use the
# documented bit values. They are the bits set in symbol_info().filling_mode.
# RETURN has no bit — it is the implicit fallback (and the mode pending orders
# normally accept).
_SYMBOL_FILLING_FOK = 1
_SYMBOL_FILLING_IOC = 2

# Market-mode transient retcodes that warrant a price refresh + retry. Built
# with getattr so a constant the installed MetaTrader5 package does not expose
# (cf. lesson Rule B — SYMBOL_FILLING_* were missing) can never raise here.
_RETRYABLE_MARKET_RETCODES: set[int] = set()
for _rc_name in ("TRADE_RETCODE_REQUOTE", "TRADE_RETCODE_INVALID_PRICE",
                 "TRADE_RETCODE_PRICE_OFF", "TRADE_RETCODE_PRICE_CHANGED"):
    _rc_val = getattr(mt5, _rc_name, None)
    if _rc_val is not None:
        _RETRYABLE_MARKET_RETCODES.add(_rc_val)


# ── Markout telemetry (infra-only) ─────────────────────────────────────
# Sample points (seconds after a confirmed fill) at which we record the
# market mid-price to measure post-fill adverse selection. Persisted to
# trades.db by TradeLogger via the MARKOUT_RECORDED event.
_MARKOUT_OFFSETS_SEC: tuple[tuple[float, str], ...] = (
    (0.1,  "markout_100ms"),
    (1.0,  "markout_1s"),
    (5.0,  "markout_5s"),
    (30.0, "markout_30s"),
)

# Fire-and-forget markout tasks are held here so the loop does not GC them
# mid-flight (asyncio keeps only a weak reference to a bare create_task()).
_MARKOUT_TASKS: "set[asyncio.Task]" = set()


def _resolve_filling_modes(sym_info: dict) -> list[int]:
    """
    Build the ordered type_filling ladder FOK → IOC → RETURN from the
    symbol's allowed-filling bitmask (req 9). RETURN is always appended as
    the final fallback — it is the mode pending orders normally accept.
    """
    mask = sym_info.get("filling_mode", 0) or 0
    ladder: list[int] = []
    if mask & _SYMBOL_FILLING_FOK:
        ladder.append(mt5.ORDER_FILLING_FOK)
    if mask & _SYMBOL_FILLING_IOC:
        ladder.append(mt5.ORDER_FILLING_IOC)
    ladder.append(mt5.ORDER_FILLING_RETURN)
    return ladder


class OrderExecutor:
    """
    Consumes TRADE_DECISION events and places PENDING LIMIT orders on MT5.
    Publishes ORDER_PENDING on successful placement (partial or full).
    """

    async def start(self) -> None:
        mode = CFG.execution.mode
        if mode not in ("market", "limit"):
            logger.warning(
                f"OrderExecutor: unknown CFG.execution.mode='{mode}' — defaulting to 'market'."
            )
            mode = "market"
        logger.info(f"⚡ OrderExecutor online — listening for TRADE_DECISION ({mode}-execution mode).")
        queue = await bus.subscribe(Topics.TRADE_DECISION)

        while True:
            try:
                decision: TradeDecision = await asyncio.wait_for(queue.get(), timeout=60.0)

                if not decision.approved:
                    logger.debug(f"OrderExecutor: skipping vetoed decision — {decision.veto_reason}")
                    continue

                # Re-read mode each decision so an .env change takes effect on
                # restart without code edits; default anything unknown to market.
                if CFG.execution.mode == "limit":
                    await self._handle_decision(decision)
                else:
                    await self._handle_market_decision(decision)

            except asyncio.TimeoutError:
                logger.debug("OrderExecutor: no TRADE_DECISION in 60s — still listening.")
            except Exception as e:
                logger.error(f"OrderExecutor main loop error: {e}", exc_info=True)

    async def _publish_rejection(self, decision: TradeDecision, reason: str) -> None:
        """Publish TRADE_REJECTED with the standard payload shape."""
        await bus.publish(Topics.TRADE_REJECTED, {
            "reason":    reason,
            "agent":     "OrderExecutor",
            "signal":    decision.gate_decision.signal,
            "timestamp": datetime.now().isoformat(),
        })

    # ─────────────────────────────────────────────────────────────────
    # Decision Handler
    # ─────────────────────────────────────────────────────────────────

    async def _handle_decision(self, decision: TradeDecision) -> None:
        """
        Refines lot, validates the limit side, splits into dual-TP legs, and
        places PENDING LIMIT orders at the OB proximal edge. Publishes
        ORDER_PENDING; never publishes TRADE_EXECUTED (that fires on fill).
        """
        # ── Final lot refinement ───────────────────────────────────────
        lot, sym_info = await refine_lot(decision.symbol, decision.lot_size)
        if lot <= 0.0 or sym_info is None:
            logger.error(
                f"OrderExecutor: lot refinement failed for {decision.symbol} "
                f"(raw={decision.lot_size:.2f}) — trade aborted."
            )
            await bus.publish(Topics.TRADE_REJECTED, {
                "reason":    "Lot refinement failed — below broker volume_min at execution time.",
                "agent":     "OrderExecutor",
                "signal":    decision.gate_decision.signal,
                "timestamp": datetime.now().isoformat(),
            })
            return

        # ── Resolve pending order type (limit at the OB proximal edge) ──
        order_type  = mt5.ORDER_TYPE_BUY_LIMIT if decision.direction == "BUY" else mt5.ORDER_TYPE_SELL_LIMIT
        limit_price = decision.entry_price   # = OB proximal edge, set by signal_builder

        # ── Live tick (side validation + server-time expiration anchor) ──
        tick = await asyncio.to_thread(mt5.symbol_info_tick, decision.symbol)
        if tick is None:
            logger.error(f"OrderExecutor: cannot fetch live tick for {decision.symbol} — aborting.")
            return

        # ── Req 8: side validation — wrong side → SKIP + log (no market) ──
        if not self._valid_side(order_type, limit_price, tick):
            reason = (
                f"Wrong-side limit: {decision.direction}_LIMIT @ {limit_price:.2f} "
                f"vs market bid={tick.bid:.2f}/ask={tick.ask:.2f} "
                f"(BUY_LIMIT must be < ask, SELL_LIMIT must be > bid) — "
                f"skipped, no order placed."
            )
            logger.warning(f"OrderExecutor: {reason}")
            await bus.publish(Topics.TRADE_REJECTED, {
                "reason":    reason,
                "agent":     "OrderExecutor",
                "signal":    decision.gate_decision.signal,
                "timestamp": datetime.now().isoformat(),
            })
            return

        # ── Broker minimum SL/TP distance (measured from the LIMIT price) ──
        # SL/TP must clear trade_stops_level + live spread from the limit.
        stops_pts  = sym_info.get("trade_stops_level", 0)
        spread_pts = sym_info.get("spread", 0)
        point      = sym_info["point"]
        min_stop_distance = (stops_pts + spread_pts) * point

        sl_distance = abs(limit_price - decision.stop_loss)
        if sl_distance < min_stop_distance:
            logger.error(
                f"OrderExecutor: SL distance {sl_distance:.2f} < broker min "
                f"{min_stop_distance:.2f} ({stops_pts}pts + {spread_pts}pt spread) "
                f"from limit — trade aborted."
            )
            await bus.publish(Topics.TRADE_REJECTED, {
                "reason":    f"SL too close to limit ({sl_distance:.2f} < {min_stop_distance:.2f}).",
                "agent":     "OrderExecutor",
                "signal":    decision.gate_decision.signal,
                "timestamp": datetime.now().isoformat(),
            })
            return

        tp1_distance = abs(limit_price - decision.take_profit_1)
        tp1_viable   = tp1_distance >= min_stop_distance
        tp1_skip_note: Optional[str] = None
        if not tp1_viable:
            tp1_skip_note = (
                f"TP1 skipped (too close to broker min): "
                f"distance {tp1_distance:.2f} < min {min_stop_distance:.2f} "
                f"({stops_pts}pts + {spread_pts}pt spread). Full lot placed at TP2."
            )
            logger.warning(f"OrderExecutor: {tp1_skip_note}")

        # ── Dual-TP lot split ──────────────────────────────────────────
        if tp1_viable:
            lot_tp1, lot_tp2 = split_lot(lot, sym_info["volume_min"], sym_info["volume_step"])
        else:
            lot_tp1, lot_tp2 = 0.0, lot

        # ── Expiration: end of the Nth H1 candle, in SERVER time ───────
        # Floor the last tick's server time to the H1 boundary, then add
        # (1 + expiry_h1_candles) hours. Flooring the epoch to 3600 equals
        # the broker's H1 boundary for any whole-hour server offset.
        hours_ahead = 1 + CFG.pending.expiry_h1_candles
        expiration  = (int(tick.time) // 3600) * 3600 + hours_ahead * 3600

        # ── Filling-mode ladder (FOK → IOC → RETURN) ───────────────────
        filling_modes = _resolve_filling_modes(sym_info)

        orders_placed: list[dict] = []

        # ── Pending leg 1: TP1 (skipped when TP1 is under broker min) ──
        if lot_tp1 > 0.0:
            res_tp1 = await self._send_pending_with_retry(
                symbol        = decision.symbol,
                order_type    = order_type,
                lot           = lot_tp1,
                price         = limit_price,
                sl            = decision.stop_loss,
                tp            = decision.take_profit_1,
                comment       = f"{_ORDER_COMMENT}_tp1",
                expiration    = expiration,
                filling_modes = filling_modes,
            )
            if res_tp1:
                res_tp1.update({"leg": "tp1", "lot": lot_tp1, "tp": decision.take_profit_1})
                orders_placed.append(res_tp1)
                logger.info(
                    f"⏳ TP1 LIMIT placed | #{res_tp1['order']} | {decision.direction} "
                    f"{lot_tp1:.2f} @ {limit_price:.2f} | SL={decision.stop_loss:.2f} "
                    f"TP={decision.take_profit_1:.2f}"
                )
            else:
                logger.error(f"OrderExecutor: TP1 pending leg FAILED — {decision.direction} {decision.symbol}")

        # ── Pending leg 2: TP2 ─────────────────────────────────────────
        if lot_tp2 > 0.0:
            res_tp2 = await self._send_pending_with_retry(
                symbol        = decision.symbol,
                order_type    = order_type,
                lot           = lot_tp2,
                price         = limit_price,
                sl            = decision.stop_loss,
                tp            = decision.take_profit_2,
                comment       = f"{_ORDER_COMMENT}_tp2",
                expiration    = expiration,
                filling_modes = filling_modes,
            )
            if res_tp2:
                res_tp2.update({"leg": "tp2", "lot": lot_tp2, "tp": decision.take_profit_2})
                orders_placed.append(res_tp2)
                logger.info(
                    f"⏳ TP2 LIMIT placed | #{res_tp2['order']} | {decision.direction} "
                    f"{lot_tp2:.2f} @ {limit_price:.2f} | SL={decision.stop_loss:.2f} "
                    f"TP={decision.take_profit_2:.2f}"
                )
            else:
                logger.warning("OrderExecutor: TP2 pending leg failed — TP1 leg (if placed) runs alone.")

        if not orders_placed:
            logger.error("OrderExecutor: no pending legs placed — trade skipped.")
            return

        # ── Publish ORDER_PENDING (locks OB; hands tracking to manager) ──
        # ob_identity matches the (direction, top, bottom) tuple SignalBuilder
        # filters on. Skip FVG-only setups (no OB lock to honor).
        signal = decision.gate_decision.signal
        ob_identity = None
        if signal is not None and signal.primary_ob is not None:
            ob = signal.primary_ob
            ob_identity = (ob.direction.value, ob.top, ob.bottom)

        await bus.publish(Topics.ORDER_PENDING, {
            "decision":    decision,
            "orders":      orders_placed,        # each: order, leg, lot, tp, price, retcode
            "limit_price": limit_price,
            "sl":          decision.stop_loss,
            "tp1":         decision.take_profit_1,
            "tp2":         decision.take_profit_2,
            "direction":   decision.direction,
            "symbol":      decision.symbol,
            "expiration":  expiration,
            "ob_identity": ob_identity,
            "lot_tp1":     lot_tp1,
            "lot_tp2":     lot_tp2 if len(orders_placed) > 1 else 0.0,
            "notes":       tp1_skip_note,
            "timestamp":   datetime.now().isoformat(),
        })

        logger.info(
            f"📌 PENDING PLACED | {decision.direction} {decision.symbol} | "
            f"limit={limit_price:.2f} | legs={len(orders_placed)} | "
            f"SL={decision.stop_loss:.2f} TP1={decision.take_profit_1:.2f} "
            f"TP2={decision.take_profit_2:.2f} | "
            f"expires={datetime.fromtimestamp(expiration).strftime('%Y-%m-%d %H:%M')} (server)"
        )

    # ─────────────────────────────────────────────────────────────────
    # MT5 Pending Order Submission
    # ─────────────────────────────────────────────────────────────────

    async def _send_pending_with_retry(
        self,
        *,
        symbol:        str,
        order_type:    int,
        lot:           float,
        price:         float,
        sl:            float,
        tp:            float,
        comment:       str,
        expiration:    int,
        filling_modes: list[int],
    ) -> Optional[dict]:
        """
        Submit a PENDING LIMIT order with TRADE_ACTION_PENDING / ORDER_TIME_SPECIFIED.

        - 10030 (unsupported filling) → advance the FOK→IOC→RETURN ladder.
        - 10015 (invalid price) / 10004 (requote) → re-validate the limit side
          on a fresh tick; retry while still valid, else SKIP (no market fallback).
        Returns {order, volume, price, retcode} on DONE, else None after retries.
        """
        fmode_idx = 0
        for attempt in range(1, CFG.pending.max_place_retries + 1):
            type_filling = filling_modes[min(fmode_idx, len(filling_modes) - 1)]
            request = {
                "action":       mt5.TRADE_ACTION_PENDING,
                "symbol":       symbol,
                "volume":       lot,
                "type":         order_type,
                "price":        price,
                "sl":           sl,
                "tp":           tp,
                "magic":        _ORDER_MAGIC,
                "comment":      comment,
                "type_time":    mt5.ORDER_TIME_SPECIFIED,
                "expiration":   expiration,
                "type_filling": type_filling,
            }

            result = await asyncio.to_thread(mt5.order_send, request)

            if result is None:
                logger.warning(
                    f"order_send(pending) returned None (attempt {attempt}/"
                    f"{CFG.pending.max_place_retries}) for {symbol} — MT5 disconnected?"
                )
                await asyncio.sleep(_RETRY_DELAY)
                continue

            if result.retcode == mt5.TRADE_RETCODE_DONE:
                return {"order": result.order, "volume": result.volume,
                        "price": price, "retcode": result.retcode}

            # Unsupported filling mode → advance the ladder, retry immediately.
            if result.retcode == mt5.TRADE_RETCODE_INVALID_FILL:
                if fmode_idx < len(filling_modes) - 1:
                    fmode_idx += 1
                    logger.warning(
                        f"Pending {comment}: filling mode unsupported (10030) — "
                        f"advancing ladder to {filling_modes[fmode_idx]}."
                    )
                    continue
                logger.error(f"Pending {comment}: all filling modes exhausted (10030) — skipping leg.")
                return None

            # Invalid price / requote → re-validate side on a fresh tick.
            if result.retcode in (mt5.TRADE_RETCODE_INVALID_PRICE, mt5.TRADE_RETCODE_REQUOTE):
                tick = await asyncio.to_thread(mt5.symbol_info_tick, symbol)
                if tick is None or not self._valid_side(order_type, price, tick):
                    logger.error(
                        f"Pending {comment}: limit {price:.2f} now wrong-side of market "
                        f"(retcode {result.retcode}) — skipping leg (no market fallback)."
                    )
                    return None
                logger.warning(
                    f"Pending {comment}: retcode {result.retcode} "
                    f"(attempt {attempt}/{CFG.pending.max_place_retries}) — "
                    f"limit still valid, retrying."
                )
                await asyncio.sleep(_RETRY_DELAY)
                continue

            # Any other retcode → log + bounded retry.
            logger.error(
                f"Pending order rejected | {comment} | lot={lot:.2f} | "
                f"retcode={result.retcode} | broker='{result.comment}' | "
                f"attempt {attempt}/{CFG.pending.max_place_retries}"
            )
            await asyncio.sleep(_RETRY_DELAY)

        return None

    @staticmethod
    def _valid_side(order_type: int, price: float, tick) -> bool:
        """
        Req 8: BUY_LIMIT must rest BELOW the ask; SELL_LIMIT ABOVE the bid.
        Returns False for any other order type (limit-only engine).
        """
        if order_type == mt5.ORDER_TYPE_BUY_LIMIT:
            return price < tick.ask
        if order_type == mt5.ORDER_TYPE_SELL_LIMIT:
            return price > tick.bid
        return False

    # ─────────────────────────────────────────────────────────────────
    # MARKET Execution (legacy baseline — live default; see module docstring)
    # ─────────────────────────────────────────────────────────────────

    async def _handle_market_decision(self, decision: TradeDecision) -> None:
        """
        MARKET execution. Sends MARKET deals at the current price with an
        RR-based fill gate: at the live fill, if the blended reward:risk
        (distance to the TP1/TP2 midpoint ÷ distance to the SL) is below
        CFG.execution.min_fill_rr, the trade is REJECTED (no order sent).
        Publishes TRADE_EXECUTED +
        POSITION_OPENED on fill — the SAME payload shape PendingOrderManager emits,
        so BreakEvenMonitor / Notifier / TradeLogger are untouched.
        """
        # ── Final lot refinement ───────────────────────────────────────
        lot, sym_info = await refine_lot(decision.symbol, decision.lot_size)
        if lot <= 0.0 or sym_info is None:
            logger.error(
                f"OrderExecutor[market]: lot refinement failed for {decision.symbol} "
                f"(raw={decision.lot_size:.2f}) — trade aborted."
            )
            await self._publish_rejection(
                decision, "Lot refinement failed — below broker volume_min at execution time."
            )
            return

        order_type = mt5.ORDER_TYPE_BUY if decision.direction == "BUY" else mt5.ORDER_TYPE_SELL

        # ── Live tick — prospective fill + slippage-gate anchor ────────
        tick = await asyncio.to_thread(mt5.symbol_info_tick, decision.symbol)
        if tick is None:
            logger.error(f"OrderExecutor[market]: cannot fetch live tick for {decision.symbol} — aborting.")
            return
        expected_fill = tick.ask if order_type == mt5.ORDER_TYPE_BUY else tick.bid

        # ── RR-based fill gate ─────────────────────────────────────────
        # Slippage hurts only insofar as it erodes the trade's reward:risk, so
        # re-evaluate RR at the LIVE fill (not the planned OB-mid entry). A worse
        # fill both shrinks the distance to target AND widens the distance to the
        # SL, so a single RR check captures both effects (and a favorable fill,
        # which raises RR, always passes — unlike the old magnitude gate).
        #   reward = distance from fill to the blended TP (TP1/TP2 midpoint)
        #   risk   = distance from fill to the SL
        # Reject if RR < CFG.execution.min_fill_rr. The position's SL/TP are the
        # decision's fixed levels, so this is the RR the live trade really carries.
        planned_entry = decision.entry_price
        tp_blended    = (decision.take_profit_1 + decision.take_profit_2) / 2.0

        if decision.direction == "BUY":
            risk_from_fill   = expected_fill - decision.stop_loss
            reward_from_fill = tp_blended - expected_fill
        else:
            risk_from_fill   = decision.stop_loss - expected_fill
            reward_from_fill = expected_fill - tp_blended

        if risk_from_fill <= 0:
            # Fill landed at/through the SL — no valid risk leg; never trade this.
            reason = (
                f"Fill through SL: live={expected_fill:.2f} SL={decision.stop_loss:.2f} "
                f"(risk_from_fill={risk_from_fill:.2f} <= 0). planned_entry={planned_entry:.2f}"
            )
            logger.warning(f"OrderExecutor[market]: {reason}")
            await self._publish_rejection(decision, reason)
            return

        rr_at_fill = reward_from_fill / risk_from_fill
        if rr_at_fill < CFG.execution.min_fill_rr:
            reason = (
                f"Fill RR too low: {rr_at_fill:.2f} < {CFG.execution.min_fill_rr:.2f} min. "
                f"planned_entry={planned_entry:.2f} live={expected_fill:.2f} "
                f"SL={decision.stop_loss:.2f} TP_blended={tp_blended:.2f} "
                f"(reward={reward_from_fill:.2f} / risk={risk_from_fill:.2f})"
            )
            logger.warning(f"OrderExecutor[market]: {reason}")
            await self._publish_rejection(decision, reason)
            return

        # ── Broker minimum SL/TP distance (measured from the prospective fill)
        stops_pts  = sym_info.get("trade_stops_level", 0)
        spread_pts = sym_info.get("spread", 0)
        point      = sym_info["point"]
        min_stop_distance = (stops_pts + spread_pts) * point

        sl_from_fill = abs(expected_fill - decision.stop_loss)
        if sl_from_fill < min_stop_distance:
            logger.error(
                f"OrderExecutor[market]: SL distance {sl_from_fill:.2f} < broker min "
                f"{min_stop_distance:.2f} from fill — trade aborted."
            )
            await self._publish_rejection(
                decision, f"SL too close to fill ({sl_from_fill:.2f} < {min_stop_distance:.2f})."
            )
            return

        tp1_distance = abs(expected_fill - decision.take_profit_1)
        tp1_viable   = tp1_distance >= min_stop_distance
        tp1_skip_note: Optional[str] = None
        if not tp1_viable:
            tp1_skip_note = (
                f"TP1 skipped (too close to broker min): distance {tp1_distance:.2f} "
                f"< min {min_stop_distance:.2f}. Full lot placed at TP2."
            )
            logger.warning(f"OrderExecutor[market]: {tp1_skip_note}")

        # ── Dual-TP lot split ──────────────────────────────────────────
        if tp1_viable:
            lot_tp1, lot_tp2 = split_lot(lot, sym_info["volume_min"], sym_info["volume_step"])
        else:
            lot_tp1, lot_tp2 = 0.0, lot

        filling_modes = self._resolve_market_filling_modes(sym_info)
        orders_placed: list[dict] = []
        unconfirmed:   list[int]  = []   # 0B: DONE sends whose deal never posted to history

        # ── Market leg 1: TP1 (skipped when TP1 is under broker min) ───
        if lot_tp1 > 0.0:
            res_tp1 = await self._send_market_with_retry(
                symbol=decision.symbol, order_type=order_type, lot=lot_tp1,
                sl=decision.stop_loss, tp=decision.take_profit_1,
                comment=f"{_ORDER_COMMENT}_tp1", filling_modes=filling_modes,
            )
            if res_tp1:
                leg = await self._confirm_or_flag(
                    res_tp1, "tp1", lot_tp1, decision.take_profit_1, unconfirmed
                )
                if leg is not None:
                    orders_placed.append(leg)
                    logger.info(
                        f"✅ TP1 MARKET filled (confirmed) | #{leg['order']} | {decision.direction} "
                        f"{lot_tp1:.2f} @ {leg['price']:.2f} | SL={decision.stop_loss:.2f} "
                        f"TP={decision.take_profit_1:.2f}"
                    )
            else:
                logger.error(f"OrderExecutor[market]: TP1 market leg FAILED — {decision.direction} {decision.symbol}")

        # ── Market leg 2: TP2 ──────────────────────────────────────────
        if lot_tp2 > 0.0:
            res_tp2 = await self._send_market_with_retry(
                symbol=decision.symbol, order_type=order_type, lot=lot_tp2,
                sl=decision.stop_loss, tp=decision.take_profit_2,
                comment=f"{_ORDER_COMMENT}_tp2", filling_modes=filling_modes,
            )
            if res_tp2:
                leg = await self._confirm_or_flag(
                    res_tp2, "tp2", lot_tp2, decision.take_profit_2, unconfirmed
                )
                if leg is not None:
                    orders_placed.append(leg)
                    logger.info(
                        f"✅ TP2 MARKET filled (confirmed) | #{leg['order']} | {decision.direction} "
                        f"{lot_tp2:.2f} @ {leg['price']:.2f} | SL={decision.stop_loss:.2f} "
                        f"TP={decision.take_profit_2:.2f}"
                    )
            else:
                logger.warning("OrderExecutor[market]: TP2 market leg failed — TP1 leg (if filled) runs alone.")

        if not orders_placed:
            # Nothing confirmed. If a DONE send went unconfirmed, that is a possible
            # phantom fill — alert + halt rather than silently skip (0B).
            if unconfirmed:
                await self._halt_on_unconfirmed(decision, unconfirmed)
            else:
                logger.error("OrderExecutor[market]: no market legs filled — trade skipped.")
            return

        # ── ob_identity for the OB lock (same tuple SignalBuilder filters on) ──
        signal = decision.gate_decision.signal
        ob_identity = None
        if signal is not None and getattr(signal, "primary_ob", None) is not None:
            ob = signal.primary_ob
            ob_identity = (ob.direction.value, ob.top, ob.bottom)

        actual_fill  = orders_placed[0]["price"]   # representative fill (legs fill ~same price)
        lot_tp1_done = next((o["lot"] for o in orders_placed if o["leg"] == "tp1"), 0.0)
        lot_tp2_done = next((o["lot"] for o in orders_placed if o["leg"] == "tp2"), 0.0)

        await bus.publish(Topics.TRADE_EXECUTED, {
            "decision":    decision,
            "orders":      [{"order": o["order"], "leg": o["leg"], "lot": o["lot"], "price": o["price"]}
                            for o in orders_placed],
            "lot_tp1":     lot_tp1_done,
            "lot_tp2":     lot_tp2_done if len(orders_placed) > 1 else 0.0,
            "entry_price": actual_fill,
            "notes":       tp1_skip_note if tp1_skip_note
                           else (None if len(orders_placed) > 1 else "Single leg filled (SOLO)."),
            "timestamp":   datetime.now().isoformat(),
        })
        if ob_identity is not None:
            await bus.publish(Topics.POSITION_OPENED, {
                "ob_identity": ob_identity,
                "ticket":      orders_placed[0]["order"],
                "timestamp":   datetime.now().isoformat(),
            })

        logger.info(
            f"🎯 MARKET EXECUTED → TRADE_EXECUTED | {decision.direction} {decision.symbol} | "
            f"legs={len(orders_placed)} | fill≈{actual_fill:.2f} | SL={decision.stop_loss:.2f} "
            f"TP1={decision.take_profit_1:.2f} TP2={decision.take_profit_2:.2f} | "
            f"fill_RR={rr_at_fill:.2f}"
        )

        # ── Markout telemetry (infra-only): sample post-fill price drift at
        #    100ms/1s/5s/30s and persist next to the fill. Fire-and-forget so
        #    the 30s sampling never blocks the executor loop; the task ref is
        #    held in _MARKOUT_TASKS so it is not GC'd mid-flight.
        _mk = asyncio.create_task(
            self._record_markout(decision.symbol, orders_placed[0]["order"], actual_fill)
        )
        _MARKOUT_TASKS.add(_mk)
        _mk.add_done_callback(_MARKOUT_TASKS.discard)

        # 0B: a sibling leg sent DONE but its deal never posted — the confirmed
        # leg(s) above are now logged, but the unconfirmed one leaves us in a
        # possibly-inconsistent state → alert + HALT-ONLY shutdown for review.
        if unconfirmed:
            await self._halt_on_unconfirmed(decision, unconfirmed)

    # ─────────────────────────────────────────────────────────────────
    # Markout telemetry (infra-only) — no strategy logic
    # ─────────────────────────────────────────────────────────────────

    async def _record_markout(self, symbol: str, ticket: int, fill_price: float) -> None:
        """
        Measure post-fill markout (adverse selection). After a confirmed
        DEAL_ENTRY_IN, sample the market mid at +100ms / +1s / +5s / +30s and
        publish MARKOUT_RECORDED; TradeLogger writes it onto the trade row (the
        same late-update-by-ticket pattern as break-even).

        Fire-and-forget: must never block execution nor raise into the caller,
        so the whole body is guarded. `fill_time_ms` is the markout clock origin
        (the fill-confirmation instant — it trails the true broker fill by the
        confirmation latency); every offset below is measured from this same
        origin so the series stays self-consistent. Offsets are honoured against
        a monotonic deadline so symbol_info_tick latency does not accumulate.
        """
        try:
            loop = asyncio.get_running_loop()
            t0   = loop.time()
            fill_time_ms = int(time.time() * 1000)
            marks: dict[str, Optional[float]] = {}
            for offset_sec, label in _MARKOUT_OFFSETS_SEC:
                delay = t0 + offset_sec - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                tick = await asyncio.to_thread(mt5.symbol_info_tick, symbol)
                marks[label] = self._tick_mid(tick)
            await bus.publish(Topics.MARKOUT_RECORDED, {
                "ticket":       ticket,
                "fill_time_ms": fill_time_ms,
                "fill_price":   fill_price,
                **marks,
            })
            summary = " ".join(
                f"{lbl}={'NA' if marks[lbl] is None else format(marks[lbl], '.2f')}"
                for _, lbl in _MARKOUT_OFFSETS_SEC
            )
            logger.info(f"📈 Markout recorded | #{ticket} | fill={fill_price:.2f} | {summary}")
        except Exception as e:
            logger.error(f"OrderExecutor: markout recording failed for #{ticket}: {e}")

    @staticmethod
    def _tick_mid(tick) -> Optional[float]:
        """Mid price from a tick; None if unavailable. Markout uses the mid so it
        reflects market move rather than the bid/ask spread."""
        if tick is None:
            return None
        bid = getattr(tick, "bid", 0.0) or 0.0
        ask = getattr(tick, "ask", 0.0) or 0.0
        if bid > 0 and ask > 0:
            return (bid + ask) / 2.0
        return bid or ask or None

    # ─────────────────────────────────────────────────────────────────
    # Fill confirmation (0B) — a 10009 DONE is not proof of a fill
    # ─────────────────────────────────────────────────────────────────

    async def _confirm_or_flag(
        self,
        res:        dict,
        leg:        str,
        lot:        float,
        tp:         float,
        unconfirmed: list[int],
    ) -> Optional[dict]:
        """
        Confirm a DONE market send against terminal deal history. On success,
        augment `res` with the authoritative fill price + deal ticket and the leg
        metadata, and return it. On confirmation timeout, record the ticket in
        `unconfirmed` (caller halts) and return None — never fabricate a fill.
        """
        confirmed = await self._confirm_deal(res["order"])
        if confirmed is None:
            logger.critical(
                f"OrderExecutor[market]: {leg} order #{res['order']} returned DONE but no "
                f"DEAL_ENTRY_IN posted within {CFG.execution.fill_confirm_timeout_sec:.0f}s "
                "— treating as UNCONFIRMED."
            )
            unconfirmed.append(res["order"])
            return None

        res["price"] = confirmed["price"]   # authoritative fill (was the send echo)
        res["deal"]  = confirmed["deal"]
        res.update({"leg": leg, "lot": lot, "tp": tp})
        return res

    async def _confirm_deal(self, position_ticket: int) -> Optional[dict]:
        """
        Poll deal history until the DEAL_ENTRY_IN deal for this position posts, or
        the fill-confirm timeout elapses. retcode 10009 (DONE) means MT5 accepted
        the request — NOT that the deal executed and posted. positions_get() can
        also lag immediately after a send, so we trust history, not positions.
        Returns {price, volume, commission, deal} on confirmation, else None.
        """
        timeout  = CFG.execution.fill_confirm_timeout_sec
        poll     = CFG.execution.fill_confirm_poll_sec
        deadline = asyncio.get_running_loop().time() + timeout

        while True:
            deals = await asyncio.to_thread(
                mt5.history_deals_get, position=position_ticket
            ) or []
            entry_in = next((d for d in deals if d.entry == mt5.DEAL_ENTRY_IN), None)
            if entry_in is not None:
                return {
                    "price":      entry_in.price,
                    "volume":     entry_in.volume,
                    "commission": getattr(entry_in, "commission", 0.0),
                    "deal":       entry_in.ticket,
                }
            if asyncio.get_running_loop().time() >= deadline:
                return None
            await asyncio.sleep(poll)

    async def _halt_on_unconfirmed(self, decision: TradeDecision, tickets: list[int]) -> None:
        """Discord alert + halt-only shutdown for one or more unconfirmed fills."""
        detail = ", ".join(f"#{t}" for t in tickets)
        msg = (
            f"{decision.direction} {decision.symbol}: order_send returned DONE but the "
            f"fill did not post to terminal history within "
            f"{CFG.execution.fill_confirm_timeout_sec:.0f}s (ticket(s) {detail})."
        )
        logger.critical(f"OrderExecutor[market]: UNCONFIRMED FILL — {msg} Halting.")
        await send_alert(
            "🚨 UNCONFIRMED FILL — halting",
            "A market order returned DONE (10009) but no deal posted to terminal "
            "history. Halting to avoid acting on phantom state — check the terminal.",
            fields=[{"name": "Detail", "value": msg[:1000], "inline": False}],
        )
        await request_shutdown(f"unconfirmed fill: {detail}", "OrderExecutor")

    @staticmethod
    def _resolve_market_filling_modes(sym_info: dict) -> list[int]:
        """
        Filling ladder for MARKET deals: IOC → FOK (RETURN is not valid for
        immediate market execution). Probed from the symbol's filling bitmask;
        defaults to IOC when the broker reports neither bit.
        """
        mask = sym_info.get("filling_mode", 0) or 0
        ladder: list[int] = []
        if mask & _SYMBOL_FILLING_IOC:
            ladder.append(mt5.ORDER_FILLING_IOC)
        if mask & _SYMBOL_FILLING_FOK:
            ladder.append(mt5.ORDER_FILLING_FOK)
        if not ladder:
            ladder.append(mt5.ORDER_FILLING_IOC)
        return ladder

    async def _send_market_with_retry(
        self,
        *,
        symbol:        str,
        order_type:    int,
        lot:           float,
        sl:            float,
        tp:            float,
        comment:       str,
        filling_modes: list[int],
    ) -> Optional[dict]:
        """
        Submit a MARKET deal (TRADE_ACTION_DEAL) at the current price with a
        deviation tolerance, refreshing the price on each attempt.

        - 10030 (unsupported filling) → advance the IOC→FOK ladder.
        - requote / price-off / invalid-price → refresh tick + retry.
        Returns {order, volume, price (ACTUAL fill), retcode} on DONE, else None.
        """
        deviation = CFG.execution.market_deviation_points
        fmode_idx = 0
        for attempt in range(1, CFG.pending.max_place_retries + 1):
            tick = await asyncio.to_thread(mt5.symbol_info_tick, symbol)
            if tick is None:
                logger.warning(
                    f"order_send(market) no tick (attempt {attempt}/"
                    f"{CFG.pending.max_place_retries}) for {symbol} — retrying."
                )
                await asyncio.sleep(_RETRY_DELAY)
                continue

            price        = tick.ask if order_type == mt5.ORDER_TYPE_BUY else tick.bid
            type_filling = filling_modes[min(fmode_idx, len(filling_modes) - 1)]
            request = {
                "action":       mt5.TRADE_ACTION_DEAL,
                "symbol":       symbol,
                "volume":       lot,
                "type":         order_type,
                "price":        price,
                "sl":           sl,
                "tp":           tp,
                "deviation":    deviation,
                "magic":        _ORDER_MAGIC,
                "comment":      comment,
                "type_time":    mt5.ORDER_TIME_GTC,
                "type_filling": type_filling,
            }

            result = await asyncio.to_thread(mt5.order_send, request)

            if result is None:
                logger.warning(
                    f"order_send(market) returned None (attempt {attempt}/"
                    f"{CFG.pending.max_place_retries}) for {symbol} — MT5 disconnected?"
                )
                await asyncio.sleep(_RETRY_DELAY)
                continue

            if result.retcode == mt5.TRADE_RETCODE_DONE:
                # result.price is the ACTUAL fill price for a market deal.
                return {"order": result.order, "volume": result.volume,
                        "price": result.price, "retcode": result.retcode}

            # Unsupported filling mode → advance the ladder, retry immediately.
            if result.retcode == mt5.TRADE_RETCODE_INVALID_FILL:
                if fmode_idx < len(filling_modes) - 1:
                    fmode_idx += 1
                    logger.warning(
                        f"market {comment}: filling mode unsupported (10030) — "
                        f"advancing ladder to {filling_modes[fmode_idx]}."
                    )
                    continue
                logger.error(f"market {comment}: all filling modes exhausted (10030) — skipping leg.")
                return None

            # Transient price retcodes → refresh tick + retry (no side concept for market).
            if result.retcode in _RETRYABLE_MARKET_RETCODES:
                logger.warning(
                    f"market {comment}: retcode {result.retcode} (attempt {attempt}/"
                    f"{CFG.pending.max_place_retries}) — refreshing price, retrying."
                )
                await asyncio.sleep(_RETRY_DELAY)
                continue

            # Any other retcode → log + bounded retry.
            logger.error(
                f"market order rejected | {comment} | lot={lot:.2f} | "
                f"retcode={result.retcode} | broker='{result.comment}' | "
                f"attempt {attempt}/{CFG.pending.max_place_retries}"
            )
            await asyncio.sleep(_RETRY_DELAY)

        return None


# ── Singleton ─────────────────────────────────────────────────────
order_executor = OrderExecutor()
