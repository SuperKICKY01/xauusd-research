"""
tests/test_no_lookahead.py — Phase 0.1 MANDATORY unit test (Gate G0).

Pre-registered spec text (Phase 0, deliverable 0.1):
    "UNIT TEST (mandatory): iterate 1,000 random replay steps; assert
     max(HTF bar close time) <= decision time. Must pass 100%."

What this does, on the REAL backtest data (data/xauusd_m5_full.csv):
  • Rebuilds the exact frames the strategy_runner uses
    (build_multi_tf_frames → H1 / M15 / M5, label="left" = open-time index).
  • Draws 1,000 random replay steps i (fixed seed — reproducible). For each,
    decision time T = entry bar i's CLOSE (= open ts + 5 min), exactly as
    strategy_runner computes it.
  • Calls slice_window() for all three frames the same way the runner does
    and asserts, for EVERY window:
        window.index[-1] + bar_duration <= T      (no forming/future bar)
        len(window) == LOOKBACK                    (full window)
        entry window ends exactly at bar i         (no over-trim regression)
  • Also counts how many of those same steps the HARNESS-V1 rule
    (.loc[:ts], open-time cutoff) would have included a forming H1 bar —
    asserted > 0, proving this test actually bites on the old behaviour.

Run:  .\\venv\\Scripts\\python.exe -m tests.test_no_lookahead
Zero third-party test deps (plain asserts); pytest-compatible if installed.
"""

import os
import random
import sys

import pandas as pd

from config import CFG
from backtest.data_loader import build_multi_tf_frames, load_csv, slice_window, tf_minutes

CSV = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "data", "xauusd_m5_full.csv")
LOOKBACK   = CFG.symbol.candles_lookback          # 200 — same as strategy_runner
N_STEPS    = 1000
SEED       = 20260704
PRIMARY_TF, SECONDARY_TF, ENTRY_TF = "H1", "M15", "M5"


def test_no_htf_lookahead_1000_random_steps():
    base_df = load_csv(CSV)
    frames  = build_multi_tf_frames(base_df, base_tf="M5",
                                    primary_tf=PRIMARY_TF,
                                    secondary_tf=SECONDARY_TF,
                                    entry_tf=ENTRY_TF)
    primary_df, secondary_df, entry_df = (
        frames["primary"], frames["secondary"], frames["entry"])

    tf_specs = [
        ("primary",   primary_df,   tf_minutes(PRIMARY_TF)),
        ("secondary", secondary_df, tf_minutes(SECONDARY_TF)),
        ("entry",     entry_df,     tf_minutes(ENTRY_TF)),
    ]
    entry_min = tf_minutes(ENTRY_TF)

    total = len(entry_df)
    rng   = random.Random(SEED)

    checked = 0
    skipped_warmup = 0
    legacy_violations = 0          # steps where harness-v1 slicing had a forming H1 bar
    attempts = 0
    max_attempts = N_STEPS * 20

    while checked < N_STEPS:
        attempts += 1
        assert attempts <= max_attempts, (
            f"could not find {N_STEPS} valid replay steps in {max_attempts} draws "
            f"(checked={checked}) — data/lookback mismatch?")
        i  = rng.randrange(LOOKBACK, total - 1)   # same replay range as strategy_runner
        ts = entry_df.index[i]                     # entry bar OPEN time
        decision_ts = ts + pd.Timedelta(minutes=entry_min)   # its CLOSE = decision time

        wins = {
            name: slice_window(df, decision_ts, LOOKBACK, mins)
            for name, df, mins in tf_specs
        }
        if any(w is None for w in wins.values()):
            skipped_warmup += 1                    # early-history step: runner skips too
            continue

        for name, df, mins in tf_specs:
            w = wins[name]
            assert len(w) == LOOKBACK, f"[{name}] i={i}: window len {len(w)} != {LOOKBACK}"
            assert w.index.is_monotonic_increasing, f"[{name}] i={i}: window not sorted"
            last_close = w.index[-1] + pd.Timedelta(minutes=mins)
            # ── THE pre-registered assertion ──────────────────────────
            assert last_close <= decision_ts, (
                f"[{name}] i={i}: LOOKAHEAD — last bar open {w.index[-1]} "
                f"closes {last_close} > decision time {decision_ts}")

        # No-over-trim regression: the entry window must still end exactly at
        # bar i (the bar whose close IS the decision time).
        assert wins["entry"].index[-1] == ts, (
            f"[entry] i={i}: window ends {wins['entry'].index[-1]}, expected {ts}")

        # Harness-v1 comparison: old rule .loc[:ts] on the H1 frame.
        legacy_last = primary_df.loc[:ts].index[-1]
        if legacy_last + pd.Timedelta(minutes=tf_minutes(PRIMARY_TF)) > decision_ts:
            legacy_violations += 1

        checked += 1

    assert checked == N_STEPS
    assert legacy_violations > 0, (
        "harness-v1 slicing produced zero forming-bar steps in the sample — "
        "the test would not distinguish v1 from v2; investigate")

    print(f"PASS  {checked}/{N_STEPS} random replay steps: every H1/M15/M5 window "
          f"bar closes <= decision time (100%).")
    print(f"      skipped warmup draws: {skipped_warmup} (runner skips these too)")
    print(f"      harness-v1 rule would have included a FORMING H1 bar on "
          f"{legacy_violations}/{N_STEPS} steps ({100.0*legacy_violations/N_STEPS:.1f}%) "
          f"— bug real, now excluded.")


def main() -> int:
    test_no_htf_lookahead_1000_random_steps()
    print("\nOK — mandatory 0.1 lookahead unit test passed 100%.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
