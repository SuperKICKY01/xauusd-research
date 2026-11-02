"""Temporary flag-composition analysis (read-only). Reads a flag-instrumented
report (must contain per-leg `flags` + `timing_flag_count`) and prints:
  STEP 3a — score-75 cohort split into CHoCH-only vs FVG-only.
  STEP 3b — whole >=75 population sliced by persisted timing_flag_count, with
            the >=2-timing-flag counterfactual (leg + signal level t-stats).
Usage: python -m backtest._flag_analysis report_flags.json [more_reports...]
Multiple reports are POOLED (e.g. parallel short-window runs).
"""
import json
import math
import sys
from collections import defaultdict
from statistics import mean, stdev

paths = sys.argv[1:] or ["report_flags.json"]
_raw = []
for p in paths:
    d = json.load(open(p))
    _raw.extend(t for t in d["trades"] if t["score"] >= 75)
# Pool may include OVERLAP from contiguous chunks (pre-roll warmup region).
# Dedup on the trade's natural identity so a trade seen in two chunks counts once.
# A trade opening near a chunk's --end is force-closed at the boundary in that
# chunk but runs to its NATURAL close in the next chunk's pre-roll. Keep the
# version with the LATEST close_ts → always the naturally-closed one.
_best = {}
for t in _raw:
    k = (t["open_ts"], t["direction"], t["score"], t["leg"], round(t["entry_price"], 2))
    if k not in _best or t["close_ts"] > _best[k]["close_ts"]:
        _best[k] = t
tr = list(_best.values())
print(f"[pool] raw legs={len(_raw)}  deduped legs={len(tr)}  (dropped {len(_raw)-len(tr)} overlaps)")

# Outcome vocab differs by engine era: current = WIN_TP1/WIN_TP2/LOSS/BE_HIT/TIMEOUT;
# legacy report_full = TP_HIT/SL_HIT/BE_HIT/TIMEOUT. Treat both.
WIN = {"WIN_TP1", "WIN_TP2", "TP_HIT"}
SLH = {"LOSS", "SL_HIT"}


def has(flags, sub, val=True):
    return any(val == v and sub in k for k, v in flags.items())


def t_of(R):
    n = len(R)
    if n < 2:
        return float("nan"), n
    sd = stdev(R)
    return ((mean(R) / sd * math.sqrt(n)) if sd > 0 else float("nan")), n


def row(rows, label):
    n = len(rows)
    if n == 0:
        return f"{label:<34} n=   0"
    R = [t["rr_realised"] for t in rows]
    pnl = [t["pnl"] for t in rows]
    gw = sum(p for p in pnl if p > 0)
    gl = -sum(p for p in pnl if p < 0)
    pf = gw / gl if gl > 0 else float("inf")
    tp = sum(1 for t in rows if t["outcome"] in WIN) / n * 100
    sl = sum(1 for t in rows if t["outcome"] in SLH) / n * 100
    t, _ = t_of(R)
    return (f"{label:<34} n={n:4d}  TP={tp:4.1f}%  SL={sl:4.1f}%  "
            f"PF={pf:5.3f}  AvgR={mean(R):+.3f}  t={t:+5.2f}")


print("=" * 90)
print(f"FLAG ANALYSIS  ({len(paths)} report(s) pooled)   total >=75 legs = {len(tr)}")
# integrity: persisted timing_flag_count must equal score-bijection {75,85}->1 ; {80,90,100}->2
viol = [(t["score"], t["timing_flag_count"]) for t in tr
        if t["timing_flag_count"] != (1 if t["score"] in (75, 85)
                                      else 2 if t["score"] in (80, 90, 100)
                                      else t["timing_flag_count"])]
print(f"score->timing_flag_count bijection violations: {len(viol)}  {viol[:5]}")
print("=" * 90)

print("\nSTEP 3a — score-75 cohort, split by the single timing flag present:")
s75 = [t for t in tr if t["score"] == 75]
choch = [t for t in s75 if has(t["flags"], "CHoCH")]
fvg = [t for t in s75 if has(t["flags"], "FVG")]
both = [t for t in s75 if has(t["flags"], "CHoCH") and has(t["flags"], "FVG")]
neither = [t for t in s75 if not has(t["flags"], "CHoCH") and not has(t["flags"], "FVG")]
print(row(s75, "  score 75 (all)"))
print(row([t for t in choch if not has(t["flags"], "FVG")], "  75 CHoCH-only"))
print(row([t for t in fvg if not has(t["flags"], "CHoCH")], "  75 FVG-only"))
print(f"  [sanity] both-present at 75: {len(both)}  neither: {len(neither)} "
      f"(decomposition predicts both=0, neither=0)")

print("\nSTEP 3b — whole >=75 sliced by PERSISTED timing_flag_count:")
one = [t for t in tr if t["timing_flag_count"] == 1]
two = [t for t in tr if t["timing_flag_count"] >= 2]
print(row(tr, "  ALL >=75 (unfiltered)"))
print(row(one, "  1 timing flag"))
print(row(two, "  >=2 timing flags"))

# signal-level de-correlation
sig = defaultdict(list)
for t in tr:
    sig[(t["open_ts"], t["direction"], t["score"])].append(t)
sig_rows = [dict(score=k[2], rr=mean(l["rr_realised"] for l in v)) for k, v in sig.items()]


def sig_t(scores):
    R = [r["rr"] for r in sig_rows if r["score"] in scores]
    t, n = t_of(R)
    return t, n


tU, _ = t_of([t["rr_realised"] for t in tr])
tF, _ = t_of([t["rr_realised"] for t in two])
stU, snU = sig_t({75, 80, 85, 90, 100})
stF, snF = sig_t({80, 90, 100})
print("\nCOUNTERFACTUAL — drop all <2-timing-flag legs:")
print(f"  cut {len(one)}/{len(tr)} legs = {len(one)/len(tr)*100:.1f}% of >=75 trades")
print(f"  LEG-LEVEL    t: {tU:+.2f} -> {tF:+.2f}  (dt={tF-tU:+.2f})")
print(f"  SIGNAL-LEVEL t: {stU:+.2f} -> {stF:+.2f}  (dt={stF-stU:+.2f})   "
      f"[n_sig {snU} -> {snF}]")
