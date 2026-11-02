"""
tests/test_per_signal_metrics.py — Phase 0.2 unit test (Gate G0).

Verifies backtest/metrics.py per-SIGNAL statistics:
  1. Legs grouped by signal_id → per-signal net PnL / net R / t-stat (ddof=1)
     match hand-computed values.
  2. Fallback grouping (signal_id == 0, pre-harness-v2 data) via the
     (open_ts, direction, entry, sl) tuple gives identical numbers — including
     two opposite-direction signals sharing the same open_ts.
  3. Legacy per-leg fields are still present and unchanged (comparison only).

Run:  .\\venv\\Scripts\\python.exe -m tests.test_per_signal_metrics
Zero third-party test deps (plain asserts); pytest-compatible if installed.
"""

import math
import sys
from dataclasses import replace
from types import SimpleNamespace

import pandas as pd

from backtest.metrics import compute_metrics
from backtest.virtual_broker import ClosedTrade

_TS0 = pd.Timestamp("2026-01-05 10:00:00")


def _leg(ticket, leg, direction, entry, sl, lot, pnl, outcome,
         signal_id, open_ts=_TS0, close_off_min=60):
    return ClosedTrade(
        ticket=ticket, leg=leg, direction=direction, bias="LONG",
        score=80, market_state="BULLISH_TREND", atr=5.0, atr_mult=1.0,
        lot=lot, entry_price=entry, sl=sl, tp=entry + 10,
        open_ts=open_ts, close_ts=open_ts + pd.Timedelta(minutes=close_off_min),
        close_price=entry, outcome=outcome, pnl=pnl, rr_realised=0.0,
        signal_id=signal_id,
    )


def _fixture_legs(with_ids: bool):
    """
    3 signals, 5 legs. slippage buffer = 0 in the fixture broker, so
    risk$ = lot * |entry - sl| * 100.

      sig 1 (BUY,  open 10:00): legs +100, +150   risk 2*(0.5*10*100)=1000  -> R +0.25
      sig 2 (SELL, open 10:00): legs  -40,  -60   risk 2*(1.0*5*100) =1000  -> R -0.10
      sig 3 (BUY,  open 12:00): solo leg     0    risk 0.2*10*100    = 200  -> R  0.00

    Per-signal pnls [250, -100, 0]: mean 50, sd(ddof=1) 180.2776,
      t = 50 / (180.2776/sqrt(3)) = 0.4804
    Per-signal Rs [0.25, -0.10, 0]: mean 0.05, t identical 0.4804
    Per-leg pnls [100, 150, -40, -60, 0]: mean 30, sd(ddof=1) 91.1043,
      t = 30 / (91.1043/sqrt(5)) = 0.7363
    """
    s1, s2, s3 = (1, 2, 3) if with_ids else (0, 0, 0)
    ts2 = _TS0 + pd.Timedelta(hours=2)
    return [
        _leg(1, "TP1",  "BUY",  2000.0, 1990.0, 0.5, +100.0, "WIN_TP1", s1),
        _leg(2, "TP2",  "BUY",  2000.0, 1990.0, 0.5, +150.0, "WIN_TP2", s1),
        # Same open_ts as sig 1 but opposite direction — the adversarial case
        # for tuple-key fallback grouping.
        _leg(3, "TP1",  "SELL", 2000.0, 1995.0, 1.0,  -40.0, "LOSS",    s2),
        _leg(4, "TP2",  "SELL", 2000.0, 1995.0, 1.0,  -60.0, "LOSS",    s2),
        _leg(5, "SOLO", "BUY",  1900.0, 1890.0, 0.2,    0.0, "BE_HIT",  s3,
             open_ts=ts2),
    ]


def _fixture_broker(trades):
    eq, curve = 100_000.0, [(_TS0, 100_000.0)]
    for t in sorted(trades, key=lambda x: x.close_ts):
        eq += t.pnl
        curve.append((t.close_ts, eq))
    return SimpleNamespace(
        closed=trades, equity_curve=curve, balance=eq,
        contract_size=100.0, point=0.01, slippage_buffer_pips=0.0,
    )


def _approx(a, b, tol=1e-3):
    assert a is not None, f"expected {b}, got None"
    assert math.isclose(a, b, abs_tol=tol), f"expected {b}, got {a}"


def _check_per_signal(ps):
    assert ps["signals"] == 3,        f"signals: {ps['signals']}"
    assert ps["legs_covered"] == 5,   f"legs_covered: {ps['legs_covered']}"
    assert ps["wins"] == 1 and ps["losses"] == 1 and ps["flat"] == 1, (
        f"W/L/F: {ps['wins']}/{ps['losses']}/{ps['flat']}")
    _approx(ps["win_rate_pct"], 33.33, tol=0.01)
    _approx(ps["profit_factor"], 2.5)
    _approx(ps["expectancy_usd"], 50.0)
    _approx(ps["expectancy_r"], 0.05, tol=1e-4)
    _approx(ps["t_stat_pnl"], 0.480)
    _approx(ps["t_stat_r"], 0.480)
    assert ps["signals_with_risk"] == 3


def test_per_signal_by_signal_id():
    trades = _fixture_legs(with_ids=True)
    m = compute_metrics(_fixture_broker(trades), 100_000.0)
    _check_per_signal(m["per_signal"])


def test_per_signal_fallback_tuple_key():
    trades = _fixture_legs(with_ids=False)
    m = compute_metrics(_fixture_broker(trades), 100_000.0)
    _check_per_signal(m["per_signal"])


def test_legacy_per_leg_unchanged():
    trades = _fixture_legs(with_ids=True)
    m = compute_metrics(_fixture_broker(trades), 100_000.0)
    assert m["trades"] == 5
    assert m["wins"] == 2 and m["losses"] == 2 and m["be_exits"] == 1
    _approx(m["t_stat"], 0.736)
    _approx(m["expectancy_per_trade"], 30.0)
    _approx(m["profit_factor"], 2.5)


def main() -> int:
    tests = [test_per_signal_by_signal_id,
             test_per_signal_fallback_tuple_key,
             test_legacy_per_leg_unchanged]
    for fn in tests:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\nOK — {len(tests)}/{len(tests)} per-signal metric tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
