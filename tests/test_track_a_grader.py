"""
tests/test_track_a_grader.py — pins the pre-registered TRACK A resolution
rules (R1–R4, tools/track_a_grader.py) with hand-computed fixtures.

Run:  .\\venv\\Scripts\\python.exe -m tests.test_track_a_grader
"""

import math
import sys

import pandas as pd

from tools.track_a_grader import grade_row, grade_file, SPREAD_PRICE

_T0 = pd.Timestamp("2026-01-02 10:00:00")


def _frame(bars):
    """bars = [(minutes_after_10:00, o, h, l, c), ...] → M5 OHLC frame."""
    idx  = [_T0 + pd.Timedelta(minutes=m) for m, *_ in bars]
    data = {
        "Open":   [b[1] for b in bars],
        "High":   [b[2] for b in bars],
        "Low":    [b[3] for b in bars],
        "Close":  [b[4] for b in bars],
        "Volume": [0.0] * len(bars),
    }
    return pd.DataFrame(data, index=pd.DatetimeIndex(idx))


def _row(**kw):
    base = dict(trade_id="T1", post_timestamp="2026-01-02 10:02:00",
                asset="XAUUSD", tf="M5", direction="SELL",
                market_price_at_post="2005", zone_top="2012",
                zone_bottom="2010", sl_stated="2013", tp_stated="2000",
                resolution="", resolve_timestamp="", r_multiple="", note="")
    base.update({k: str(v) for k, v in kw.items()})
    return base


def _approx(a, b, tol=1e-3):
    assert a is not None, f"expected {b}, got None"
    assert math.isclose(a, b, abs_tol=tol), f"expected {b}, got {a}"


def test_win_first_touch_sell():
    # R1 touch-entry at proximal (zone_bottom, approach from below is not
    # possible for supply — market is BELOW, so proximal = zone_bottom 2010),
    # R2 TP-first, R3 costs: entry 2010-0.25=2009.75, sl_dist 3.25,
    # net R = (2009.75-2000)/3.25 - 7/325 = 3.0 - 0.021538 = 2.9785
    ohlc = _frame([(5, 2006, 2007, 2004, 2006),
                   (10, 2006, 2010.5, 2005, 2009),
                   (15, 2008, 2009, 1999.5, 2001)])
    g = grade_row(_row(), ohlc)
    assert g.outcome == "WIN", g.outcome
    _approx(g.entry_price, 2009.75)
    _approx(g.planned_r, 3.0)
    _approx(g.net_r, 2.9785, tol=1e-3)
    assert g.entry_ts == str(_T0 + pd.Timedelta(minutes=10))
    assert g.exit_ts  == str(_T0 + pd.Timedelta(minutes=15))


def test_loss_first_touch_buy():
    # BUY demand zone 1990-1992 from above → proximal 1992, entry 1992.25,
    # sl 1988 → sl_dist 4.25; SL hit → net R = -1 - 7/425 = -1.0165
    ohlc = _frame([(5, 1994, 1995, 1991.8, 1994),
                   (10, 1991, 1991.5, 1987.5, 1990)])
    g = grade_row(_row(direction="BUY", market_price_at_post=1995,
                       zone_top=1992, zone_bottom=1990,
                       sl_stated=1988, tp_stated=2002), ohlc)
    assert g.outcome == "LOSS", g.outcome
    _approx(g.entry_price, 1992.25)
    _approx(g.net_r, -1.0165, tol=1e-3)


def test_same_bar_both_touches_is_pessimistic_loss():
    # Entry bar range covers SL AND TP → R2 pessimistic: LOSS.
    ohlc = _frame([(5, 1984, 1985, 1978.5, 1984)])
    g = grade_row(_row(direction="BUY", market_price_at_post=1985,
                       zone_top=1982, zone_bottom=1980,
                       sl_stated=1979, tp_stated=1983.5), ohlc)
    assert g.outcome == "LOSS", g.outcome
    _approx(g.net_r, -1.0 - 7.0 / ((1982 + SPREAD_PRICE - 1979) * 100), tol=1e-3)


def test_no_trigger():
    ohlc = _frame([(5, 2006, 2007, 2004, 2006), (10, 2006, 2009, 2005, 2008)])
    g = grade_row(_row(zone_top=2052, zone_bottom=2050, sl_stated=2053,
                       tp_stated=2040), ohlc)
    assert g.outcome == "NO_TRIGGER", g.outcome


def test_unresolved_inside_zone_entry():
    # Market already inside the zone at post → immediate entry (R1);
    # neither level hit by data end → UNRESOLVED (R2).
    ohlc = _frame([(5, 1990, 1996, 1988, 1992), (10, 1992, 1995, 1989, 1994)])
    g = grade_row(_row(direction="BUY", market_price_at_post=1990.5,
                       zone_top=1991, zone_bottom=1990,
                       sl_stated=1985, tp_stated=2050), ohlc)
    assert g.outcome == "UNRESOLVED", g.outcome
    _approx(g.entry_price, 1990.75)


def test_ungradeable_no_sl_beats_missing_asset_data():
    # R4 order: no-SL is counted even when we ALSO lack OHLC for the asset.
    g = grade_row(_row(sl_stated="N/A"), None)
    assert g.outcome == "UNGRADEABLE_NO_SL", g.outcome


def test_ungradeable_asset():
    g = grade_row(_row(asset="BTCUSD"), None)
    assert g.outcome == "UNGRADEABLE_ASSET", g.outcome


def test_grade_file_counts_and_preregistered_verdict(tmp_path=None):
    import tempfile, os
    ohlc = _frame([(5, 2006, 2007, 2004, 2006),
                   (10, 2006, 2010.5, 2005, 2009),
                   (15, 2008, 2009, 1999.5, 2001)])
    rows = [
        _row(trade_id="A"),                                   # WIN
        _row(trade_id="B", sl_stated="N/A"),                  # UNGRADEABLE_NO_SL
        _row(trade_id="C", asset="BTCUSD"),                   # UNGRADEABLE_ASSET
        _row(trade_id="D", zone_top=2052, zone_bottom=2050,
             sl_stated=2053, tp_stated=2040),                 # NO_TRIGGER
    ]
    with tempfile.TemporaryDirectory() as td:
        posts = os.path.join(td, "posts.csv")
        pd.DataFrame(rows).to_csv(posts, index=False)
        rep = grade_file(posts, {"XAUUSD": ohlc})
    assert rep["rows_total"] == 4
    assert rep["outcome_counts"] == {"NO_TRIGGER": 1, "UNGRADEABLE_ASSET": 1,
                                     "UNGRADEABLE_NO_SL": 1, "WIN": 1}
    _approx(rep["ungradeable_no_sl_rate_pct"], 25.0)
    assert rep["n_gradeable"] == 1
    _approx(rep["hit_rate_pct"], 100.0)
    _approx(rep["expectancy_net_r"], 2.9785, tol=1e-3)
    # n < 30 → the pre-registered read must FAIL regardless of expectancy.
    assert rep["verdict"].startswith("FAIL"), rep["verdict"]


def main() -> int:
    tests = [test_win_first_touch_sell, test_loss_first_touch_buy,
             test_same_bar_both_touches_is_pessimistic_loss,
             test_no_trigger, test_unresolved_inside_zone_entry,
             test_ungradeable_no_sl_beats_missing_asset_data,
             test_ungradeable_asset,
             test_grade_file_counts_and_preregistered_verdict]
    for fn in tests:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\nOK — {len(tests)}/{len(tests)} Track A grader tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
