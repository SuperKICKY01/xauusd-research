"""
agents/risk_manager.py — Agent 3: RiskManager (Hard-Logic VETO Engine)

Role in the pipeline:
  SIGNAL_READY ──▶ RiskManager ──▶ TRADE_DECISION event
                       │
                       ├─ APPROVED → TRADE_DECISION(approved=True)
                       └─ VETOED   → TRADE_DECISION(approved=False)

As of 2026-06-05 the LLM gates (FastGate/CloudGate) were demoted OFF the M5
critical path. The deterministic SignalBuilder is now the sole approver: every
SignalPacket on SIGNAL_READY is wrapped in a synthetic APPROVE GateDecision and
fed straight into the existing veto + sizing logic (unchanged). The GateDecision
wrapper is preserved only so downstream .gate_decision consumers (order_executor,
trade_logger) keep their contract. See lessons/2026-06-05-llm-htf-demotion.md.

The RiskManager has ABSOLUTE VETO POWER over every trade.
It doesn't use AI — it uses hard arithmetic.
No LLM output can override these checks.

Veto Conditions (any single failure = NO TRADE):
  ─────────────────────────────────────────────────────────────────
  1. DAILY DRAWDOWN:    current equity DD% >= max_daily_drawdown_pct
  2b.DAILY DD CB:       projected daily DD% >= 3.5% (preemptive guard)
  2. TOTAL DRAWDOWN:    total equity DD% >= max_total_drawdown_pct
  3. CONCURRENT TRADES: open positions >= max_concurrent_trades
  4. LOT SIZE ZERO:     calculated lot rounds to 0.0 after sizing
  5. SL TOO WIDE:       SL distance (with slippage) > 3× ATR (runaway risk)
  6. SL TOO TIGHT:      SL distance < 0.3× ATR (almost certain SL hit from spread)
  7. MARGIN PREFLIGHT:  projected margin usage > 80% of free margin
  8. NEWS BLOCK:        NEWS_BLOCK_ACTIVE event received and not yet cleared
  9. DUPLICATE TRADE:   a position in the same direction already exists
  ─────────────────────────────────────────────────────────────────

Lot Sizing (also done here — central source of truth):
  lot = (balance × risk_pct) / (sl_distance_with_slippage × pip_value)
  pip_value for XAUUSD = contract_size × point (typically $0.01 per 0.01 lot)
  CRITICAL: sl_distance always includes CFG.risk.slippage_buffer_pips

Architecture:
  - Subscribes to: SIGNAL_READY, ACCOUNT_STATE, NEWS_BLOCK_ACTIVE,
                   NEWS_BLOCK_CLEAR, TICK_DATA
  - Publishes: TRADE_DECISION (approved or vetoed with full context)
  - Maintains internal state via latest_account_state and news_block_active flag
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import MetaTrader5 as mt5

from config import CFG
from core.event_bus import bus, Topics
from core.mt5_connector import connector
from agents.fast_gate import AgentDecision, GateDecision
from quant.signal_builder import SignalPacket

logger = logging.getLogger(__name__)

_last_rejection_reason: str   = ""
_last_rejection_time:   float = 0.0
_REJECTION_DEDUP_WINDOW       = 60.0

# ── VETO 2b: Daily DD circuit breaker threshold ─────────────────────
# Preempt trades whose worst-case loss would push projected daily DD
# past this percentage. Set 0.5pp below the FTMO 4% cap so that any
# single trade slipping past projection still leaves room before
# VETO 2 (the absolute backstop) fires. Module constant rather than
# RiskConfig field because it's tightly coupled to the 4% cap and not
# a tuning parameter — same pattern as `_REJECTION_DEDUP_WINDOW`.
_DAILY_DD_CIRCUIT_BREAKER_PCT = 3.5

# Direction mapping for concurrency grouping (VETO 4/5). Covers position
# types (BUY/SELL) and all pending order types so a resting limit is
# bucketed into the same slot as a filled position of the same direction.
_BUY_TYPES = {
    mt5.ORDER_TYPE_BUY, mt5.ORDER_TYPE_BUY_LIMIT,
    mt5.ORDER_TYPE_BUY_STOP, mt5.ORDER_TYPE_BUY_STOP_LIMIT,
}
_SELL_TYPES = {
    mt5.ORDER_TYPE_SELL, mt5.ORDER_TYPE_SELL_LIMIT,
    mt5.ORDER_TYPE_SELL_STOP, mt5.ORDER_TYPE_SELL_STOP_LIMIT,
}


def _should_publish_rejection(reason: str) -> bool:
    global _last_rejection_reason, _last_rejection_time
    now = time.time()
    if reason == _last_rejection_reason and (now - _last_rejection_time) < _REJECTION_DEDUP_WINDOW:
        return False
    _last_rejection_reason = reason
    _last_rejection_time   = now
    return True


# ─────────────────────────────────────────────────────────────────
# Trade Decision Dataclass
# ─────────────────────────────────────────────────────────────────

@dataclass
class TradeDecision:
    """
    Final output of the full agent pipeline.
    The execution engine (Phase 4) consumes this directly.
    """
    approved:       bool
    veto_reason:    str                     # Empty string if approved
    gate_decision:  GateDecision            # Synthetic APPROVE GateDecision wrapping the SignalPacket

    # Execution-ready parameters (only populated when approved=True)
    symbol:         str                     = ""
    direction:      str                     = ""    # "BUY" | "SELL"
    lot_size:       float                   = 0.0
    entry_price:    float                   = 0.0
    stop_loss:      float                   = 0.0
    take_profit_1:  float                   = 0.0
    take_profit_2:  float                   = 0.0
    sl_distance:    float                   = 0.0   # Including slippage buffer
    risk_amount:    float                   = 0.0   # In account currency
    rr_ratio:       float                   = 0.0
    timestamp:      datetime                = field(default_factory=datetime.now)

    # Risk snapshot at time of approval (for logging and RAG)
    account_balance:float                   = 0.0
    account_equity: float                   = 0.0
    daily_dd_pct:   float                   = 0.0

    def summary(self) -> str:
        if not self.approved:
            return f"VETOED | Reason: {self.veto_reason}"
        return (
            f"APPROVED | {self.direction} {self.symbol} | "
            f"Lots: {self.lot_size:.2f} | "
            f"Entry: {self.entry_price:.2f} | "
            f"SL: {self.stop_loss:.2f} | "
            f"TP1: {self.take_profit_1:.2f} | "
            f"TP2: {self.take_profit_2:.2f} | "
            f"Risk: ${self.risk_amount:.2f} | "
            f"RR: 1:{self.rr_ratio:.2f}"
        )


# ─────────────────────────────────────────────────────────────────
# RiskManager
# ─────────────────────────────────────────────────────────────────

class RiskManager:
    """
    Stateful risk manager. Maintains live account snapshot and news flag
    by subscribing to side-channel topics in parallel with trade evaluation.
    """

    def __init__(self) -> None:
        # Live state — updated by background listeners
        self._latest_account:      Optional[dict]  = None
        self._latest_tick:         Optional[dict]  = None
        self._news_block_active:   bool            = False

        # Internal config shortcuts
        self._risk_cfg  = CFG.risk
        self._sym_cfg   = CFG.symbol

        # Running peak equity (live high-water mark) for DD-modulated sizing.
        # Seeded with the configured starting balance; climbs as equity makes
        # new highs — refreshed in _account_listener on every account snapshot
        # and again in _evaluate_trade right before order_send. See CFG.dd_mod.
        self._peak_equity: float = self._risk_cfg.initial_balance

    # ─────────────────────────────────────────────────────────────
    # Startup — launches three concurrent tasks
    # ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Launch all three RiskManager coroutines concurrently."""
        logger.info("🛡️  RiskManager online — VETO engine armed.")
        await asyncio.gather(
            self._trade_evaluator(),
            self._account_listener(),
            self._news_listener(),
            return_exceptions=True,
        )

    # ─────────────────────────────────────────────────────────────
    # COROUTINE 1: Trade Evaluator (core veto logic)
    # ─────────────────────────────────────────────────────────────

    async def _trade_evaluator(self) -> None:
        """
        Subscribes to SIGNAL_READY (LLM gates demoted 2026-06-05).

        Every deterministic SignalPacket is wrapped in a synthetic APPROVE
        GateDecision and run through the hard veto checks + lot sizing. There is
        no upstream REJECT/SKIP to forward anymore — the only rejections are this
        manager's own vetoes.
        """
        queue = await bus.subscribe(Topics.SIGNAL_READY)
        logger.debug("RiskManager: trade_evaluator listening on SIGNAL_READY (deterministic path).")

        while True:
            try:
                signal: SignalPacket = await asyncio.wait_for(
                    queue.get(), timeout=60.0
                )

                # Wrap the deterministic signal in a synthetic APPROVE GateDecision.
                # adjusted_* stay None → _evaluate_trade falls back to the signal's
                # own suggested levels (its existing behaviour). agent="Deterministic"
                # makes trade_logger's was_escalated correctly False.
                gate = GateDecision(
                    decision   = AgentDecision.APPROVE,
                    confidence = 1.0,
                    reasoning  = "Deterministic SMC signal (no LLM gate).",
                    agent      = "Deterministic",
                    signal     = signal,
                )

                decision = await self._evaluate_trade(gate)
                await bus.publish(Topics.TRADE_DECISION, decision)

                if decision.approved:
                    logger.info(f"✅ RiskManager: {decision.summary()}")
                else:
                    logger.warning(
                        f"🛡️  RiskManager VETO: {decision.veto_reason}"
                    )
                    if _should_publish_rejection(decision.veto_reason):
                        await bus.publish(Topics.TRADE_REJECTED, {
                            "reason":    decision.veto_reason,
                            "agent":     "RiskManager",
                            "signal":    gate.signal,
                            "timestamp": datetime.now().isoformat(),
                        })

            except asyncio.TimeoutError:
                logger.debug("RiskManager: No SIGNAL_READY in 60s.")
            except Exception as e:
                logger.error(f"RiskManager trade evaluator error: {e}", exc_info=True)

    # ─────────────────────────────────────────────────────────────
    # COROUTINE 2: Account State Listener
    # ─────────────────────────────────────────────────────────────

    async def _account_listener(self) -> None:
        """
        Keeps self._latest_account up-to-date from the DataStream.
        This is used by the veto checks — always uses the freshest snapshot.
        """
        queue = await bus.subscribe(Topics.ACCOUNT_STATE)
        while True:
            try:
                self._latest_account = await asyncio.wait_for(
                    queue.get(), timeout=30.0
                )
                # Track the live high-water mark on every account snapshot
                # (≈ every tick at the 1s account poll) so the DD-modulated
                # sizing taper is measured from a CURRENT peak, never an
                # end-of-day value (Klass & Nowicki 2005). Peak only climbs.
                eq = self._latest_account.get("equity") if self._latest_account else None
                if eq is not None and eq > self._peak_equity:
                    self._peak_equity = eq
            except asyncio.TimeoutError:
                pass
            except Exception as e:
                logger.error(f"RiskManager account listener: {e}")

    # ─────────────────────────────────────────────────────────────
    # COROUTINE 3: News Block Listener
    # ─────────────────────────────────────────────────────────────

    async def _news_listener(self) -> None:
        """
        Toggles the news block flag based on NEWS_BLOCK_ACTIVE / NEWS_BLOCK_CLEAR.
        When active, ALL trades are vetoed regardless of signal quality.
        """
        block_q = await bus.subscribe(Topics.NEWS_BLOCK_ACTIVE)
        clear_q = await bus.subscribe(Topics.NEWS_BLOCK_CLEAR)

        while True:
            try:
                await asyncio.sleep(0.1)
                # Check both queues non-blocking
                for q, active in [(block_q, True), (clear_q, False)]:
                    try:
                        msg = q.get_nowait()
                        if self._news_block_active != active:
                            self._news_block_active = active
                            status = "BLOCKED 🚫" if active else "CLEAR ✅"
                            logger.info(
                                f"RiskManager: News window {status} | "
                                f"Details: {msg}"
                            )
                    except asyncio.QueueEmpty:
                        pass
            except Exception as e:
                logger.error(f"RiskManager news listener: {e}")

    # ─────────────────────────────────────────────────────────────
    # Core Veto + Sizing Logic
    # ─────────────────────────────────────────────────────────────

    async def _evaluate_trade(self, gate: GateDecision) -> TradeDecision:
        """
        Runs all veto checks. If any fail → returns TradeDecision(approved=False).
        If all pass → calculates lot size and returns TradeDecision(approved=True).
        """
        signal = gate.signal
        if signal is None:
            return self._veto(gate, "No signal payload attached to gate decision.")

        # Resolve levels: use agent's adjusted values if provided, else signal defaults
        entry  = gate.adjusted_sl and signal.suggested_entry or signal.suggested_entry
        sl_raw = gate.adjusted_sl  or signal.suggested_sl
        tp1    = gate.adjusted_tp1 or signal.suggested_tp1
        tp2    = gate.adjusted_tp2 or signal.suggested_tp2

        if None in (entry, sl_raw, tp1, tp2):
            return self._veto(gate, "Incomplete trade levels — entry/SL/TP missing.")

        # ── Get live account state ─────────────────────────────────
        account = self._latest_account
        if account is None:
            # Fallback: fetch directly from MT5
            account = await connector.get_account_info()
        if account is None:
            return self._veto(gate, "Cannot retrieve account state — MT5 unreachable.")

        balance      = account["balance"]
        equity       = account["equity"]
        free_margin  = account["free_margin"]
        daily_dd_pct = account.get("daily_dd_pct", 0.0)
        sod_equity   = account.get("start_of_day_equity", 0.0)

        # ── VETO 1: News Block ─────────────────────────────────────
        if self._news_block_active:
            return self._veto(gate, "High-impact news window is active — trading blocked.")

        # ── VETO 2: Daily Drawdown ─────────────────────────────────
        # Reactive backstop: fires after daily DD has already crossed
        # the FTMO cap. Pairs with VETO 2b below, which is the
        # preemptive guard that should catch breaches first.
        if daily_dd_pct >= self._risk_cfg.max_daily_drawdown_pct:
            return self._veto(
                gate,
                f"Daily drawdown {daily_dd_pct:.2f}% >= limit "
                f"{self._risk_cfg.max_daily_drawdown_pct}% — no new trades today."
            )

        # ── VETO 2b: Daily DD Circuit Breaker (preemptive) ─────────
        # VETO 2 is reactive — it fires AFTER daily DD crosses the 4%
        # cap. 2b is preemptive — it blocks any trade whose worst-case
        # loss (the configured per-trade risk amount) would push
        # projected daily DD past 3.5%, leaving 0.5pp safety margin
        # under the FTMO cap.
        #
        # Background: 2026-05-08 baseline backtest had 15 days where
        # the strategy would have exceeded the 4% daily DD cap on a
        # $100k FTMO account — each one is a challenge-failure event.
        # VETO 2 alone can't prevent these because it only fires after
        # the line is crossed. See lessons/2026-05-09-daily-dd-circuit-breaker.md.
        #
        # Live-only: VirtualBroker has no equivalent mirror because
        # backtest equity is recomputed per-bar from realised trades
        # one trade at a time, throttled by VETO 4 (max_concurrent).
        # Live needs the explicit projection because positions are
        # open and floating losses can cross the cap before the next
        # signal cycle fires. See virtual_broker.py for the brief note.
        if sod_equity > 0:
            daily_loss_so_far_dollars  = max(0.0, sod_equity - equity)
            risk_amount_for_projection = balance * (
                self._risk_cfg.max_risk_per_trade_pct / 100.0
            )
            projected_daily_dd_pct = (
                (daily_loss_so_far_dollars + risk_amount_for_projection)
                / sod_equity * 100.0
            )
            if projected_daily_dd_pct >= _DAILY_DD_CIRCUIT_BREAKER_PCT:
                return self._veto(
                    gate,
                    f"Daily DD circuit breaker: projected DD "
                    f"{projected_daily_dd_pct:.2f}% (current "
                    f"{daily_dd_pct:.2f}% + ${risk_amount_for_projection:.2f} "
                    f"new risk) >= {_DAILY_DD_CIRCUIT_BREAKER_PCT}% safety "
                    f"cap — preempting trade to preserve FTMO 4% headroom."
                )

        # ── VETO 3: Total Drawdown ─────────────────────────────────
        total_dd = max(0.0, (self._risk_cfg.initial_balance - equity) /
                       self._risk_cfg.initial_balance * 100)
        if total_dd >= self._risk_cfg.max_total_drawdown_pct:
            return self._veto(
                gate,
                f"Total drawdown {total_dd:.2f}% >= limit "
                f"{self._risk_cfg.max_total_drawdown_pct}% — account at risk."
            )

        # ── VETO 4: Concurrent Slots (positions + pending orders) ──
        # "One signal = one slot": a dual-TP signal opens 2 legs that share
        # ONE (symbol, direction) group and consume a single slot — whether
        # the legs are filled positions or still-resting pending limits.
        # Pending orders COUNT here so several pending fills can't overshoot
        # the concurrency cap (overlot guard). Counts the whole account,
        # consistent with pre-pending behaviour (dedicated bot account);
        # magic-level filtering lives in the recovery paths.
        positions = await asyncio.to_thread(mt5.positions_get)
        pending    = await asyncio.to_thread(mt5.orders_get)
        groups     = self._occupied_groups(positions, pending)
        if len(groups) >= self._risk_cfg.max_concurrent_trades:
            return self._veto(
                gate,
                f"{len(groups)} concurrent slot(s) occupied {sorted(groups)} "
                f"(positions + pending) — max is "
                f"{self._risk_cfg.max_concurrent_trades}."
            )

        # ── VETO 5: Duplicate Direction (positions + pending) ──────
        # Same-direction exposure already open OR resting as a pending limit
        # blocks a new same-direction signal — stops a second pending order
        # stacking on the same OB/direction before the first fills.
        direction = "BUY" if signal.bias.value == "LONG" else "SELL"
        if (signal.symbol, direction) in groups:
            return self._veto(
                gate,
                f"Duplicate {direction} exposure on {signal.symbol} — "
                f"already open or pending."
            )

        # ── Get Symbol Info for Lot Sizing ─────────────────────────
        sym_info = await connector.get_symbol_info(signal.symbol)
        if sym_info is None:
            return self._veto(gate, f"Cannot fetch symbol info for {signal.symbol}.")

        # ── Calculate SL Distance with Slippage Buffer ─────────────
        # CRITICAL LESSON: NEVER use raw SL distance for lot sizing.
        # Always add the slippage buffer before calculating lot size.
        point            = sym_info["point"]
        slippage_in_price = self._risk_cfg.slippage_buffer_pips * point * 10
        sl_distance_with_slippage = abs(entry - sl_raw) + slippage_in_price

        if sl_distance_with_slippage <= 0:
            return self._veto(gate, "SL distance after slippage buffer is zero or negative.")

        # ── VETO 6: SL Too Wide (> 3× ATR) ────────────────────────
        if signal.atr > 0 and sl_distance_with_slippage > signal.atr * 3.0:
            return self._veto(
                gate,
                f"SL distance {sl_distance_with_slippage:.2f} > 3× ATR "
                f"({signal.atr * 3:.2f}) — risk too large relative to volatility."
            )

        # ── VETO 7: SL Too Tight (< 0.3× ATR) ────────────────────
        if signal.atr > 0 and sl_distance_with_slippage < signal.atr * 0.3:
            return self._veto(
                gate,
                f"SL distance {sl_distance_with_slippage:.2f} < 0.3× ATR "
                f"({signal.atr * 0.3:.2f}) — will be hit by spread noise."
            )

        # ── DD-Modulated Position Sizing (Grossman-Zhou / Hsieh-Barmish 2017) ──
        # Refresh the running peak equity HERE, right before order_send, so it
        # is a live high-water mark and never an end-of-day value (Klass &
        # Nowicki 2005); _account_listener keeps it climbing between signals.
        # The multiplier ∈ [0, 1] (peak ≥ equity always), so it can only taper
        # risk DOWN in drawdown; at the peak it equals BaseRisk exactly.
        # equity ≤ ruin → 0% → explicit no-trade veto. Mirrored in
        # VirtualBroker._calculate_lot for backtest parity.
        if equity > self._peak_equity:
            self._peak_equity = equity
        effective_risk_pct = self._risk_cfg.max_risk_per_trade_pct
        if CFG.dd_mod.enabled:
            ruin_level = self._risk_cfg.initial_balance * CFG.dd_mod.ruin_level_frac
            denom      = self._peak_equity - ruin_level
            ratio      = ((equity - ruin_level) / denom) if denom > 0 else 0.0
            ratio      = max(0.0, min(1.0, ratio))
            effective_risk_pct = (
                self._risk_cfg.max_risk_per_trade_pct * (ratio ** CFG.dd_mod.gamma)
            )
            if effective_risk_pct <= 0.0:
                return self._veto(
                    gate,
                    f"DD-mod: equity ${equity:,.2f} <= ruin level "
                    f"${ruin_level:,.2f} (peak ${self._peak_equity:,.2f}) — "
                    f"risk tapered to 0%, no trade."
                )
            logger.debug(
                f"DD-mod: equity=${equity:,.2f} peak=${self._peak_equity:,.2f} "
                f"ruin=${ruin_level:,.2f} ratio={ratio:.4f} "
                f"risk%={self._risk_cfg.max_risk_per_trade_pct}→{effective_risk_pct:.4f}"
            )

        # ── Lot Size Calculation ───────────────────────────────────
        lot_size = self._calculate_lot_size(
            balance              = balance,
            sl_distance          = sl_distance_with_slippage,
            contract_size        = sym_info["contract_size"],
            point                = point,
            volume_min           = sym_info["volume_min"],
            volume_max           = sym_info["volume_max"],
            volume_step          = sym_info["volume_step"],
            risk_pct             = effective_risk_pct,
        )

        # ── VETO 8: Lot Size is Zero ───────────────────────────────
        if lot_size <= 0.0:
            return self._veto(
                gate,
                f"Calculated lot size rounds to {lot_size:.2f} — "
                "account too small or SL too wide for minimum lot."
            )

        # ── VETO 9: Margin Pre-flight ──────────────────────────────
        # Estimate margin required: (lot × contract_size × entry) / leverage
        # We use a simplified approximation; broker may vary slightly.
        leverage = account.get("leverage", 100)
        estimated_margin = (lot_size * sym_info["contract_size"] * entry) / leverage
        if estimated_margin > free_margin * 0.8:
            return self._veto(
                gate,
                f"Estimated margin ${estimated_margin:.2f} > 80% of free margin "
                f"${free_margin:.2f} — insufficient cushion."
            )

        # ── All Checks Passed ──────────────────────────────────────
        # Report the ACTUAL risked amount (DD-modulated when enabled) so the
        # TradeDecision snapshot and downstream RAG reflect what was sized.
        risk_amount = balance * (effective_risk_pct / 100)
        rr = round(abs(tp2 - entry) / sl_distance_with_slippage, 2) if sl_distance_with_slippage else 0.0

        return TradeDecision(
            approved        = True,
            veto_reason     = "",
            gate_decision   = gate,
            symbol          = signal.symbol,
            direction       = direction,
            lot_size        = lot_size,
            entry_price     = round(entry, 5),
            stop_loss       = round(sl_raw, 5),
            take_profit_1   = round(tp1, 5),
            take_profit_2   = round(tp2, 5),
            sl_distance     = round(sl_distance_with_slippage, 5),
            risk_amount     = round(risk_amount, 2),
            rr_ratio        = rr,
            account_balance = round(balance, 2),
            account_equity  = round(equity, 2),
            daily_dd_pct    = round(daily_dd_pct, 4),
        )

    # ─────────────────────────────────────────────────────────────
    # Lot Size Calculator
    # ─────────────────────────────────────────────────────────────

    def _calculate_lot_size(
        self,
        balance:       float,
        sl_distance:   float,   # In price units, WITH slippage buffer
        contract_size: float,   # e.g. 100 oz for XAUUSD
        point:         float,   # Smallest price move (e.g. 0.01 for XAUUSD)
        volume_min:    float,
        volume_max:    float,
        volume_step:   float,
        risk_pct:      Optional[float] = None,   # Effective % (DD-modulated); None → BaseRisk
    ) -> float:
        """
        Risk-based lot sizing.

        Formula:
          risk_amount    = balance × risk_pct / 100
          pip_value      = contract_size × point   (value of 1 point move per lot)
          lot_size       = risk_amount / (sl_distance / point × pip_value)
                        = risk_amount / (sl_distance × contract_size)

        Note: sl_distance must already include the slippage buffer (enforced upstream).

        The result is snapped to the broker's volume_step and clamped to
        [volume_min, volume_max].
        """
        if risk_pct is None:
            risk_pct = self._risk_cfg.max_risk_per_trade_pct
        risk_amount = balance * (risk_pct / 100.0)

        # Value at risk per lot = sl_distance × contract_size
        # (For XAUUSD: 1 lot = 100 oz, price move of $1 = $100 P&L)
        value_per_lot = sl_distance * contract_size

        if value_per_lot <= 0:
            logger.error(
                f"Lot calc: value_per_lot = {value_per_lot} "
                f"(sl_distance={sl_distance}, contract={contract_size})"
            )
            return 0.0

        raw_lot = risk_amount / value_per_lot

        # Snap to broker's volume step (e.g. 0.01 steps)
        if volume_step > 0:
            raw_lot = round(raw_lot / volume_step) * volume_step

        # Clamp to broker limits
        lot = max(volume_min, min(volume_max, raw_lot))
        lot = round(lot, 2)  # Final rounding for MT5 submission

        logger.debug(
            f"Lot sizing: balance=${balance:.2f}, risk%={risk_pct}, "
            f"sl_dist={sl_distance:.5f}, value/lot={value_per_lot:.2f}, "
            f"raw={raw_lot:.4f}, final={lot:.2f}"
        )

        return lot

    # ─────────────────────────────────────────────────────────────
    # Helper
    # ─────────────────────────────────────────────────────────────

    @staticmethod
    def _occupied_groups(positions, pending) -> set:
        """
        Collapse open positions AND pending orders into the set of
        (symbol, direction) concurrency groups. Each dual-TP signal's two
        legs share one group → one slot. Unknown types (e.g. CLOSE_BY) are
        ignored. See VETO 4/5 and references/veto-rules.md.
        """
        groups: set[tuple[str, str]] = set()
        for coll in (positions, pending):
            if not coll:
                continue
            for item in coll:
                if item.type in _BUY_TYPES:
                    groups.add((item.symbol, "BUY"))
                elif item.type in _SELL_TYPES:
                    groups.add((item.symbol, "SELL"))
        return groups

    @staticmethod
    def _veto(gate: GateDecision, reason: str) -> TradeDecision:
        """Convenience method to build a vetoed TradeDecision."""
        return TradeDecision(
            approved     = False,
            veto_reason  = reason,
            gate_decision= gate,
        )


# ── Singleton ─────────────────────────────────────────────────────
risk_manager = RiskManager()
