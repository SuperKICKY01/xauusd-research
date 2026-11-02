"""
backtest/walk_forward.py — Anchored expanding-window Walk-Forward Validation.

For each window:
  - Slice the base CSV to [train_start .. test_end] so the strategy gets
    full warmup history before the test period begins.
  - Run strategy_runner on the slice (signal_builder is a singleton —
    strategy_runner.run() resets its per-run state).
  - Filter the broker's closed trades to [test_start .. test_end] and
    rebuild a fresh equity curve from `initial_balance`. This rebases
    each test window so the warmup PnL doesn't leak into DD / return %.
  - Compute metrics; flag PASS = (per-SIGNAL t_stat > threshold). Since
    harness-v2 (Phase 0.2) the gate uses per-signal stats — per-leg t is
    inflated up to ~sqrt(2) by correlated dual-TP legs and is reported for
    comparison only.

Under Option A the strategy has no fittable parameters — "train" is purely
warmup observation history. Only test-window trades count toward stats.

Run:
    python -m backtest.walk_forward data/xauusd_m5_full.csv
    python -m backtest walk-forward data/xauusd_m5_full.csv --risk-pct 0.65
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import pandas as pd

from backtest.data_loader     import load_csv
from backtest.metrics         import compute_metrics
from backtest.strategy_runner import strategy_runner

logger = logging.getLogger(__name__)


# Default windows match the project's current WFV spec.
DEFAULT_WINDOWS = [
    ("W1", "2024-01-01", "2024-12-31", "2025-01-01", "2025-06-30"),
    ("W2", "2024-01-01", "2025-06-30", "2025-07-01", "2025-12-31"),
    ("W3", "2024-01-01", "2025-12-31", "2026-01-01", "2026-05-31"),
]


@dataclass
class Window:
    name:        str
    train_start: pd.Timestamp
    train_end:   pd.Timestamp
    test_start:  pd.Timestamp
    test_end:    pd.Timestamp


@dataclass
class WindowResult:
    window:  Window
    metrics: dict
    passed:  bool


def _default_windows() -> list[Window]:
    return [
        Window(
            name        = n,
            train_start = pd.Timestamp(trs),
            train_end   = pd.Timestamp(tre),
            test_start  = pd.Timestamp(tes),
            test_end    = pd.Timestamp(tee),
        )
        for n, trs, tre, tes, tee in DEFAULT_WINDOWS
    ]


def _filter_broker_to_window(
    broker,
    test_start:      pd.Timestamp,
    test_end:        pd.Timestamp,
    initial_balance: float,
) -> SimpleNamespace:
    """
    Return a duck-typed broker stand-in whose `closed`, `equity_curve`, and
    `balance` are scoped to the test window. Equity is rebased to a fresh
    `initial_balance` start so DD / return-% reflect only the test window —
    warmup PnL is not allowed to leak through the equity baseline.
    """
    trades_in = sorted(
        [t for t in broker.closed if test_start <= t.close_ts <= test_end],
        key = lambda t: t.close_ts,
    )

    eq       = initial_balance
    eq_curve = [(test_start, eq)]
    for t in trades_in:
        eq += t.pnl
        eq_curve.append((t.close_ts, eq))

    return SimpleNamespace(
        closed       = trades_in,
        equity_curve = eq_curve,
        balance      = eq,
        # Sizing attrs so compute_metrics' per-signal net-R denominator uses
        # the REAL broker constants instead of silent defaults.
        contract_size        = getattr(broker, "contract_size", 100.0),
        point                = getattr(broker, "point", 0.01),
        slippage_buffer_pips = getattr(broker, "slippage_buffer_pips", 0.0),
    )


def _gate_t(metrics: dict) -> Optional[float]:
    """
    The t-statistic the PASS gate runs on: per-SIGNAL PnL t (canonical since
    harness-v2). Falls back to the legacy per-leg t only when per-signal
    stats are absent (pre-harness-v2 payloads) — logged loudly, because a
    per-leg gate overstates significance.
    """
    ps = metrics.get("per_signal") or {}
    t = ps.get("t_stat_pnl")
    if t is None and metrics.get("t_stat") is not None:
        logger.warning(
            "walk_forward: per-signal t unavailable — gating on LEGACY per-leg "
            "t (inflated up to ~sqrt(2)). Regenerate with harness-v2."
        )
        return metrics.get("t_stat")
    return t


async def _run_window(
    full_df:         pd.DataFrame,
    window:          Window,
    risk_pct:        float,
    initial_balance: float,
    base_tf:         str,
    primary_tf:      str,
    secondary_tf:    str,
    entry_tf:        str,
    be_buffer_pips:  float,
    apply_costs:     bool,
    verbose_every:   int,
    fill_model:      str = "limit",
) -> dict:
    """Run one window, return metrics dict scoped to its test range."""
    slice_df = full_df.loc[window.train_start : window.test_end].copy()

    logger.info(
        f"[{window.name}] slice {window.train_start.date()} .. "
        f"{window.test_end.date()} ({len(slice_df):,} bars) | "
        f"test {window.test_start.date()} .. {window.test_end.date()}"
    )

    broker = await strategy_runner.run(
        base_df         = slice_df,
        base_tf         = base_tf,
        initial_balance = initial_balance,
        risk_pct        = risk_pct,
        be_buffer_pips  = be_buffer_pips,
        primary_tf      = primary_tf,
        secondary_tf    = secondary_tf,
        entry_tf        = entry_tf,
        # Fill model defaults to "limit" (the recorded WFV baseline). "market"
        # mirrors the live execution path (ENTRY_AT_PROXIMAL=False) — opt-in via
        # --fill-model so existing limit results are unchanged.
        fill_model      = fill_model,
        # Costs are off by default to preserve the recorded cost-free WFV
        # baseline (run() now defaults apply_costs=True). Turning them on is a
        # deliberate, separate exercise via --costs — when set, ALL windows are
        # re-measured cost-aware so the t-stats stay internally comparable.
        apply_costs     = apply_costs,
        verbose_every   = verbose_every,
    )

    filtered = _filter_broker_to_window(
        broker, window.test_start, window.test_end, initial_balance,
    )
    return compute_metrics(filtered, initial_balance)


async def run_wfv(
    csv_path:         str,
    windows:          list[Window],
    risk_pct:         float,
    t_stat_threshold: float,
    initial_balance:  float = 100_000.0,
    base_tf:          str   = "M5",
    primary_tf:       str   = "H1",
    secondary_tf:     str   = "M15",
    entry_tf:         str   = "M5",
    be_buffer_pips:   float = 1.0,
    apply_costs:      bool  = False,
    verbose_every:    int   = 0,
    fill_model:       str   = "limit",
) -> list[WindowResult]:
    df = load_csv(csv_path)
    span_start, span_end = df.index[0], df.index[-1]

    for w in windows:
        if w.test_end < span_start or w.train_start > span_end:
            raise ValueError(
                f"[{w.name}] window {w.train_start.date()}..{w.test_end.date()} "
                f"has no overlap with CSV span {span_start}..{span_end}"
            )
        if w.train_start < span_start:
            logger.warning(
                f"[{w.name}] train_start {w.train_start.date()} precedes CSV start "
                f"{span_start}; warmup begins at first available bar."
            )
        if w.test_end > span_end:
            logger.warning(
                f"[{w.name}] test_end {w.test_end.date()} exceeds CSV end "
                f"{span_end}; test truncated to available data."
            )

    results: list[WindowResult] = []
    for w in windows:
        metrics = await _run_window(
            full_df         = df,
            window          = w,
            risk_pct        = risk_pct,
            initial_balance = initial_balance,
            base_tf         = base_tf,
            primary_tf      = primary_tf,
            secondary_tf    = secondary_tf,
            entry_tf        = entry_tf,
            be_buffer_pips  = be_buffer_pips,
            apply_costs     = apply_costs,
            verbose_every   = verbose_every,
            fill_model      = fill_model,
        )
        t  = _gate_t(metrics)
        ok = t is not None and t > t_stat_threshold
        results.append(WindowResult(window=w, metrics=metrics, passed=ok))

        logger.info(
            f"[{w.name}] {'PASS' if ok else 'FAIL'} | "
            f"signals={(metrics.get('per_signal') or {}).get('signals', 0)} "
            f"legs={metrics.get('trades', 0)} "
            f"PF={metrics.get('profit_factor')} "
            f"WR={metrics.get('win_rate_pct')}% "
            f"DD={metrics.get('max_drawdown_pct')}% "
            f"t/sig={t} (per-leg t={metrics.get('t_stat')})"
        )

    return results


def print_report(results: list[WindowResult], threshold: float) -> None:
    print()
    print("=" * 102)
    print(f"  WALK-FORWARD VALIDATION  (PASS threshold: per-SIGNAL t-stat > {threshold})")
    print("=" * 102)
    print(f"  {'W':<4}{'Train':<26}{'Test':<26}"
          f"{'Trades':>8}{'PF':>7}{'Win%':>7}{'MaxDD%':>9}{'t/sig':>10}  Result")
    print("-" * 102)
    for r in results:
        w, m = r.window, r.metrics
        pf  = m.get("profit_factor")
        wr  = m.get("win_rate_pct")
        dd  = m.get("max_drawdown_pct")
        t   = _gate_t(m)
        # Build the date-range strings first; date.__format__ delegates to
        # strftime, so an alignment spec like ":<26" on a date object would
        # be printed literally (e.g. "<26") rather than padding the value.
        train_str = f"{w.train_start.date()} -> {w.train_end.date()}"
        test_str  = f"{w.test_start.date()} -> {w.test_end.date()}"
        print(
            f"  {w.name:<4}"
            f"{train_str:<26}"
            f"{test_str:<26}"
            f"{m.get('trades', 0):>8}"
            f"{(pf if pf is not None else 0):>7.2f}"
            f"{(wr if wr is not None else 0):>6.1f}%"
            f"{(dd if dd is not None else 0):>8.2f}%"
            f"{(t  if t  is not None else 0):>10.2f}"
            f"   {'PASS' if r.passed else 'FAIL'}"
        )
    print("=" * 102)
    all_pass = all(r.passed for r in results)
    print(f"  Verdict: {'ALL WINDOWS PASS' if all_pass else 'FAIL'}")
    if not all_pass:
        fails = ", ".join(r.window.name for r in results if not r.passed)
        print(f"  Failed: {fails}")
    print("=" * 102)
    print()


def write_json(results: list[WindowResult], threshold: float, path: str) -> None:
    payload = {
        "t_stat_threshold": threshold,
        "all_passed":       all(r.passed for r in results),
        "windows": [
            {
                "name":        r.window.name,
                "train_start": r.window.train_start.isoformat(),
                "train_end":   r.window.train_end.isoformat(),
                "test_start":  r.window.test_start.isoformat(),
                "test_end":    r.window.test_end.isoformat(),
                "passed":      r.passed,
                "metrics":     r.metrics,
            }
            for r in results
        ],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)
    logger.info(f"walk_forward: JSON report written -> {path}")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog        = "python -m backtest.walk_forward",
        description = "Anchored expanding-window Walk-Forward Validation.",
    )
    p.add_argument("csv_path",          help="Path to OHLCV CSV (see backtest/data_loader.py).")
    p.add_argument("--risk-pct",        type=float, default=0.65,
                   help="Risk per trade, %% of balance. Default: 0.65.")
    p.add_argument("--t-stat-threshold",type=float, default=3.0,
                   help="Per-window PASS threshold on per-trade PnL t-stat. Default: 3.0.")
    p.add_argument("--initial-balance", type=float, default=100_000.0)
    p.add_argument("--base-tf",         default="M5")
    p.add_argument("--primary-tf",      default="H1")
    p.add_argument("--secondary-tf",    default="M15")
    p.add_argument("--entry-tf",        default="M5")
    p.add_argument("--be-buffer-pips",  type=float, default=1.0)
    p.add_argument("--costs",           action="store_true",
                   help="Model XM commission + swap (CFG.costs) in every window. "
                        "Default off = cost-free baseline. When set, all windows "
                        "are re-measured cost-aware so t-stats stay comparable.")
    p.add_argument("--fill-model",      choices=["limit", "market"], default="limit",
                   help="Order fill model for every window: 'limit' (proximal "
                        "pending, recorded WFV baseline) or 'market' (mid + "
                        "next-bar open, mirrors live execution). Default: limit.")
    p.add_argument("--report-json",     default=None, help="Optional path for full JSON report.")
    p.add_argument("--quiet",           action="store_true")
    p.add_argument("--sequential",      action="store_true",
                   help="Run windows sequentially in-process. Default: parallel subprocesses.")
    # Internal child-mode flags — hidden, used by parent to fan out to children.
    p.add_argument("--internal-window-spec", default=None, help=argparse.SUPPRESS)
    p.add_argument("--internal-out-json",    default=None, help=argparse.SUPPRESS)
    return p.parse_args(argv)


def _child_run_one(args: argparse.Namespace) -> int:
    """
    Child-mode entry: run exactly one window and write its metrics dict
    to args.internal_out_json. Used by _parallel_run for subprocess fan-out.
    """
    parts = args.internal_window_spec.split("|")
    if len(parts) != 5:
        print(f"ERROR: bad --internal-window-spec: {args.internal_window_spec}",
              file=sys.stderr)
        return 2
    name, ts, te, tts, tte = parts
    window = Window(
        name        = name,
        train_start = pd.Timestamp(ts),
        train_end   = pd.Timestamp(te),
        test_start  = pd.Timestamp(tts),
        test_end    = pd.Timestamp(tte),
    )

    async def _do() -> dict:
        df = load_csv(args.csv_path)
        return await _run_window(
            full_df         = df,
            window          = window,
            risk_pct        = args.risk_pct,
            initial_balance = args.initial_balance,
            base_tf         = args.base_tf,
            primary_tf      = args.primary_tf,
            secondary_tf    = args.secondary_tf,
            entry_tf        = args.entry_tf,
            be_buffer_pips  = args.be_buffer_pips,
            apply_costs     = args.costs,
            verbose_every   = 0 if args.quiet else 2000,
            fill_model      = args.fill_model,
        )

    metrics = asyncio.run(_do())
    payload = {
        "window": {
            "name":        window.name,
            "train_start": window.train_start.isoformat(),
            "train_end":   window.train_end.isoformat(),
            "test_start":  window.test_start.isoformat(),
            "test_end":    window.test_end.isoformat(),
        },
        "metrics": metrics,
    }
    out_path = Path(args.internal_out_json)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)
    return 0


def _parallel_run(
    args:    argparse.Namespace,
    windows: list[Window],
) -> list[WindowResult]:
    """
    Launch each window as a separate Python subprocess (signal_builder is a
    module-level singleton — only safe across processes, not threads).

    Children stream stdout/stderr to wfv_logs/wfv_<window>.log and write
    metrics JSON into a tempdir. Parent waits with periodic heartbeat,
    then merges results.
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="wfv_"))
    log_dir = Path("wfv_logs")
    log_dir.mkdir(exist_ok=True)

    children: list[tuple[Window, subprocess.Popen, Path, Path]] = []
    try:
        for w in windows:
            spec = "|".join([
                w.name,
                w.train_start.isoformat(),
                w.train_end.isoformat(),
                w.test_start.isoformat(),
                w.test_end.isoformat(),
            ])
            out_json = tmp_dir / f"{w.name}.json"
            log_path = log_dir / f"wfv_{w.name}.log"

            cmd = [
                sys.executable, "-m", "backtest.walk_forward",
                args.csv_path,
                "--risk-pct",             str(args.risk_pct),
                "--initial-balance",      str(args.initial_balance),
                "--base-tf",              args.base_tf,
                "--primary-tf",           args.primary_tf,
                "--secondary-tf",         args.secondary_tf,
                "--entry-tf",             args.entry_tf,
                "--be-buffer-pips",       str(args.be_buffer_pips),
                "--fill-model",           args.fill_model,
                "--internal-window-spec", spec,
                "--internal-out-json",    str(out_json),
            ]
            if args.costs:
                cmd.append("--costs")
            log_f = open(log_path, "w", encoding="utf-8")
            proc  = subprocess.Popen(
                cmd,
                stdout = log_f,
                stderr = subprocess.STDOUT,
                creationflags = (
                    subprocess.CREATE_NEW_PROCESS_GROUP
                    if sys.platform == "win32" else 0
                ),
            )
            logger.info(f"  launched {w.name}  PID={proc.pid}  log={log_path}")
            children.append((w, proc, out_json, log_path))

        logger.info(f"Waiting for {len(children)} child processes "
                    f"(heartbeat every 5 min)...")
        t0 = time.time()
        next_beat = t0 + 300
        while any(p.poll() is None for _, p, _, _ in children):
            time.sleep(15)
            if time.time() >= next_beat:
                running = [w.name for w, p, _, _ in children if p.poll() is None]
                done    = [w.name for w, p, _, _ in children if p.poll() is not None]
                logger.info(
                    f"  [heartbeat +{(time.time()-t0)/60:.1f}min] "
                    f"running={running}  done={done}"
                )
                next_beat += 300

        results: list[WindowResult] = []
        for w, p, out_json, log_path in children:
            if p.returncode != 0:
                raise RuntimeError(
                    f"[{w.name}] child exit {p.returncode}; see {log_path}"
                )
            with open(out_json, "r", encoding="utf-8") as f:
                payload = json.load(f)
            metrics = payload["metrics"]
            t  = _gate_t(metrics)
            ok = t is not None and t > args.t_stat_threshold
            results.append(WindowResult(window=w, metrics=metrics, passed=ok))
            logger.info(
                f"[{w.name}] {'PASS' if ok else 'FAIL'} | "
                f"signals={(metrics.get('per_signal') or {}).get('signals', 0)} "
                f"legs={metrics.get('trades', 0)} "
                f"PF={metrics.get('profit_factor')} "
                f"WR={metrics.get('win_rate_pct')}% "
                f"DD={metrics.get('max_drawdown_pct')}% "
                f"t/sig={t} (per-leg t={metrics.get('t_stat')})"
            )
        return results

    except KeyboardInterrupt:
        logger.warning("Interrupted — terminating children...")
        for _, p, _, _ in children:
            if p.poll() is None:
                p.terminate()
        raise
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    logging.basicConfig(
        level  = logging.WARNING if args.quiet else logging.INFO,
        format = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )

    # Suppress per-signal noise from quant.signal_builder during the WFV run.
    # Restored in finally; in child mode the process exits right after so the
    # restore is effectively a no-op there.
    sig_logger = logging.getLogger("quant.signal_builder")
    prev_level = sig_logger.level
    sig_logger.setLevel(logging.WARNING)

    try:
        # ── Child mode ────────────────────────────────────────────
        if args.internal_window_spec is not None:
            if args.internal_out_json is None:
                print("ERROR: --internal-window-spec requires --internal-out-json",
                      file=sys.stderr)
                return 2
            return _child_run_one(args)

        # ── Parent mode ───────────────────────────────────────────
        windows = _default_windows()
        try:
            if args.sequential:
                logger.info("Running windows sequentially (in-process).")
                results = asyncio.run(run_wfv(
                    csv_path         = args.csv_path,
                    windows          = windows,
                    risk_pct         = args.risk_pct,
                    t_stat_threshold = args.t_stat_threshold,
                    initial_balance  = args.initial_balance,
                    base_tf          = args.base_tf,
                    primary_tf       = args.primary_tf,
                    secondary_tf     = args.secondary_tf,
                    entry_tf         = args.entry_tf,
                    be_buffer_pips   = args.be_buffer_pips,
                    apply_costs      = args.costs,
                    verbose_every    = 0 if args.quiet else 2000,
                    fill_model       = args.fill_model,
                ))
            else:
                logger.info(f"Running {len(windows)} windows in parallel as subprocesses.")
                results = _parallel_run(args, windows)
        except KeyboardInterrupt:
            print("\nInterrupted.", file=sys.stderr)
            return 130
        except (FileNotFoundError, ValueError, RuntimeError) as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2

        print_report(results, args.t_stat_threshold)
        if args.report_json:
            write_json(results, args.t_stat_threshold, args.report_json)
        return 0 if all(r.passed for r in results) else 1

    finally:
        sig_logger.setLevel(prev_level)


if __name__ == "__main__":
    raise SystemExit(main())
