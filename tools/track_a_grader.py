"""
tools/track_a_grader.py — TRACK A: forward-log grader for the source
trader's posted zone calls (Master Spec v1.0, section 7).

Input CSV schema (one row per posted trade call):
    trade_id, post_timestamp, asset, tf, direction, market_price_at_post,
    zone_top, zone_bottom, sl_stated, tp_stated, resolution,
    resolve_timestamp, r_multiple, note
  • direction: BUY | SELL (case-insensitive; LONG/SHORT accepted).
  • sl_stated / tp_stated: price, or "N/A" (missing SL ⇒ UNGRADEABLE — the
    ungradeable RATE is itself a pre-registered finding).
  • resolution / resolve_timestamp / r_multiple: the POSTER's claims —
    ignored for grading (we re-resolve from price), echoed for comparison.

Pre-registered resolution rules (fixed BEFORE any real log is graded; the
unit test tests/test_track_a_grader.py pins each rule):
  R1 ENTRY. From post_timestamp forward (bars whose close time > post ts):
     • price already inside [zone_bottom, zone_top] at post → entry at
       market_price_at_post, effective immediately;
     • else entry triggers on the FIRST bar whose range touches the zone's
       proximal edge (the zone boundary on the side price is approaching
       from); entry price = that proximal edge, or the bar's OPEN when it
       gaps through the edge (worse fill — honest).
     • zone never touched before end of data → NO_TRIGGER (not a trade;
       excluded from expectancy, count reported).
  R2 EXIT. From the entry bar (inclusive) forward, FIRST TOUCH decides:
     • SL touched before TP → LOSS (−1R). TP before SL → WIN (+R_planned).
     • BOTH inside one bar's range (incl. the entry bar) → pessimistic:
       SL first ⇒ LOSS. (House convention — see
       .claude/skills/quant-trading/references/intra-bar-fills.md.)
     • Gap-open beyond SL/TP → filled at that bar's OPEN (can make a loss
       worse than −1R or a win better than +R_planned).
     • Neither hit by end of data → UNRESOLVED (excluded from expectancy,
       count reported).
  R3 COSTS. Graded R is NET of typical retail XAUUSD friction:
     • effective spread: entry worsened by SPREAD_PRICE = 0.25 (the house
       PFOF/internalization number, backtest/strategy_runner.py);
     • commission: $7/lot round-turn expressed in R via the same sizing
       arithmetic the house uses: r_cost = 7 / (sl_distance_price × 100).
  R4 SCOPE. Rows whose asset has no OHLC provided → UNGRADEABLE_ASSET.
     Rows with missing/invalid SL → UNGRADEABLE_NO_SL.

Pre-registered read (spec section 7, verbatim):
    "n>=30 gradeable AND expectancy > +0.2R -> 'the source trader plausibly
     has edge' ... Otherwise -> the visible layer of his feed carries no
     measurable edge; only Phase 1-4 evidence counts."

Run:
    .\\venv\\Scripts\\python.exe -m tools.track_a_grader posts.csv ^
        --ohlc XAUUSD=data\\xauusd_m5_full.csv --report-json track_a_report.json
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from dataclasses import dataclass, asdict
from typing import Optional

import pandas as pd

from backtest.data_loader import load_csv

logger = logging.getLogger(__name__)

SPREAD_PRICE       = 0.25    # effective one-sided spread, XAUUSD price units
COMMISSION_PER_LOT = 7.0     # $ round-turn / lot
CONTRACT           = 100.0   # oz / lot — $ per 1.0 price move per lot

GRADEABLE_OUTCOMES = ("WIN", "LOSS")


@dataclass
class GradedRow:
    trade_id:        str
    asset:           str
    direction:       str
    post_ts:         str
    outcome:         str              # WIN | LOSS | NO_TRIGGER | UNRESOLVED |
                                      # UNGRADEABLE_NO_SL | UNGRADEABLE_ASSET | INVALID_ROW
    entry_price:     Optional[float] = None
    entry_ts:        Optional[str]   = None
    exit_price:      Optional[float] = None
    exit_ts:         Optional[str]   = None
    planned_r:       Optional[float] = None   # (tp-entry)/(entry-sl), direction-signed
    net_r:           Optional[float] = None   # realized R net of spread+commission
    poster_claim:    str             = ""     # echoed resolution column
    note:            str             = ""


def _norm_direction(raw: str) -> Optional[str]:
    d = str(raw).strip().upper()
    if d in ("BUY", "LONG"):   return "BUY"
    if d in ("SELL", "SHORT"): return "SELL"
    return None


def _price(raw) -> Optional[float]:
    """Parse a price cell; N/A / blank / non-numeric → None."""
    s = str(raw).strip()
    if not s or s.upper() in ("N/A", "NA", "NONE", "-", "NAN"):
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return v if math.isfinite(v) else None


def grade_row(row: dict, ohlc: Optional[pd.DataFrame]) -> GradedRow:
    """Resolve one posted call against M5 OHLC per rules R1–R4."""
    out = GradedRow(
        trade_id     = str(row.get("trade_id", "")).strip(),
        asset        = str(row.get("asset", "")).strip().upper(),
        direction    = str(row.get("direction", "")).strip().upper(),
        post_ts      = str(row.get("post_timestamp", "")).strip(),
        outcome      = "INVALID_ROW",
        poster_claim = str(row.get("resolution", "")).strip(),
        note         = str(row.get("note", "")).strip(),
    )

    direction = _norm_direction(row.get("direction", ""))
    post_ts   = pd.to_datetime(str(row.get("post_timestamp", "")), errors="coerce")
    zone_top  = _price(row.get("zone_top"))
    zone_bot  = _price(row.get("zone_bottom"))
    mkt       = _price(row.get("market_price_at_post"))
    sl        = _price(row.get("sl_stated"))
    tp        = _price(row.get("tp_stated"))

    if direction is None or pd.isna(post_ts) or zone_top is None or zone_bot is None \
            or mkt is None or tp is None:
        return out
    out.direction = direction
    if zone_bot > zone_top:
        zone_bot, zone_top = zone_top, zone_bot

    # R4 — ungradeable classes (checked in pre-registered order: SL first,
    # so a no-SL row is counted against the trader even if we also lack data).
    if sl is None:
        out.outcome = "UNGRADEABLE_NO_SL"
        return out
    if ohlc is None:
        out.outcome = "UNGRADEABLE_ASSET"
        return out

    # Bars strictly after the post moment: a bar is usable once it has
    # CLOSED after post_ts. Conservative: skip the bar forming at post time
    # (the poster couldn't have been filled on its already-elapsed part).
    df = ohlc.loc[ohlc.index > post_ts]
    if len(df) == 0:
        out.outcome = "UNRESOLVED"
        return out

    # ── R1: entry ────────────────────────────────────────────────────
    if zone_bot <= mkt <= zone_top:
        entry_raw, entry_i = mkt, 0
    else:
        proximal = zone_top if mkt > zone_top else zone_bot
        entry_raw = None
        entry_i   = None
        for i, (ts, bar) in enumerate(df.iterrows()):
            o, h, l = float(bar["Open"]), float(bar["High"]), float(bar["Low"])
            if l <= proximal <= h:
                # Gap through the edge on the open → filled at the open.
                gapped = (o < proximal) if mkt > zone_top else (o > proximal)
                entry_raw = o if gapped else proximal
                entry_i   = i
                break
        if entry_raw is None:
            out.outcome = "NO_TRIGGER"
            return out

    # R3: spread — worsen the entry against the trade.
    entry = entry_raw + SPREAD_PRICE if direction == "BUY" else entry_raw - SPREAD_PRICE
    out.entry_price = round(entry, 3)
    out.entry_ts    = str(df.index[entry_i])

    sl_dist = (entry - sl) if direction == "BUY" else (sl - entry)
    tp_dist = (tp - entry) if direction == "BUY" else (entry - tp)
    if sl_dist <= 0 or tp_dist <= 0:
        # SL on the wrong side of the actual fill (or TP already passed) —
        # the posted geometry is incoherent at fill time. Not gradeable as
        # a first-touch race.
        out.outcome = "INVALID_ROW"
        out.note = (out.note + " | geometry invalid at fill "
                    f"(sl_dist={sl_dist:.3f}, tp_dist={tp_dist:.3f})").strip(" |")
        return out
    out.planned_r = round(tp_dist / sl_dist, 3)
    r_commission  = COMMISSION_PER_LOT / (sl_dist * CONTRACT)   # R3 commission in R

    # ── R2: first-touch race from the entry bar (inclusive) ─────────
    for ts, bar in df.iloc[entry_i:].iterrows():
        o, h, l = float(bar["Open"]), float(bar["High"]), float(bar["Low"])
        sl_hit = (l <= sl <= h)
        tp_hit = (l <= tp <= h)
        # Gap-open beyond a level → that bar's open IS the fill.
        if direction == "BUY":
            if o <= sl: sl_hit, sl_px = True, o
            else:       sl_px = sl
            if o >= tp: tp_hit, tp_px = True, o
            else:       tp_px = tp
        else:
            if o >= sl: sl_hit, sl_px = True, o
            else:       sl_px = sl
            if o <= tp: tp_hit, tp_px = True, o
            else:       tp_px = tp

        if sl_hit:                                  # pessimistic: SL first
            move = (sl_px - entry) if direction == "BUY" else (entry - sl_px)
            out.outcome, out.exit_price, out.exit_ts = "LOSS", round(sl_px, 3), str(ts)
            out.net_r = round(move / sl_dist - r_commission, 4)
            return out
        if tp_hit:
            move = (tp_px - entry) if direction == "BUY" else (entry - tp_px)
            out.outcome, out.exit_price, out.exit_ts = "WIN", round(tp_px, 3), str(ts)
            out.net_r = round(move / sl_dist - r_commission, 4)
            return out

    out.outcome = "UNRESOLVED"
    return out


def grade_file(posts_csv: str, ohlc_by_asset: dict[str, pd.DataFrame]) -> dict:
    posts = pd.read_csv(posts_csv, dtype=str).fillna("")
    graded: list[GradedRow] = []
    for _, row in posts.iterrows():
        asset = str(row.get("asset", "")).strip().upper()
        graded.append(grade_row(row.to_dict(), ohlc_by_asset.get(asset)))

    counts: dict[str, int] = {}
    for g in graded:
        counts[g.outcome] = counts.get(g.outcome, 0) + 1

    gradeable = [g for g in graded if g.outcome in GRADEABLE_OUTCOMES]
    n  = len(gradeable)
    rs = [g.net_r for g in gradeable if g.net_r is not None]
    wins = sum(1 for g in gradeable if g.outcome == "WIN")

    n_rows = len(graded)
    n_ungradeable = counts.get("UNGRADEABLE_NO_SL", 0)
    expectancy = (sum(rs) / len(rs)) if rs else None

    # Pre-registered read (spec section 7).
    verdict = (
        "PASS: source trader plausibly has edge — his rules deserve "
        "Phase-1 respec priority"
        if (n >= 30 and expectancy is not None and expectancy > 0.2)
        else "FAIL: the visible layer of the feed carries no measurable "
             "edge; only Phase 1-4 evidence counts"
    )

    return {
        "rows_total":            n_rows,
        "outcome_counts":        dict(sorted(counts.items())),
        "ungradeable_no_sl_rate_pct":
            round(100.0 * n_ungradeable / n_rows, 1) if n_rows else None,
        "n_gradeable":           n,
        "hit_rate_pct":          round(100.0 * wins / n, 1) if n else None,
        "expectancy_net_r":      round(expectancy, 4) if expectancy is not None else None,
        "gate_text": "n>=30 gradeable AND expectancy > +0.2R",
        "gate_inputs": {"n_gradeable": n,
                        "expectancy_net_r": round(expectancy, 4) if expectancy is not None else None},
        "verdict": verdict,
        "cost_model": {"spread_price": SPREAD_PRICE,
                       "commission_per_lot_rt": COMMISSION_PER_LOT,
                       "contract_size": CONTRACT},
        "rows": [asdict(g) for g in graded],
    }


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m tools.track_a_grader",
        description="TRACK A — first-touch grader for posted zone calls.")
    p.add_argument("posts_csv", help="CSV of posted calls (see module docstring).")
    p.add_argument("--ohlc", action="append", required=True,
                   metavar="ASSET=CSV",
                   help="Asset→M5 OHLC mapping, repeatable "
                        "(e.g. --ohlc XAUUSD=data\\xauusd_m5_full.csv).")
    p.add_argument("--report-json", default=None)
    args = p.parse_args(argv if argv is not None else sys.argv[1:])

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")

    ohlc_by_asset: dict[str, pd.DataFrame] = {}
    for spec in args.ohlc:
        asset, _, path = spec.partition("=")
        if not path:
            print(f"ERROR: bad --ohlc '{spec}' (expected ASSET=path.csv)", file=sys.stderr)
            return 2
        ohlc_by_asset[asset.strip().upper()] = load_csv(path)

    report = grade_file(args.posts_csv, ohlc_by_asset)

    print("\n" + "=" * 64)
    print("  TRACK A — FORWARD LOG GRADER")
    print("=" * 64)
    print(f"  Rows                : {report['rows_total']}")
    for k, v in report["outcome_counts"].items():
        print(f"    {k:<18}: {v}")
    print(f"  Ungradeable (no SL) : {report['ungradeable_no_sl_rate_pct']}%")
    print(f"  n gradeable         : {report['n_gradeable']}")
    print(f"  Hit rate            : {report['hit_rate_pct']}%")
    print(f"  Expectancy (net R)  : {report['expectancy_net_r']}")
    print(f"  Gate                : {report['gate_text']}")
    print(f"  Verdict             : {report['verdict']}")
    print("=" * 64 + "\n")

    if args.report_json:
        with open(args.report_json, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, default=str)
        logger.info(f"track_a_grader: report written -> {args.report_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
