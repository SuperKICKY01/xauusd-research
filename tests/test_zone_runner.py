"""
tests/test_zone_runner.py — end-to-end tests of backtest/zone_runner.py on
synthetic M5 data engineered bar-by-bar (frozen spec v1.0 semantics).

Zero-dependency plain asserts:
    .\\venv\\Scripts\\python.exe -m tests.test_zone_runner

Fixture: 14 warmup H1 hours pin ATR(14) at exactly 10.0, then a SUPPLY zone
(distal 2012, proximal 2004, SL 2014) activates at the close of hour 18.
Scenario A adds a DEMAND zone (activates close of hour 23) and a touch bar
that hits BOTH zones — exercising fill, TP ladder, BE move, per-signal
grouping, and the concurrency skip in one deterministic replay.
"""

import pandas as pd

from backtest.zone_runner import run_zone_backtest

# H1 script shared by all scenarios (hours 0..18). TR = 10 every bar through
# hour 18 ⇒ ATR(14) = 10.0 exactly at the supply swing bar (hour 16).
_H1_BASE = (
    [(2000.0, 2005.0, 1995.0, 2000.0)] * 14 +   # 0-13 warmup
    [
        (2000.0, 2005.0, 1995.0, 2000.0),        # 14
        (2000.0, 2005.0, 1995.0, 2002.0),        # 15
        (2004.0, 2012.0, 2002.0, 2004.0),        # 16 SUPPLY swing
        (2004.0, 2004.0, 1994.0, 1996.0),        # 17 impulse down (≤ 1997)
        (1996.0, 2000.0, 1990.0, 1995.0),        # 18 wing → active from 19:00
    ]
)

# Hours 19..23 (Scenario A only): builds a DEMAND zone (swing low 1985 at
# hour 21, upward impulse at 22, activation at close of hour 23 = 00:00 D2).
_H1_DEMAND = [
    (1995.0, 2001.0, 1992.0, 1998.0),            # 19
    (1998.0, 2002.0, 1993.0, 2000.0),            # 20
    (1992.0, 1993.0, 1985.0, 1990.0),            # 21 DEMAND swing
    (1990.0, 2003.0, 1988.0, 2002.0),            # 22 impulse up
    (2002.0, 2003.0, 1989.0, 2001.0),            # 23 wing
]


def _h1_to_m5(o, h, l, c):
    """12 M5 bars aggregating exactly to the H1 bar (extremes on bar 5)."""
    return [(o, o, o, o)] * 5 + [(o, h, l, c)] + [(c, c, c, c)] * 6


def _build_df(hours_scripted, hours_explicit_m5):
    rows = []
    for o, h, l, c in hours_scripted:
        rows.extend(_h1_to_m5(o, h, l, c))
    for hour in hours_explicit_m5:
        assert len(hour) == 12
        rows.extend(hour)
    idx = pd.date_range("2024-01-01 00:00", periods=len(rows), freq="5min")
    df = pd.DataFrame(rows, columns=["Open", "High", "Low", "Close"], index=idx)
    df["Volume"] = 0.0
    return df


def _run(df):
    return run_zone_backtest(
        base_df=df, initial_balance=100_000.0, risk_pct=0.5,
        apply_costs=False, verbose_every=0,
    )


def test_full_trade_tp1_tp2_and_concurrency_skip():
    """Scenario A: touch → SELL fill 2002.75 → TP1 (+1R) → TP2 (+2R);
    the same touch bar hits the demand zone → consumed + skipped_concurrent."""
    flat  = lambda v: (v, v, v, v)
    h24 = [flat(2001.0)] * 4 + [
        (2001.0, 2006.0, 1988.0, 2001.0),   # 00:20 touches BOTH zones
        flat(2003.0),                       # 00:25 entry bar → fill 2002.75
        (2003.0, 2003.0, 1991.0, 1992.0),   # 00:30 TP1 1991.5 hit; TP2 SL→BE
        (1992.0, 1992.0, 1980.0, 1981.0),   # 00:35 TP2 1980.25 hit
    ] + [flat(1981.0)] * 4
    h25 = [flat(1981.0)] * 12
    df = _build_df(_H1_BASE + _H1_DEMAND, [h24, h25])

    broker, engine, funnel = _run(df)

    assert funnel["zones_activated"] == 2, funnel
    assert funnel["touches"] == 2, funnel
    assert funnel["signals_opened"] == 1, funnel
    assert funnel["skipped_concurrent"] == 1, funnel
    assert funnel["skipped_gap"] == 0 and funnel["skipped_degenerate"] == 0
    assert funnel["zones_consumed"] == 2

    legs = broker.closed
    assert len(legs) == 2, [(t.outcome, t.close_price) for t in legs]
    sid = {t.signal_id for t in legs}
    assert len(sid) == 1 and sid.pop() > 0
    for t in legs:
        assert t.direction == "SELL"
        assert abs(t.entry_price - 2002.75) < 1e-9
        assert abs(t.sl - 2014.0) < 1e-9            # structural, not re-anchored
    tp1 = next(t for t in legs if t.leg == "TP1")
    tp2 = next(t for t in legs if t.leg == "TP2")
    assert tp1.outcome == "WIN_TP1" and abs(tp1.close_price - 1991.50) < 1e-9
    assert tp2.outcome == "WIN_TP2" and abs(tp2.close_price - 1980.25) < 1e-9
    assert abs(tp1.rr_realised - 1.0) < 1e-3
    assert abs(tp2.rr_realised - 2.0) < 1e-3
    assert abs(tp1.lot + tp2.lot - 0.43) < 1e-9    # 0.5% of 100k @ 11.5 risk
    assert abs(tp1.pnl - 11.25 * tp1.lot * 100) < 1e-6
    assert abs(tp2.pnl - 22.50 * tp2.lot * 100) < 1e-6

    from backtest.metrics import compute_metrics
    m = compute_metrics(broker, 100_000.0)
    ps = m["per_signal"]
    assert ps["signals"] == 1 and ps["legs_covered"] == 2
    expected_r = (tp1.pnl + tp2.pnl) / (0.43 * (11.25 + 0.25) * 100.0)
    assert abs(ps["expectancy_r"] - expected_r) < 1e-3, ps


def test_gap_beyond_sl_skips_and_consumes():
    """Scenario B: next M5 open 2015 ≥ SL 2014 → skipped_gap, no trade,
    zone consumed (first touch consumes regardless — spec §1.1.3)."""
    flat = lambda v: (v, v, v, v)
    h19 = [flat(1995.0)] * 4 + [
        (1995.0, 2005.0, 1995.0, 1995.0),   # 19:20 touch
        (2015.0, 2015.0, 2005.0, 2005.0),   # 19:25 open beyond SL
    ] + [flat(2005.0)] * 6
    h20 = [flat(2005.0)] * 12
    df = _build_df(_H1_BASE, [h19, h20])

    broker, engine, funnel = _run(df)

    assert funnel["zones_activated"] == 1
    assert funnel["touches"] == 1
    assert funnel["skipped_gap"] == 1
    assert funnel["signals_opened"] == 0
    assert funnel["zones_consumed"] == 1
    assert broker.closed == []


def test_break_even_exit_at_exact_fill():
    """Scenario C: TP1 hits, then price returns to the fill price → TP2 leg
    closes BE_HIT at EXACTLY the fill (be_buffer = 0, spec §1.1.6)."""
    flat = lambda v: (v, v, v, v)
    h19 = [flat(2000.0)] * 4 + [
        (2000.0, 2005.0, 2000.0, 2000.0),   # 19:20 touch
        flat(2003.0),                       # 19:25 entry → fill 2002.75
        (2003.0, 2003.0, 1991.0, 1992.0),   # 19:30 TP1 hit; TP2 SL→2002.75
        (1992.0, 2003.0, 1992.0, 2000.0),   # 19:35 high 2003 ≥ BE SL
    ] + [flat(2000.0)] * 4
    h20 = [flat(2000.0)] * 12
    df = _build_df(_H1_BASE, [h19, h20])

    broker, engine, funnel = _run(df)

    legs = broker.closed
    assert len(legs) == 2, [(t.outcome, t.close_price) for t in legs]
    tp2 = next(t for t in legs if t.leg == "TP2")
    assert tp2.outcome == "BE_HIT", tp2.outcome
    assert abs(tp2.close_price - 2002.75) < 1e-9
    assert tp2.pnl == 0.0                       # exact BE, costs off
    tp1 = next(t for t in legs if t.leg == "TP1")
    assert tp1.outcome == "WIN_TP1"


def test_touch_before_activation_close_is_not_eligible():
    """A high that grazes the proximal INSIDE the activation hour itself must
    not trigger (zone active only from the H1 close). Hour 18 M5 bars touch
    2004 — no trade may result; the zone survives to hour 19 untouched."""
    flat = lambda v: (v, v, v, v)
    # Rebuild hour 18 with an intra-hour graze of the proximal edge:
    h18 = [flat(1996.0)] * 4 + [
        (1996.0, 2004.5, 1990.0, 1996.0),   # 18:20 — grazes 2004 pre-activation
        (1996.0, 2000.0, 1996.0, 1995.0),
    ] + [flat(1995.0)] * 6
    h19 = [flat(1995.0)] * 12               # quiet hour — no touch
    df = _build_df(_H1_BASE[:18], [h18, h19])

    broker, engine, funnel = _run(df)

    assert funnel["zones_activated"] == 1, funnel
    assert funnel["touches"] == 0, funnel
    assert funnel["zones_consumed"] == 0
    assert broker.closed == []


def main() -> int:
    tests = [
        test_full_trade_tp1_tp2_and_concurrency_skip,
        test_gap_beyond_sl_skips_and_consumes,
        test_break_even_exit_at_exact_fill,
        test_touch_before_activation_close_is_not_eligible,
    ]
    passed = 0
    for t in tests:
        t()
        passed += 1
        print(f"PASS  {t.__name__}")
    print(f"\n{passed}/{len(tests)} PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
