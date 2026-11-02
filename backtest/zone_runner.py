"""
backtest/zone_runner.py — M5 replay loop for the frozen zone strategy.

Implements specs/phase1_zone_spec_v1.md §1.1.4–§1.1.8 on top of the
Phase-0-verified VirtualBroker (market path: open_dual_tp). Deliberately
separate from strategy_runner.py — the SMC path stays untouched.

Harness-v2 compliance is structural: the H1 feed pointer advances only when
a bar's CLOSE time (open + 60 min) ≤ the decision time (entry-bar close), so
the zone engine can never see a forming bar (anti-pattern #9). An assert
guards the invariant on every feed.

Frozen wiring (spec §1.1.5–§1.1.7 — deviations from broker defaults are
spec-conformance, not tuning):
  • be_buffer_pips = 0.0     → BE moves TP2's SL to the exact fill price.
  • max_hold_bars  = huge    → the spec declares NO time-based exit
    (SL / TP1 / TP2 / BE are exhaustive). The broker's legacy 96-bar
    TIMEOUT is disarmed. End-of-replay flush still closes stragglers
    (boundary artifact, same as the legacy runner).
  • time_stop / dd_mod OFF (hard-coded, not read from CFG/.env).
  • max_concurrent_trades = 2 legs = 1 signal.
  • Fill = next M5 open ± 0.25 adverse slippage (frozen infra constant,
    same as the legacy market path); sizing buffer 2.5 pips (anti-pattern #4).
"""

import logging
import time
from typing import Optional

import pandas as pd

from config import CFG
from backtest.data_loader import resample, tf_minutes
from backtest.virtual_broker import VirtualBroker
from quant.zone_engine import ZoneEngine

logger = logging.getLogger(__name__)

# Frozen infrastructure constants (spec §1.1.7 / Appendix A category C).
_FILL_SLIPPAGE_PRICE   = 0.25   # adverse entry slippage, price units
_SIZING_BUFFER_PIPS    = 2.5    # slippage buffer inside lot sizing (= 0.25)
_DEGENERATE_MIN_R      = 0.05   # |fill − SL| floor, price units (spec §1.1.4)
_NEVER_TIMEOUT_BARS    = 10**9  # disarms the legacy 96-bar TIMEOUT


def run_zone_backtest(
    base_df:         pd.DataFrame,
    base_tf:         str   = "M5",
    initial_balance: float = 100_000.0,
    risk_pct:        float = 0.5,
    apply_costs:     bool  = True,
    tp1_r:           float = 1.0,
    tp2_r:           float = 2.0,
    engine_params:   Optional[dict] = None,
    verbose_every:   int   = 20_000,
) -> tuple[VirtualBroker, ZoneEngine, dict]:
    """
    Replay the zone strategy over `base_df`. Returns (broker, engine, funnel).
    `engine_params` overrides ZoneEngine constructor defaults (every override
    is a new trial variant → the CLI logs it to trials.csv).
    """
    entry_min = tf_minutes("M5")
    h1_min    = tf_minutes("H1")

    m5 = resample(base_df, "M5")
    h1 = resample(base_df, "H1")

    if len(m5) > 1:
        diffs = m5.index.to_series().diff().dropna()
        bar_seconds = max(1, int(diffs.median().total_seconds()))
    else:
        bar_seconds = 300

    commission = CFG.costs.commission_per_lot_rt if apply_costs else 0.0
    swap_long  = CFG.costs.swap_long_per_lot     if apply_costs else 0.0
    swap_short = CFG.costs.swap_short_per_lot    if apply_costs else 0.0

    broker = VirtualBroker(
        initial_balance       = initial_balance,
        risk_pct              = risk_pct,
        slippage_buffer_pips  = _SIZING_BUFFER_PIPS,
        be_buffer_pips        = 0.0,
        max_hold_bars         = _NEVER_TIMEOUT_BARS,
        max_concurrent_trades = 2,
        bar_seconds           = bar_seconds,
        commission_per_lot_rt = commission,
        swap_long_per_lot     = swap_long,
        swap_short_per_lot    = swap_short,
        swap_triple_day       = CFG.costs.swap_triple_day,
        dd_mod_enabled        = False,
        time_stop_enabled     = False,
    )
    engine = ZoneEngine(**(engine_params or {}))

    funnel: dict[str, int] = {
        "touches":              0,
        "skipped_concurrent":   0,
        "skipped_gap":          0,
        "skipped_degenerate":   0,
        "skipped_broker_veto":  0,   # VETO 4/5 or lot-sized-out (safety net)
        "signals_opened":       0,
    }

    logger.info(
        f"ZoneRunner: {len(m5)} M5 bars ({m5.index[0]} → {m5.index[-1]}), "
        f"{len(h1)} H1 bars, balance=${initial_balance:,.0f}, risk={risk_pct}%, "
        f"costs={'ON' if apply_costs else 'OFF'}, TP ladder={tp1_r}R/{tp2_r}R."
    )

    # Column positions for fast row access.
    io, ih, il, ic = (m5.columns.get_loc(c) for c in ("Open", "High", "Low", "Close"))
    m5_idx  = m5.index
    m5_vals = m5.to_numpy()
    h1_idx  = h1.index
    h1_vals = h1.to_numpy()
    jo, jh, jl, jc = (h1.columns.get_loc(c) for c in ("Open", "High", "Low", "Close"))

    h1_ptr = 0
    t0 = time.time()
    total = len(m5)

    for i in range(total - 1):          # last bar cannot host a next-open fill
        ts  = m5_idx[i]
        bar = m5.iloc[i]

        # 1) Resolve open positions on this bar (SL/TP/BE).
        broker.step(ts, bar)

        # 2) Feed H1 bars fully CLOSED at this bar's close (harness-v2).
        decision_ts = ts + pd.Timedelta(minutes=entry_min)
        while h1_ptr < len(h1_idx):
            h1_close_ts = h1_idx[h1_ptr] + pd.Timedelta(minutes=h1_min)
            if h1_close_ts > decision_ts:
                break
            assert h1_close_ts <= decision_ts   # no-lookahead invariant
            engine.on_h1_close(
                h1_idx[h1_ptr],
                float(h1_vals[h1_ptr, jo]), float(h1_vals[h1_ptr, jh]),
                float(h1_vals[h1_ptr, jl]), float(h1_vals[h1_ptr, jc]),
            )
            h1_ptr += 1

        # 3) Touch detection on THIS bar; entry (if any) at NEXT bar's open.
        # Eligibility: the zone is active only from its activation H1 bar's
        # CLOSE, so only M5 bars that OPEN at/after that instant can touch it
        # (a touch inside the very bar whose close activated the zone
        # pre-dates activation — live would never see it).
        bar_high = float(m5_vals[i, ih])
        bar_low  = float(m5_vals[i, il])
        h1_delta = pd.Timedelta(minutes=h1_min)
        touched = [
            z for z in engine.active_zones
            if ts >= z.activation_ts + h1_delta
            and (bar_high >= z.proximal if z.side == "SUPPLY" else bar_low <= z.proximal)
        ]
        if not touched:
            if verbose_every and i and i % verbose_every == 0:
                _progress(i, total, engine, broker, funnel, t0)
            continue

        next_open = float(m5_vals[i + 1, io])
        next_ts   = m5_idx[i + 1]

        for zone in touched:            # active_zones order is deterministic
            engine.consume(zone)        # first touch consumes — always
            funnel["touches"] += 1

            # Max concurrent = 1 signal: touch while legs open → no entry.
            if broker.has_open:
                funnel["skipped_concurrent"] += 1
                continue

            direction = "SELL" if zone.side == "SUPPLY" else "BUY"

            # Gap guard (spec §1.1.4): next M5 open at/beyond SL or distal.
            if direction == "SELL":
                if next_open >= zone.sl or next_open >= zone.distal:
                    funnel["skipped_gap"] += 1
                    continue
                fill = next_open - _FILL_SLIPPAGE_PRICE
            else:
                if next_open <= zone.sl or next_open <= zone.distal:
                    funnel["skipped_gap"] += 1
                    continue
                fill = next_open + _FILL_SLIPPAGE_PRICE

            r_dist = abs(fill - zone.sl)
            if r_dist < _DEGENERATE_MIN_R:
                funnel["skipped_degenerate"] += 1
                continue

            # TP ladder anchored to the actual fill; SL structural (frozen).
            if direction == "SELL":
                tp1 = fill - tp1_r * r_dist
                tp2 = fill - tp2_r * r_dist
            else:
                tp1 = fill + tp1_r * r_dist
                tp2 = fill + tp2_r * r_dist

            legs = broker.open_dual_tp(
                ts          = next_ts,
                direction   = direction,
                entry_price = fill,
                sl          = zone.sl,
                tp1         = tp1,
                tp2         = tp2,
                signal_meta = {
                    "bias":         "SHORT" if direction == "SELL" else "LONG",
                    "score":        0,
                    "market_state": "ZONE",
                    "atr":          zone.atr,
                    "atr_mult":     engine.sl_atr_mult,
                    "tp1_price":    tp1,
                    "sl_initial":   zone.sl,
                    "flags":        {},
                },
            )
            if legs:
                funnel["signals_opened"] += 1
            else:
                funnel["skipped_broker_veto"] += 1

        if verbose_every and i and i % verbose_every == 0:
            _progress(i, total, engine, broker, funnel, t0)

    # Final flush — close stragglers at the last close (boundary artifact).
    last_ts    = m5_idx[-1]
    last_close = float(m5_vals[-1, ic])
    for pos in list(broker._open):
        if not pos.closed:
            broker._close_position(pos, last_ts, last_close, "TIMEOUT")
    broker.equity = broker.balance
    broker.equity_curve.append((last_ts, broker.equity))

    funnel.update(engine.counters)
    logger.info(
        f"ZoneRunner: done in {time.time() - t0:.1f}s | "
        f"zones_activated={engine.counters['zones_activated']} | "
        f"touches={funnel['touches']} | signals={funnel['signals_opened']} | "
        f"closed_legs={len(broker.closed)} | balance=${broker.balance:,.2f}"
    )
    return broker, engine, funnel


def _progress(i, total, engine, broker, funnel, t0) -> None:
    logger.info(
        f"  …{100.0 * i / total:5.1f}%  bar {i}/{total}  "
        f"zones={engine.counters['zones_activated']}  "
        f"signals={funnel['signals_opened']}  "
        f"closed={len(broker.closed)}  balance=${broker.balance:,.2f}  "
        f"({time.time() - t0:.1f}s)"
    )
