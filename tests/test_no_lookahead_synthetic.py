"""
tests/test_no_lookahead_synthetic.py — the pre-registered lookahead invariant on SYNTHETIC data.

Public counterpart of tests/test_no_lookahead.py. That test runs on the real price data, which is
not published (redistribution terms not verified). This one exercises the same invariant with the
same public code and needs no price CSV and no private configuration:

  * synthetic M5 OHLCV series (seeded random walk, trading days only, a missing-bar gap each day)
    -> build_multi_tf_frames() -> H1 / M15 / M5 frames, exactly as the backtest builds them;
  * 1,000 random replay steps (fixed seed); decision time T = close of the entry bar;
  * slice_window() for every frame, and the pre-registered assertion for every window:
        last bar open + bar duration <= T          (no forming / future bar)
    plus: full window length, sorted index, entry window ends exactly at the decision bar.

Mutation check: the same invariant is run against the harness-v1 rule (slice .loc[:ts], with ts the
entry bar's OPEN time). That rule MUST violate the invariant — otherwise this test could not tell
the old slicer from the fixed one. In CI both directions run:
    python -m tests.test_no_lookahead_synthetic                    # fixed rule: must pass
    python -m tests.test_no_lookahead_synthetic --slicer legacy    # old rule: must FAIL (exit 1)

What it does not cover: the real data (gaps, timestamps and resampling of the actual price file).
That is covered only by tests/test_no_lookahead.py, which needs the withheld price data.
"""
import argparse
import random
import sys

import numpy as np
import pandas as pd

from backtest.data_loader import build_multi_tf_frames, slice_window, tf_minutes
from tests.public_config_stub import CFG

LOOKBACK = CFG.symbol.candles_lookback      # 200, as in the backtest runner
N_STEPS = 1000
SEED = 20260704                              # same seed as the real-data test
PRIMARY_TF, SECONDARY_TF, ENTRY_TF = "H1", "M15", "M5"


def synthetic_m5(days: int = 120, seed: int = 7) -> pd.DataFrame:
    """Seeded random-walk M5 bars on weekdays, 00:00-22:55 (a daily gap, like a broker session
    break), with a few missing bars per day, so window boundaries are irregular."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2025-01-06", periods=days * 288, freq="5min")
    idx = idx[(idx.dayofweek < 5) & (idx.hour < 23)]
    keep = rng.random(len(idx)) > 0.01                  # ~1% missing bars
    idx = idx[keep]
    close = 2000 + np.cumsum(rng.normal(0, 1.0, len(idx)))
    open_ = np.concatenate([[close[0]], close[:-1]])
    spread = np.abs(rng.normal(0, 0.6, len(idx)))
    return pd.DataFrame({"Open": open_, "High": np.maximum(open_, close) + spread,
                         "Low": np.minimum(open_, close) - spread, "Close": close,
                         "Volume": rng.integers(1, 100, len(idx))}, index=idx)


def legacy_slice_window(full_df, decision_ts, bars, bar_minutes):
    """Harness-v1 rule, reproduced for the mutation check: slice by the entry bar's OPEN time
    (decision time minus one entry bar), which keeps a still-forming higher-timeframe bar."""
    ts = decision_ts - pd.Timedelta(minutes=tf_minutes(ENTRY_TF))
    cutoff = full_df.loc[:ts]
    return None if len(cutoff) < bars else cutoff.iloc[-bars:].copy()


def run(slicer) -> dict:
    frames = build_multi_tf_frames(synthetic_m5(), base_tf="M5", primary_tf=PRIMARY_TF,
                                   secondary_tf=SECONDARY_TF, entry_tf=ENTRY_TF)
    specs = [("primary", frames["primary"], tf_minutes(PRIMARY_TF)),
             ("secondary", frames["secondary"], tf_minutes(SECONDARY_TF)),
             ("entry", frames["entry"], tf_minutes(ENTRY_TF))]
    entry_df = frames["entry"]
    rng = random.Random(SEED)
    checked = warmup = attempts = 0
    violations = {name: 0 for name, _, _ in specs}
    while checked < N_STEPS:
        attempts += 1
        assert attempts <= N_STEPS * 20, "could not draw enough valid replay steps"
        i = rng.randrange(LOOKBACK, len(entry_df) - 1)
        ts = entry_df.index[i]                                        # entry bar OPEN time
        decision_ts = ts + pd.Timedelta(minutes=tf_minutes(ENTRY_TF))  # its CLOSE = decision time
        wins = {name: slicer(df, decision_ts, LOOKBACK, mins) for name, df, mins in specs}
        if any(w is None for w in wins.values()):
            warmup += 1
            continue
        for name, _, mins in specs:
            w = wins[name]
            assert len(w) == LOOKBACK, f"[{name}] window length {len(w)} != {LOOKBACK}"
            assert w.index.is_monotonic_increasing, f"[{name}] window not sorted"
            if w.index[-1] + pd.Timedelta(minutes=mins) > decision_ts:   # THE invariant
                violations[name] += 1
        assert wins["entry"].index[-1] == ts, "entry window does not end at the decision bar"
        checked += 1
    return {"checked": checked, "warmup": warmup, "violations": violations}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slicer", choices=("fixed", "legacy"), default="fixed",
                    help="fixed = backtest.data_loader.slice_window (must pass); "
                         "legacy = harness-v1 rule (must fail)")
    a = ap.parse_args()
    slicer = slice_window if a.slicer == "fixed" else legacy_slice_window
    r = run(slicer)
    v = r["violations"]
    print(f"[{a.slicer}] {r['checked']}/{N_STEPS} synthetic replay steps (warmup draws skipped: {r['warmup']}); "
          f"windows with a bar closing after the decision time: H1 {v['primary']}, M15 {v['secondary']}, "
          f"M5 {v['entry']}")
    if any(v.values()):
        print(f"FAIL — lookahead: the {a.slicer} slicer included a bar that closes after the decision time.")
        return 1
    print("PASS — every H1/M15/M5 window bar closes at or before the decision time.")
    return 0


def test_fixed_rule_passes():
    assert not any(run(slice_window)["violations"].values())


def test_legacy_rule_fails():          # mutation check: the invariant must catch the old rule
    assert run(legacy_slice_window)["violations"]["primary"] > 0


if __name__ == "__main__":
    sys.exit(main())
