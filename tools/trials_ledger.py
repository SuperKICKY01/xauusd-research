"""
tools/trials_ledger.py — append-only trial ledger (Master Spec 1.3).

> TRIAL LEDGER: a file (trials.csv) logging EVERY config variant ever
> backtested (params + result). The ledger count N feeds the DSR
> computation. Deleting rows is forbidden.

This module can only APPEND. It never rewrites, edits, or deletes rows.
Corrections are new rows carrying a `supersedes=<trial_id>` note.
"""

import csv
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

LEDGER_COLUMNS = [
    "trial_id", "timestamp_utc", "phase", "partition", "spec_version",
    "params_json", "fill_model", "risk_pct", "costs_on", "n_signals",
    "expectancy_r_per_signal", "t_stat_per_signal", "pf_per_signal",
    "max_dd_pct", "notes",
]


def next_trial_id(ledger_path: str) -> int:
    """1 + max existing trial_id (rows may be blank-resulted but never absent)."""
    path = Path(ledger_path)
    if not path.exists():
        raise FileNotFoundError(
            f"trials ledger not found at {ledger_path} — it must be "
            f"initialized at spec freeze (Phase 1.3), never auto-created."
        )
    max_id = 0
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                max_id = max(max_id, int(row["trial_id"]))
            except (KeyError, TypeError, ValueError):
                continue
    return max_id + 1


def append_trial(
    ledger_path:   str,
    timestamp_utc: str,
    phase:         str,
    partition:     str,
    spec_version:  str,
    params:        dict,
    fill_model:    str,
    risk_pct:      float,
    costs_on:      bool,
    metrics:       dict,
    notes:         str = "",
) -> int:
    """
    Append one trial row (append mode only — the existing file is never
    rewritten). Result fields come from the canonical per-signal block;
    blank when the run produced no signals (still a ledger row — every
    backtest counts toward N).
    """
    ps = (metrics or {}).get("per_signal") or {}
    trial_id = next_trial_id(ledger_path)
    row = {
        "trial_id":                 trial_id,
        "timestamp_utc":            timestamp_utc,
        "phase":                    phase,
        "partition":                partition,
        "spec_version":             spec_version,
        "params_json":              json.dumps(params, sort_keys=True),
        "fill_model":               fill_model,
        "risk_pct":                 risk_pct,
        "costs_on":                 costs_on,
        "n_signals":                ps.get("signals", 0),
        "expectancy_r_per_signal":  ps.get("expectancy_r", ""),
        "t_stat_per_signal":        ps.get("t_stat_pnl", ""),
        "pf_per_signal":            ps.get("profit_factor", ""),
        "max_dd_pct":               (metrics or {}).get("max_drawdown_pct", ""),
        "notes":                    notes,
    }
    with open(ledger_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=LEDGER_COLUMNS)
        writer.writerow(row)
    logger.info(f"trials_ledger: appended trial_id={trial_id} ({partition}, {notes!r})")
    return trial_id
