"""
backtest/zone_cli.py — CLI for the zone-strategy backtest (Phase 2).

    python -m backtest.zone_cli data/xauusd_m5_full.csv --partition DEV \
        --start 2024-01-01 --end 2025-01-01 --report-json report_zone_dev_t1.json

Every run on a real CSV is APPENDED to trials.csv automatically (Master
Spec 1.3 — the ledger counts every backtest; there is deliberately no
opt-out flag). Zone-parameter overrides are allowed ONLY on the DEV
partition; EVAL/FRESH partitions refuse any non-frozen override.
"""

import argparse
import logging
import sys
from datetime import datetime, timezone

from backtest.data_loader import load_csv
from backtest.metrics import compute_metrics, print_report, write_json_report
from backtest.zone_runner import run_zone_backtest
from tools.trials_ledger import append_trial

# Frozen v1.0 values (specs/phase1_zone_spec_v1.md §1.1).
FROZEN = {
    "atr_period":           14,
    "impulse_atr_mult":     1.5,
    "impulse_confirm_bars": 3,
    "min_height_atr":       0.05,
    "max_height_atr":       2.0,
    "max_age_bars":         72,
    "sl_atr_mult":          0.2,
    "tp1_r":                1.0,
    "tp2_r":                2.0,
    "risk_pct":             0.5,
}

_EVAL_PARTITIONS = ("EVAL-1", "EVAL-2", "EVAL-3", "FRESH-OOS")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m backtest.zone_cli",
        description="Zone-strategy backtest (frozen spec v1.0, harness-v2, "
                    "market fill, per-signal stats).",
    )
    p.add_argument("csv_path")
    p.add_argument("--partition", required=True,
                   help="Ledger partition label: DEV | EVAL-1 | EVAL-2 | "
                        "EVAL-3 | FRESH-OOS | SMOKE (SMOKE = tiny slices "
                        "for pipeline checks; still ledgered).")
    p.add_argument("--start", default=None, help="ISO start cutoff (inclusive).")
    p.add_argument("--end",   default=None, help="ISO end cutoff (exclusive-ish, pandas .loc).")
    p.add_argument("--initial-balance", type=float, default=100_000.0)
    p.add_argument("--risk-pct",        type=float, default=FROZEN["risk_pct"])
    p.add_argument("--no-costs",        action="store_true")
    p.add_argument("--report-json",     default=None)
    p.add_argument("--quiet",           action="store_true")
    p.add_argument("--note",            default="", help="Free-text ledger note.")
    p.add_argument("--ledger",          default="trials.csv")
    # DEV-tunable zone params (Category B, spec Appendix A). Defaults = frozen.
    p.add_argument("--impulse-atr-mult",     type=float, default=FROZEN["impulse_atr_mult"])
    p.add_argument("--impulse-confirm-bars", type=int,   default=FROZEN["impulse_confirm_bars"])
    p.add_argument("--min-height-atr",       type=float, default=FROZEN["min_height_atr"])
    p.add_argument("--max-height-atr",       type=float, default=FROZEN["max_height_atr"])
    p.add_argument("--max-age-bars",         type=int,   default=FROZEN["max_age_bars"])
    p.add_argument("--sl-atr-mult",          type=float, default=FROZEN["sl_atr_mult"])
    p.add_argument("--tp1-r",                type=float, default=FROZEN["tp1_r"])
    p.add_argument("--tp2-r",                type=float, default=FROZEN["tp2_r"])
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )

    params = {
        "atr_period":           FROZEN["atr_period"],
        "impulse_atr_mult":     args.impulse_atr_mult,
        "impulse_confirm_bars": args.impulse_confirm_bars,
        "min_height_atr":       args.min_height_atr,
        "max_height_atr":       args.max_height_atr,
        "max_age_bars":         args.max_age_bars,
        "sl_atr_mult":          args.sl_atr_mult,
        "tp1_r":                args.tp1_r,
        "tp2_r":                args.tp2_r,
        "risk_pct":             args.risk_pct,
    }
    deviations = {k: (FROZEN[k], v) for k, v in params.items() if v != FROZEN[k]}

    # EVAL discipline: the frozen spec sees the EVAL partitions exactly once,
    # with exactly the frozen configuration. Overrides refuse to run there.
    if args.partition in _EVAL_PARTITIONS and deviations:
        print(f"ERROR: partition {args.partition} accepts ONLY the frozen "
              f"spec; overridden: {sorted(deviations)}.", file=sys.stderr)
        return 2

    df = load_csv(args.csv_path)
    if args.start:
        df = df.loc[args.start:]
    if args.end:
        df = df.loc[:args.end]
    if len(df) < 300:
        print(f"ERROR: too few bars after filtering ({len(df)}).", file=sys.stderr)
        return 2

    engine_params = {k: v for k, v in params.items()
                     if k not in ("tp1_r", "tp2_r", "risk_pct")}
    broker, engine, funnel = run_zone_backtest(
        base_df         = df,
        initial_balance = args.initial_balance,
        risk_pct        = args.risk_pct,
        apply_costs     = not args.no_costs,
        tp1_r           = args.tp1_r,
        tp2_r           = args.tp2_r,
        engine_params   = engine_params,
        verbose_every   = 0 if args.quiet else 20_000,
    )

    metrics = compute_metrics(broker, args.initial_balance)
    metrics["zone_funnel"] = funnel
    metrics["data_window"] = {
        "csv":   args.csv_path,
        "start": str(df.index[0]),
        "end":   str(df.index[-1]),
        "bars":  len(df),
    }
    print_report(metrics)
    print("  Zone funnel:")
    for k, v in funnel.items():
        print(f"    {k:<22}{v:>10}")
    print()

    if args.report_json:
        write_json_report(metrics, broker, args.report_json)

    trial_id = append_trial(
        ledger_path   = args.ledger,
        timestamp_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        phase         = "P2",
        partition     = args.partition,
        spec_version  = "zone-v1.0",
        params        = params,
        fill_model    = "market",
        risk_pct      = args.risk_pct,
        costs_on      = not args.no_costs,
        metrics       = metrics,
        notes         = args.note or ("frozen" if not deviations else
                                      "deviates:" + ",".join(sorted(deviations))),
    )
    print(f"  trials.csv: appended trial_id={trial_id} "
          f"(partition={args.partition})\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
