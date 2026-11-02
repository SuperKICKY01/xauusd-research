"""
tests/test_zone_engine.py — unit tests for quant/zone_engine.py against the
frozen Phase-1 spec (specs/phase1_zone_spec_v1.md §1.1.2–§1.1.3).

Zero-dependency plain asserts (no pytest in the venv):
    .\\venv\\Scripts\\python.exe -m tests.test_zone_engine

Fixture design (hand-computed): 14 warmup H1 bars with TR exactly 10 pin
ATR(14) at exactly 10.0, so zone geometry / SL / impulse thresholds are
exact decimals, not floating approximations.
"""

from quant.zone_engine import ZoneEngine

# Warmup: TR = 10 every bar (H−L=10, prev close inside range) ⇒ ATR[13] = 10.
_WARMUP = [(2000.0, 2005.0, 1995.0, 2000.0)] * 14

# Supply-swing block (indices 14..18 after warmup; TR stays exactly 10):
#   i=16 swing high: H=2012 > highs of 14,15,17,18. Zone: distal 2012,
#   proximal max(O,C)=2004, height 8 = 0.8×ATR. Impulse k=17: close 1996
#   ≤ 2012 − 1.5×10 = 1997. Activation a = max(k=17, i+2=18) = 18.
_SUPPLY_BLOCK = [
    (2000.0, 2005.0, 1995.0, 2000.0),   # 14
    (2000.0, 2005.0, 1995.0, 2002.0),   # 15
    (2004.0, 2012.0, 2002.0, 2004.0),   # 16  swing
    (2004.0, 2004.0, 1994.0, 1996.0),   # 17  impulse (close ≤ 1997)
    (1996.0, 2000.0, 1990.0, 1995.0),   # 18  wing → activation bar
]


def _feed(engine: ZoneEngine, bars) -> None:
    for n, (o, h, l, c) in enumerate(bars):
        engine.on_h1_close(n, o, h, l, c)


def test_atr_wilder_hand_computed():
    """ATR definition (§1.1.2): TR[0]=H−L; seed = SMA; then Wilder RMA."""
    e = ZoneEngine(atr_period=3)
    bars = [
        (11.0, 12.0, 10.0, 11.0),   # TR = 2
        (11.0, 13.0, 11.0, 12.0),   # TR = max(2, 2, 0) = 2
        (12.0, 15.0, 12.0, 13.0),   # TR = max(3, 3, 0) = 3 → ATR[2] = 7/3
        (13.0, 14.0, 12.0, 13.0),   # TR = max(2, 1, 1) = 2 → ATR[3] = (7/3·2+2)/3
    ]
    _feed(e, bars)
    assert e._atr[0] is None and e._atr[1] is None
    assert abs(e._atr[2] - 7.0 / 3.0) < 1e-12, e._atr[2]
    assert abs(e._atr[3] - (7.0 / 3.0 * 2 + 2) / 3.0) < 1e-12, e._atr[3]


def test_supply_zone_activation_and_geometry():
    """Fractal + impulse + geometry + frozen SL; activation at max(k, i+2)."""
    e = ZoneEngine()
    _feed(e, _WARMUP + _SUPPLY_BLOCK)

    zones = e.active_zones
    assert len(zones) == 1, [z.__dict__ for z in e.all_zones]
    z = zones[0]
    assert z.side == "SUPPLY"
    assert z.distal == 2012.0
    assert z.proximal == 2004.0
    assert abs(z.atr - 10.0) < 1e-9, z.atr
    assert abs(z.sl - 2014.0) < 1e-9, z.sl          # 2012 + 0.2×10
    assert z.swing_idx == 16
    assert z.activation_idx == 18                    # max(k=17, i+2=18)
    assert e.counters["zones_activated"] == 1


def test_late_impulse_activates_at_k():
    """Impulse only at k = i+3 → activation bar = k = 19 (> i+2)."""
    block = [
        (2000.0, 2005.0, 1995.0, 2000.0),   # 14
        (2000.0, 2005.0, 1995.0, 2002.0),   # 15
        (2004.0, 2012.0, 2002.0, 2004.0),   # 16  swing
        (2004.0, 2004.0, 1994.0, 2000.0),   # 17  close 2000 > 1997 — no impulse
        (2000.0, 2000.0, 1990.0, 1999.0),   # 18  close 1999 > 1997 — no impulse
        (1999.0, 1999.0, 1989.0, 1994.0),   # 19  close 1994 ≤ 1997 → k = 19
    ]
    e = ZoneEngine()
    _feed(e, _WARMUP + block)
    zones = e.active_zones
    assert len(zones) == 1
    assert zones[0].activation_idx == 19


def test_no_impulse_no_zone():
    """No close ≥ 1.5×ATR from the extreme within i+1..i+3 → candidate dies."""
    block = [
        (2000.0, 2005.0, 1995.0, 2000.0),   # 14
        (2000.0, 2005.0, 1995.0, 2002.0),   # 15
        (2004.0, 2012.0, 2002.0, 2004.0),   # 16  swing
        (2004.0, 2004.0, 1994.0, 2000.0),   # 17
        (2000.0, 2000.0, 1990.0, 1999.0),   # 18
        (1999.0, 1999.0, 1989.0, 1998.0),   # 19  close 1998 > 1997 — window ends
    ]
    e = ZoneEngine()
    _feed(e, _WARMUP + block)
    assert e.active_zones == []
    assert e.counters["candidates_expired"] == 1
    assert e.counters["zones_activated"] == 0


def test_min_height_guard_rejects_doji_zone():
    """Swing bar with body edge == extreme → height 0 < 0.05×ATR → rejected."""
    block = [
        (2000.0, 2005.0, 1995.0, 2000.0),   # 14
        (2000.0, 2005.0, 1995.0, 2002.0),   # 15
        (2012.0, 2012.0, 2002.0, 2012.0),   # 16  swing, O=C=H → height 0
        (2004.0, 2004.0, 1994.0, 1996.0),   # 17  impulse
        (1996.0, 2000.0, 1990.0, 1995.0),   # 18
    ]
    e = ZoneEngine()
    _feed(e, _WARMUP + block)
    assert e.active_zones == []
    assert e.counters["rejected_min_height"] == 1


def test_max_height_guard():
    """Same fixture, engine capped at 0.5×ATR → height 8 > 5 → rejected."""
    e = ZoneEngine(max_height_atr=0.5)
    _feed(e, _WARMUP + _SUPPLY_BLOCK)
    assert e.active_zones == []
    assert e.counters["rejected_max_height"] == 1


def test_invalidation_on_close_beyond_distal():
    e = ZoneEngine()
    _feed(e, _WARMUP + _SUPPLY_BLOCK)
    assert len(e.active_zones) == 1
    e.on_h1_close(19, 1995.0, 2015.0, 1994.0, 2013.0)   # H1 CLOSE 2013 > 2012
    assert e.active_zones == []
    assert e.all_zones[0].death_reason == "invalidated"
    assert e.counters["zones_invalidated"] == 1


def test_high_beyond_distal_does_not_invalidate():
    """Only a CLOSE beyond distal kills the zone; a wick above does not."""
    e = ZoneEngine()
    _feed(e, _WARMUP + _SUPPLY_BLOCK)
    e.on_h1_close(19, 1995.0, 2015.0, 1994.0, 2005.0)   # high 2015, close 2005
    assert len(e.active_zones) == 1


def test_expiry_after_max_age():
    e = ZoneEngine(max_age_bars=3)
    _feed(e, _WARMUP + _SUPPLY_BLOCK)              # activation_idx = 18
    for n in (19, 20):
        e.on_h1_close(n, 2000.0, 2004.0, 1996.0, 2000.0)
        assert len(e.active_zones) == 1, n
    e.on_h1_close(21, 2000.0, 2004.0, 1996.0, 2000.0)   # age = 21−18 = 3 → dead
    assert e.active_zones == []
    assert e.all_zones[0].death_reason == "expired"
    assert e.counters["zones_expired"] == 1


def test_overlap_newer_replaces_older():
    """Second overlapping SUPPLY zone replaces the first (freshness)."""
    block2 = [
        (1995.0, 2000.0, 1990.0, 1998.0),   # 19
        (1998.0, 2002.0, 1992.0, 2000.0),   # 20
        (2003.0, 2010.0, 2000.0, 2003.0),   # 21  swing 2: distal 2010, prox 2003
        (2003.0, 2003.0, 1993.0, 1994.0),   # 22  impulse (≤ 2010 − 1.5×ATR)
        (1994.0, 1999.0, 1989.0, 1994.0),   # 23  wing → activation
    ]
    e = ZoneEngine()
    _feed(e, _WARMUP + _SUPPLY_BLOCK + block2)
    zones = e.active_zones
    assert len(zones) == 1, [(z.distal, z.death_reason) for z in e.all_zones]
    assert zones[0].distal == 2010.0                # the newer zone survives
    assert e.counters["zones_replaced"] == 1
    old = [z for z in e.all_zones if z.distal == 2012.0][0]
    assert old.dead and old.death_reason == "replaced"


def test_consumption():
    e = ZoneEngine()
    _feed(e, _WARMUP + _SUPPLY_BLOCK)
    z = e.active_zones[0]
    e.consume(z)
    assert e.active_zones == []
    assert e.counters["zones_consumed"] == 1
    e.consume(z)                                     # idempotent
    assert e.counters["zones_consumed"] == 1


def test_demand_zone_mirror():
    """Mirrored demand fixture: swing low + upward impulse."""
    block = [
        (2000.0, 2005.0, 1995.0, 2000.0),   # 14
        (2000.0, 2005.0, 1995.0, 1998.0),   # 15
        (1996.0, 1998.0, 1988.0, 1996.0),   # 16  swing low 1988; prox min(O,C)=1996
        (1996.0, 2006.0, 1996.0, 2004.0),   # 17  impulse: 2004 ≥ 1988 + 15 = 2003
        (2004.0, 2008.0, 1998.0, 2004.0),   # 18  wing (low 1998 > 1988)
    ]
    e = ZoneEngine()
    _feed(e, _WARMUP + block)
    zones = e.active_zones
    assert len(zones) == 1, [z.__dict__ for z in e.all_zones]
    z = zones[0]
    assert z.side == "DEMAND"
    assert z.distal == 1988.0
    assert z.proximal == 1996.0
    # ATR drifts off 10 here (TR[16] = |1988−1998| = 10, TR[17] = 10 — recompute):
    # TR14..18 = 10,10,10,10,10 → ATR stays exactly 10.
    assert abs(z.atr - 10.0) < 1e-9, z.atr
    assert abs(z.sl - 1986.0) < 1e-9, z.sl          # 1988 − 0.2×10
    assert z.activation_idx == 18


def main() -> int:
    tests = [
        test_atr_wilder_hand_computed,
        test_supply_zone_activation_and_geometry,
        test_late_impulse_activates_at_k,
        test_no_impulse_no_zone,
        test_min_height_guard_rejects_doji_zone,
        test_max_height_guard,
        test_invalidation_on_close_beyond_distal,
        test_high_beyond_distal_does_not_invalidate,
        test_expiry_after_max_age,
        test_overlap_newer_replaces_older,
        test_consumption,
        test_demand_zone_mirror,
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
