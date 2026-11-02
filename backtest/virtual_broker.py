"""
backtest/virtual_broker.py — In-memory broker simulator.

Mirrors the live execution stack:
  • Dual-TP order placement (TP1 leg + TP2 leg) like OrderExecutor.
  • Break-even SL move on TP2 leg once TP1 fills, like BreakEvenMonitor.
  • Risk-based lot sizing using the same formula RiskManager applies.

Every strategy signal opens up to two positions. Each candle, every open
position is checked against its SL/TP using the candle's high/low. When both
SL and TP fall within a single bar's range, we conservatively assume the SL
hit first — that's the pessimistic backtest convention.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class Position:
    ticket:       int
    leg:          str            # "TP1" | "TP2" | "SOLO"
    direction:    str            # "BUY" | "SELL"
    lot:          float
    entry_price:  float
    sl:           float
    tp:           float
    open_ts:      pd.Timestamp
    sibling_tp1:  Optional[int] = None   # ticket of TP1 leg (set on TP2 leg)
    moved_to_be:  bool = False           # TP2 leg's SL has been moved to BE
    closed:       bool = False
    close_ts:     Optional[pd.Timestamp] = None
    close_price:  Optional[float] = None
    outcome:      Optional[str]  = None  # WIN_TP1|WIN_TP2|LOSS|BE_HIT|TIMEOUT|TIME_STOP (unified vocab)
    pnl:          float = 0.0
    # One id per SIGNAL: the TP1+TP2 legs spawned from a single SignalPacket
    # share it. 0 = unassigned (never emitted by _spawn_legs; kept as the
    # sentinel so metrics can fall back to tuple-key grouping on old data).
    signal_id:    int = 0


@dataclass
class ClosedTrade:
    """
    One closed trade row. The strategy runner aggregates these for metrics.
    """
    ticket:       int
    leg:          str
    direction:    str
    bias:         str            # "LONG" | "SHORT"
    score:        int
    market_state: str
    atr:          float
    atr_mult:     float
    lot:          float
    entry_price:  float
    sl:           float
    tp:           float
    open_ts:      pd.Timestamp
    close_ts:     pd.Timestamp
    close_price:  float
    outcome:      str
    pnl:          float
    rr_realised:  float          # +1.5 / +2.5 on TP hits, -1 on SL, ~0 on BE
    # ── Confluence instrumentation (analysis only; does NOT affect scoring) ──
    # The exact boolean flag dict signal_builder computed for this setup, plus
    # the count of independent TIMING/zone confluences (CHoCH + FVG). Persisted
    # so the backtest report can be sliced by flag composition. See
    # _spawn_legs/_close for how packet.confluence_flags reaches here via _meta.
    flags:             dict = field(default_factory=dict)
    timing_flag_count: int  = 0
    # Signal identity: both legs of one dual-TP signal carry the same id, so
    # metrics can aggregate per SIGNAL (the statistically independent unit —
    # per-leg t-stats double-count correlated legs, inflating t up to ~sqrt(2)).
    signal_id:         int  = 0


@dataclass
class _PendingOrder:
    """A resting limit order in the backtest limit-fill model."""
    direction:   str            # "BUY" | "SELL"
    limit_price: float          # OB proximal edge
    sl:          float
    tp1:         float
    tp2:         float
    lot_tp1:     float
    lot_tp2:     float
    placed_ts:   pd.Timestamp
    expiry_ts:   pd.Timestamp   # end of next H1 (≤2h) — miss if unfilled by then
    signal_meta: dict


class VirtualBroker:
    """
    Stateful in-process broker. Not thread-safe; the strategy runner drives
    it sequentially through candles.
    """

    def __init__(
        self,
        initial_balance:  float,
        contract_size:    float = 100.0,   # XAUUSD: 100 oz / lot
        point:            float = 0.01,    # XAUUSD: 0.01 / point
        risk_pct:         float = 0.5,
        slippage_buffer_pips: float = 0.8,
        be_buffer_pips:   float = 1.0,
        max_hold_bars:    int   = 96,      # Force-close after 96 bars (4 days @ H1 / 8 hours @ M5)
        max_concurrent_trades: int = 2,    # Mirrors RiskManager VETO 4 — counts open legs
        bar_seconds:      int   = 300,     # Seconds per bar; derived from entry-TF data by StrategyRunner
        commission_per_lot_rt: float = 0.0,  # $/lot round-turn; 0 = cost-free (legacy baseline)
        swap_long_per_lot:     float = 0.0,  # $/lot/night carrying long  (negative = cost)
        swap_short_per_lot:    float = 0.0,  # $/lot/night carrying short (negative = cost)
        swap_triple_day:       int   = 2,    # Weekday charged 3× swap (Python weekday: Wed=2)
        dd_mod_enabled:        bool  = False, # DD-modulated sizing; False = legacy baseline
        dd_mod_ruin_frac:      float = 0.90,  # ruin = initial_balance × this (FTMO 10% DD)
        dd_mod_gamma:          float = 2.0,   # taper exponent (quadratic)
        time_stop_enabled:     bool  = False, # flatten at market if TP1 not hit within time_stop_bars
        time_stop_bars:        int   = 0,     # 45 min on M5 = 9 bars; 0 = disabled
    ) -> None:
        self.balance:          float          = initial_balance
        self.equity:           float          = initial_balance
        self.contract_size:    float          = contract_size
        self.point:             float         = point
        self.risk_pct:          float         = risk_pct
        self.slippage_buffer_pips: float      = slippage_buffer_pips
        self.be_buffer_pips:   float          = be_buffer_pips
        self.max_hold_bars:    int            = max_hold_bars
        self.max_concurrent_trades: int       = max_concurrent_trades
        self._bar_seconds:     int            = max(1, bar_seconds)

        # ── Transaction costs (XM Global). All default to 0 so a broker
        #    built without explicit cost args stays cost-free (legacy
        #    baseline). StrategyRunner injects the live CFG.costs values
        #    when apply_costs=True. See lessons/2026-05-30-transaction-costs.md.
        self.commission_per_lot_rt: float     = commission_per_lot_rt
        self.swap_long_per_lot:     float     = swap_long_per_lot
        self.swap_short_per_lot:    float     = swap_short_per_lot
        self.swap_triple_day:       int       = swap_triple_day

        # ── DD-Modulated Position Sizing (mirrors RiskManager). The peak is the
        #    running high-water mark, refreshed every step() (the backtest's
        #    per-bar "tick") before the next sizing call — matching the live
        #    "update peak before order_send" rule (Klass & Nowicki 2005).
        self.dd_mod_enabled:        bool      = dd_mod_enabled
        self.dd_mod_gamma:          float     = dd_mod_gamma
        self._ruin_level:           float     = initial_balance * dd_mod_ruin_frac
        self._peak_equity:          float     = initial_balance

        # ── Time-Based Stop (mirrors live BreakEvenMonitor). A trade that has
        #    not reached TP1 within time_stop_bars of fill is flattened at
        #    market (TIME_STOP). Break-even takes precedence: a leg whose trade
        #    reached TP1 (TP2 moved_to_be) is exempt. SOLO legs have no TP1 and
        #    are never time-stopped — matching BreakEvenMonitor (skips SOLO).
        self.time_stop_enabled:     bool      = time_stop_enabled
        self.time_stop_bars:        int       = time_stop_bars

        # Running totals of dollars paid (>= 0), surfaced by metrics.
        self.total_commission_paid: float     = 0.0
        self.total_swap_paid:       float     = 0.0

        self._next_ticket:    int             = 1
        self._next_signal_id: int             = 1
        self._open:           list[Position]  = []
        self.closed:          list[ClosedTrade] = []

        # Limit fill model (no-op under the market model — stays empty).
        self._pending:        list            = []   # list[_PendingOrder]
        self.pending_placed:  int             = 0
        self.pending_filled:  int             = 0
        self.pending_expired: int             = 0     # unfilled at expiry = missed trade

        # Mirrors of RiskManager VETOs we apply at signal-fill time.
        # Surfaced by the strategy runner so users can confirm parity with live.
        self.vetoes: dict[str, int] = {
            "max_concurrent":      0,
            "duplicate_direction": 0,
        }

        # One-trade-per-OB ledger. Keyed by `(direction, top, bottom)` —
        # the OB's price-anchored identity, which is stable across the
        # per-cycle OB list rebuilds in find_order_blocks. StrategyRunner
        # shares this set as a reference into signal_builder._traded_obs
        # so SMCAnalysis.nearest_*_ob() can filter against it next cycle.
        self.traded_obs: set[tuple[str, float, float]] = set()

        # Equity curve sampled per candle for drawdown analysis.
        self.equity_curve:    list[tuple[pd.Timestamp, float]] = []

    # ─────────────────────────────────────────────────────────────────
    # Sizing
    # ─────────────────────────────────────────────────────────────────

    def _calculate_lot(self, sl_distance_with_slip: float) -> float:
        """
        Risk-based lot sizing — same formula as RiskManager._calculate_lot_size,
        including the DD-modulated risk taper when enabled. Snapped to XAUUSD's
        typical 0.01 step and clamped to [0.01, 100].
        """
        if sl_distance_with_slip <= 0:
            return 0.0

        # ── DD-Modulated risk % (mirrors RiskManager._evaluate_trade) ──
        # Multiplier ∈ [0, 1]: tapers risk DOWN as equity falls from the
        # running peak toward ruin. equity ≤ ruin → 0% → no trade (return 0,
        # which the callers treat as "skip signal"). Peak is refreshed from
        # current equity here so it is current at the sizing call.
        risk_pct = self.risk_pct
        if self.dd_mod_enabled:
            if self.equity > self._peak_equity:
                self._peak_equity = self.equity
            denom = self._peak_equity - self._ruin_level
            ratio = ((self.equity - self._ruin_level) / denom) if denom > 0 else 0.0
            ratio = max(0.0, min(1.0, ratio))
            risk_pct = self.risk_pct * (ratio ** self.dd_mod_gamma)
            if risk_pct <= 0.0:
                return 0.0

        risk_amount   = self.balance * (risk_pct / 100.0)
        value_per_lot = sl_distance_with_slip * self.contract_size
        if value_per_lot <= 0:
            return 0.0
        raw = risk_amount / value_per_lot
        snapped = round(raw / 0.01) * 0.01
        return round(max(0.01, min(100.0, snapped)), 2)

    # ─────────────────────────────────────────────────────────────────
    # Order Submission
    # ─────────────────────────────────────────────────────────────────

    def open_dual_tp(
        self,
        ts:           pd.Timestamp,
        direction:    str,
        entry_price:  float,
        sl:           float,
        tp1:          float,
        tp2:          float,
        signal_meta:  dict,
    ) -> list[Position]:
        """
        Open two market orders (TP1 + TP2 legs). Returns the legs that
        were actually opened. If lot sizing falls below the minimum, no
        position is opened at all.

        Mirrors RiskManager VETO 4 (max_concurrent_trades) and VETO 5
        (duplicate direction) — these are the same hard checks the live
        system applies before any order leaves the agent pipeline. Without
        them, the backtester would stack 30+ legs on the same OB during
        consecutive bars and badly overstate the strategy edge.

        VETO 2b (live daily-DD circuit breaker) is intentionally NOT
        mirrored here. Backtest equity is recomputed per-bar from
        realised trades — there are no floating losses to project
        against — and signal flow is throttled by VETO 4 one trade
        at a time, so a runaway sequence is naturally bounded.
        Live needs the explicit projection because positions are open
        and floating losses can cross the 4% cap before the next
        signal cycle fires. See agents/risk_manager.py VETO 2b.
        """
        # ── VETO 4: concurrent trades ──────────────────────────────
        # Count open legs (live MT5 positions_get returns one row per leg,
        # so a dual-TP trade consumes 2 of the slot budget).
        open_legs = sum(1 for p in self._open if not p.closed)
        if open_legs >= self.max_concurrent_trades:
            self.vetoes["max_concurrent"] += 1
            logger.debug(
                f"VirtualBroker VETO: {open_legs} open legs >= max "
                f"{self.max_concurrent_trades} at {ts} — signal dropped."
            )
            return []

        # ── VETO 5: duplicate direction ────────────────────────────
        if any(p.direction == direction for p in self._open if not p.closed):
            self.vetoes["duplicate_direction"] += 1
            logger.debug(
                f"VirtualBroker VETO: {direction} position already open "
                f"at {ts} — duplicate signal dropped."
            )
            return []

        # ── One-trade-per-OB: mark the OB before constructing legs.
        # Done *after* the VETOes (which can legitimately defer a re-fire
        # until slots free up) but *before* leg construction (so a
        # degenerate lot-size of 0 still counts as "decided on" and won't
        # respawn the same setup next bar). See lesson 2026-05-06 step (d).
        ob_identity = signal_meta.get("ob_identity")
        if ob_identity is not None:
            self.traded_obs.add(ob_identity)

        slip_in_price = self.slippage_buffer_pips * self.point * 10
        sl_distance   = abs(entry_price - sl) + slip_in_price
        if sl_distance <= 0:
            return []

        total_lot = self._calculate_lot(sl_distance)
        if total_lot <= 0:
            logger.debug(f"VirtualBroker: lot size 0 — skipping signal at {ts}.")
            return []

        # Split (matches execution.lot_calculator.split_lot logic).
        half = round(total_lot / 2 / 0.01) * 0.01
        if half >= 0.01:
            lot_tp1, lot_tp2 = round(half, 2), round(total_lot - half, 2)
        else:
            lot_tp1, lot_tp2 = total_lot, 0.0

        return self._spawn_legs(ts, direction, entry_price, sl, tp1, tp2,
                                lot_tp1, lot_tp2, signal_meta)

    def _spawn_legs(
        self, ts, direction, entry_price, sl, tp1, tp2,
        lot_tp1, lot_tp2, signal_meta,
    ) -> list[Position]:
        """
        Construct and register the TP1/TP2 (or SOLO) position legs at a
        resolved fill price. Shared by the market path (open_dual_tp) and the
        limit-fill path (_process_pending).

        Every call = exactly one SIGNAL: mint one signal_id here and stamp it
        on all legs, so downstream stats can group correlated legs back into
        their independent unit.
        """
        legs: list[Position] = []
        signal_id = self._next_signal_id
        self._next_signal_id += 1

        tp1_pos = Position(
            ticket      = self._issue_ticket(),
            leg         = "TP1" if lot_tp2 > 0 else "SOLO",
            direction   = direction,
            lot         = lot_tp1,
            entry_price = entry_price,
            sl          = sl,
            tp          = tp1,
            open_ts     = ts,
            signal_id   = signal_id,
        )
        # Stash signal metadata on the position so we can carry it into the
        # closed trade record without a separate lookup.
        tp1_pos._meta = dict(signal_meta)  # type: ignore[attr-defined]
        self._charge_open_commission(tp1_pos)
        self._open.append(tp1_pos)
        legs.append(tp1_pos)

        if lot_tp2 > 0:
            tp2_pos = Position(
                ticket      = self._issue_ticket(),
                leg         = "TP2",
                direction   = direction,
                lot         = lot_tp2,
                entry_price = entry_price,
                sl          = sl,
                tp          = tp2,
                open_ts     = ts,
                sibling_tp1 = tp1_pos.ticket,
                signal_id   = signal_id,
            )
            tp2_pos._meta = dict(signal_meta)  # type: ignore[attr-defined]
            self._charge_open_commission(tp2_pos)
            self._open.append(tp2_pos)
            legs.append(tp2_pos)

        return legs

    def _charge_open_commission(self, pos: Position) -> None:
        """
        Charge the OPEN half of the round-turn commission ($3.50/lot of the
        $7 round-turn) the moment a leg is opened — like the live broker
        debiting commission on fill. Reduces balance immediately so
        subsequent lot sizing and the equity curve see it; the matching
        close half is taken in _close_position. The amount is stashed on the
        position so the closed-trade PnL nets BOTH halves.
        """
        comm = round(pos.lot * self.commission_per_lot_rt / 2.0, 2)
        pos._open_comm = comm                # type: ignore[attr-defined]
        self.balance -= comm
        self.total_commission_paid += comm

    def _issue_ticket(self) -> int:
        t = self._next_ticket
        self._next_ticket += 1
        return t

    # ─────────────────────────────────────────────────────────────────
    # Limit Fill Model — pending placement (req 1-4, 12)
    # ─────────────────────────────────────────────────────────────────

    def _occupied_directions(self) -> set:
        """
        Distinct direction groups across open positions AND resting pending
        orders. One dual-TP signal = 2 legs of one direction = ONE slot.
        Mirrors the live group-based VETO 4 (single-symbol backtest).
        """
        dirs = {p.direction for p in self._open if not p.closed}
        dirs |= {po.direction for po in self._pending}
        return dirs

    def place_pending_limit(
        self, ts, direction, limit_price, sl, tp1, tp2, expiry_ts, signal_meta,
    ):
        """
        Rest a limit order at the OB proximal edge (limit fill model). Applies
        the NEW group-based VETO 4 + duplicate-direction VETO 5 across BOTH
        open positions and pending orders, so pending fills can't overshoot
        the concurrency cap. Returns the _PendingOrder, or None if vetoed/sized-out.
        """
        occupied = self._occupied_directions()
        # VETO 4 — adding a NEW direction beyond the slot cap.
        if len(occupied) >= self.max_concurrent_trades and direction not in occupied:
            self.vetoes["max_concurrent"] += 1
            return None
        # VETO 5 — duplicate direction (open or already pending).
        if direction in occupied:
            self.vetoes["duplicate_direction"] += 1
            return None

        # One-trade-per-OB: mark before sizing (mirrors open_dual_tp).
        ob_identity = signal_meta.get("ob_identity")
        if ob_identity is not None:
            self.traded_obs.add(ob_identity)

        slip = self.slippage_buffer_pips * self.point * 10
        sl_distance = abs(limit_price - sl) + slip
        if sl_distance <= 0:
            return None
        total_lot = self._calculate_lot(sl_distance)
        if total_lot <= 0:
            return None
        half = round(total_lot / 2 / 0.01) * 0.01
        if half >= 0.01:
            lot_tp1, lot_tp2 = round(half, 2), round(total_lot - half, 2)
        else:
            lot_tp1, lot_tp2 = total_lot, 0.0

        po = _PendingOrder(
            direction=direction, limit_price=limit_price, sl=sl, tp1=tp1, tp2=tp2,
            lot_tp1=lot_tp1, lot_tp2=lot_tp2, placed_ts=ts, expiry_ts=expiry_ts,
            signal_meta=dict(signal_meta),
        )
        self._pending.append(po)
        self.pending_placed += 1
        return po

    def _process_pending(self, ts: pd.Timestamp, low: float, high: float) -> None:
        """
        Limit-fill resolution for resting pending orders:
          • ts past expiry          → EXPIRE (missed trade); release the OB
            lock so the still-valid OB can re-arm (mirrors live ORDER_CANCELLED).
          • bar range touches limit → FILL at the limit price (no slippage),
            spawning the TP1/TP2 legs via the shared _spawn_legs path.
        (OB-broken / H1-reversal cancellation is intentionally NOT modelled
        here — those only remove dead-thesis trades before fill, so omitting
        them makes the limit backtest a CONSERVATIVE lower bound on PF.)
        """
        still: list = []
        for po in self._pending:
            if ts > po.expiry_ts:
                self.pending_expired += 1
                ob_id = po.signal_meta.get("ob_identity")
                if ob_id is not None:
                    self.traded_obs.discard(ob_id)
                continue
            if low <= po.limit_price <= high:
                self.pending_filled += 1
                self._spawn_legs(ts, po.direction, po.limit_price, po.sl,
                                 po.tp1, po.tp2, po.lot_tp1, po.lot_tp2, po.signal_meta)
                continue
            still.append(po)
        self._pending = still

    # ─────────────────────────────────────────────────────────────────
    # Per-Candle Step
    # ─────────────────────────────────────────────────────────────────

    def step(self, ts: pd.Timestamp, candle: pd.Series) -> None:
        """
        Advance one bar. Resolves SL/TP/BE/timeout on every open position,
        applies BE-on-TP1 logic, updates balance + equity curve.
        """
        high  = float(candle["High"])
        low   = float(candle["Low"])
        close = float(candle["Close"])

        # O(1) break-even scan: positions closed during THIS step() are appended
        # to self.closed by _close_position below. Record the pre-step length so
        # the break-even pass scans only this bar's tail, not the whole (growing)
        # ledger — the old `for t in self.closed` made step() O(bars × trades).
        # ts strictly increases per step, so every tail item has close_ts == ts;
        # results are byte-identical.
        _closed_mark = len(self.closed)

        # ── Limit fill model: resolve resting pending orders FIRST ──────
        # No-op under the market model (self._pending stays empty). A pending
        # FILLS when this bar's range touches the limit (filled AT the limit —
        # no slippage, req 6); EXPIRES (missed) once ts passes its
        # end-of-next-H1 stamp.
        if self._pending:
            self._process_pending(ts, low, high)

        # Snapshot before mutation so we can move TP2 SLs based on what TP1
        # did during *this* bar.
        before_open_tickets = {p.ticket for p in self._open if not p.closed}

        for pos in list(self._open):
            if pos.closed:
                continue

            # ── Force-close on hold-time exhaustion ─────────────────
            bars_held = self._bars_between(pos.open_ts, ts)
            if bars_held > self.max_hold_bars:
                self._close_position(pos, ts, close, "TIMEOUT")
                continue

            # ── SL / TP resolution within the bar's range ───────────
            sl_hit = (low  <= pos.sl <= high)
            tp_hit = (low  <= pos.tp <= high)

            if pos.direction == "BUY":
                # Gap-down through SL on the open?
                if float(candle["Open"]) <= pos.sl:
                    self._close_position(pos, ts, float(candle["Open"]), "LOSS")
                    continue
                # Gap-up through TP on the open?
                if float(candle["Open"]) >= pos.tp:
                    self._close_position(pos, ts, float(candle["Open"]),
                                         "WIN_TP2" if pos.leg == "TP2" else "WIN_TP1")
                    continue
            else:  # SELL
                if float(candle["Open"]) >= pos.sl:
                    self._close_position(pos, ts, float(candle["Open"]), "LOSS")
                    continue
                if float(candle["Open"]) <= pos.tp:
                    self._close_position(pos, ts, float(candle["Open"]),
                                         "WIN_TP2" if pos.leg == "TP2" else "WIN_TP1")
                    continue

            # Both touched within this bar → conservatively assume SL first.
            if sl_hit and tp_hit:
                outcome = "BE_HIT" if pos.moved_to_be else "LOSS"
                self._close_position(pos, ts, pos.sl, outcome)
                continue

            if sl_hit:
                outcome = "BE_HIT" if pos.moved_to_be else "LOSS"
                self._close_position(pos, ts, pos.sl, outcome)
                continue

            if tp_hit:
                self._close_position(pos, ts, pos.tp,
                                     "WIN_TP2" if pos.leg == "TP2" else "WIN_TP1")
                continue

        # ── Apply break-even logic ──────────────────────────────────
        # If a TP1 leg closed this bar with TP_HIT, move its TP2 sibling's
        # SL to entry ± buffer.
        closed_this_bar = [
            t for t in self.closed[_closed_mark:]
            if t.close_ts == ts and t.leg == "TP1" and t.outcome == "WIN_TP1"
        ]
        for tp1_trade in closed_this_bar:
            self._move_sibling_to_be(tp1_trade)

        # Also check the price-based BE trigger (TP1 leg may still be open
        # but live price already passed the TP1 level favourably this bar).
        for pos in self._open:
            if pos.closed or pos.leg != "TP2" or pos.moved_to_be:
                continue
            tp1_price = pos._meta.get("tp1_price")  # type: ignore[attr-defined]
            if tp1_price is None:
                continue
            crossed = (
                (pos.direction == "BUY"  and high >= tp1_price) or
                (pos.direction == "SELL" and low  <= tp1_price)
            )
            if crossed:
                self._set_be_sl(pos)

        # ── Time-based stop ──────────────────────────────────────────
        # Mirror of the live time-stop (BreakEvenMonitor): if a trade has not
        # reached TP1 within time_stop_bars of its fill, flatten it at market
        # (this bar's close). Runs AFTER the break-even logic above so a trade
        # that reached TP1 *this* bar (its TP2 leg is now moved_to_be) is
        # exempt — BE precedence, identical to live. SOLO legs have no TP1 and
        # are not managed here, matching BreakEvenMonitor.
        if self.time_stop_enabled and self.time_stop_bars > 0:
            for pos in list(self._open):
                if pos.closed or pos.leg == "SOLO":
                    continue
                if pos.leg == "TP2" and pos.moved_to_be:
                    continue   # TP1 reached → trade is past the time-stop's scope
                if self._bars_between(pos.open_ts, ts) >= self.time_stop_bars:
                    self._close_position(pos, ts, close, "TIME_STOP")

        # ── Equity sample ────────────────────────────────────────────
        unrealised = self._unrealised_pnl(close)
        self.equity = self.balance + unrealised
        self.equity_curve.append((ts, self.equity))

        # Running peak equity (high-water mark) for DD-modulated sizing —
        # updated every bar before the next sizing call, mirroring the live
        # "update peak before order_send" rule. Peak only ever climbs.
        if self.equity > self._peak_equity:
            self._peak_equity = self.equity

    # ─────────────────────────────────────────────────────────────────
    # Internals
    # ─────────────────────────────────────────────────────────────────

    def _bars_between(self, a: pd.Timestamp, b: pd.Timestamp) -> int:
        return max(0, int((b - a).total_seconds() // self._bar_seconds))

    def _swap_cost(
        self,
        direction: str,
        lot:       float,
        open_ts:   pd.Timestamp,
        close_ts:  pd.Timestamp,
    ) -> float:
        """
        Overnight swap PnL (<= 0 cost) for a position held across one or more
        22:00 server-time rollovers. Convention (XM Global / standard metals):

          • A rollover at 22:00 is charged if the position was open through it,
            i.e. open_ts < rollover <= close_ts.
          • Wednesday (swap_triple_day) is charged 3× to pre-pay the weekend.
          • Saturday/Sunday rollovers are NOT charged separately — the
            Wednesday triple already accounts for them (gold doesn't trade
            the weekend anyway).

        Rates are per-lot-per-night from config (negative = cost). The 22:00
        boundary is read off the CSV's native timestamp clock, which is assumed
        to be broker-server time (XM = EET). Returns the signed PnL impact.
        """
        rate = self.swap_long_per_lot if direction == "BUY" else self.swap_short_per_lot
        if rate == 0.0 or lot <= 0:
            return 0.0

        # First 22:00 boundary strictly after open_ts.
        roll = open_ts.normalize() + pd.Timedelta(hours=22)
        if roll <= open_ts:
            roll += pd.Timedelta(days=1)

        total = 0.0
        while roll <= close_ts:
            wd = roll.weekday()            # Mon=0 … Sun=6
            if wd not in (5, 6):           # skip Sat/Sun rollovers
                mult = 3 if wd == self.swap_triple_day else 1
                total += rate * lot * mult
            roll += pd.Timedelta(days=1)
        return round(total, 2)

    def _move_sibling_to_be(self, tp1_trade: ClosedTrade) -> None:
        """When the TP1 leg closes profitably, lift the TP2 sibling's SL."""
        for pos in self._open:
            if pos.closed or pos.leg != "TP2" or pos.moved_to_be:
                continue
            if pos.sibling_tp1 != tp1_trade.ticket:
                continue
            self._set_be_sl(pos)
            break

    def _set_be_sl(self, pos: Position) -> None:
        buf = self.be_buffer_pips * self.point * 10
        new_sl = pos.entry_price + buf if pos.direction == "BUY" else pos.entry_price - buf
        pos.sl = round(new_sl, 5)
        pos.moved_to_be = True
        logger.debug(
            f"VirtualBroker: BE applied to #{pos.ticket} "
            f"({pos.direction}) — SL → {pos.sl:.2f}"
        )

    def _close_position(
        self,
        pos:         Position,
        ts:          pd.Timestamp,
        close_price: float,
        outcome:     str,
    ) -> None:
        pos.closed       = True
        pos.close_ts     = ts
        pos.close_price  = close_price
        pos.outcome      = outcome

        # Gross PnL = (close - entry) * lot * contract_size for BUY, inverted for SELL.
        direction_mult = 1.0 if pos.direction == "BUY" else -1.0
        gross_pnl = (close_price - pos.entry_price) * direction_mult * pos.lot * self.contract_size

        # ── Transaction costs ──────────────────────────────────────
        # comm_open was already debited from balance on open; the close half
        # is taken now. Swap is the overnight carry for every 22:00 rollover
        # the position was held through (<= 0). The closed-trade PnL records
        # the FULL net (both commission halves + swap) so PF/expectancy are
        # cost-aware, but balance only moves by the close-side delta here.
        comm_open  = getattr(pos, "_open_comm", 0.0)
        comm_close = round(pos.lot * self.commission_per_lot_rt / 2.0, 2)
        swap_pnl   = self._swap_cost(pos.direction, pos.lot, pos.open_ts, ts)
        self.total_commission_paid += comm_close
        self.total_swap_paid       += -swap_pnl

        pos.pnl = gross_pnl - comm_open - comm_close + swap_pnl
        self.balance += gross_pnl - comm_close + swap_pnl

        # Realised R multiple for stats.
        sl_distance = abs(pos.entry_price - pos._meta.get("sl_initial", pos.sl))  # type: ignore[attr-defined]
        if sl_distance <= 0:
            rr = 0.0
        else:
            rr = ((close_price - pos.entry_price) * direction_mult) / sl_distance

        meta = pos._meta  # type: ignore[attr-defined]
        # Confluence flag composition (analysis instrumentation only). The
        # timing/zone confluences are the CHoCH and FVG flags — count the ones
        # that fired (direction-gated True in signal_builder). Key substrings
        # are direction-agnostic ("Bullish CHoCH …" / "Bearish CHoCH …").
        flags = dict(meta.get("flags", {}) or {})
        timing_flag_count = sum(
            1 for k, v in flags.items()
            if v and ("CHoCH" in k or "FVG" in k)
        )
        self.closed.append(ClosedTrade(
            ticket       = pos.ticket,
            leg          = pos.leg,
            direction    = pos.direction,
            bias         = meta.get("bias",  ""),
            score        = int(meta.get("score", 0)),
            market_state = meta.get("market_state", ""),
            atr          = float(meta.get("atr", 0.0)),
            atr_mult     = float(meta.get("atr_mult", 1.0)),
            lot          = pos.lot,
            entry_price  = pos.entry_price,
            sl           = meta.get("sl_initial", pos.sl),
            tp           = pos.tp,
            open_ts      = pos.open_ts,
            close_ts     = ts,
            close_price  = close_price,
            outcome      = outcome,
            pnl          = round(pos.pnl, 2),
            rr_realised  = round(rr, 3),
            flags             = flags,
            timing_flag_count = timing_flag_count,
            signal_id         = pos.signal_id,
        ))

        # Drop from open list.
        self._open = [p for p in self._open if not p.closed]

    def _unrealised_pnl(self, close_price: float) -> float:
        total = 0.0
        for pos in self._open:
            if pos.closed:
                continue
            mult = 1.0 if pos.direction == "BUY" else -1.0
            total += (close_price - pos.entry_price) * mult * pos.lot * self.contract_size
        return total

    @property
    def has_open(self) -> bool:
        return any(not p.closed for p in self._open)
