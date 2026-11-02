"""
backtest/data_loader.py — CSV ingest and timeframe resampling.

Accepts a single CSV at any base timeframe (default M5) and produces three
DataFrames for signal_builder: primary (H1), secondary (M15), entry (M5).
This means one input file can drive the full multi-TF pipeline without
needing to source three separate exports.

Expected CSV columns (case-insensitive, common MT5 / TradingView formats):
    timestamp | datetime | time   → DatetimeIndex
    open  high  low  close        → required
    volume                        → optional, defaults to 0

Output DataFrames carry capitalised columns (Open/High/Low/Close/Volume) to
match the convention used by signal_builder (`df["Close"].iloc[-1]`).
"""

import logging
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

# Pandas resample rules per timeframe label. Only the ones signal_builder
# actually consumes are listed.
_TF_RULES = {
    "M1":  "1min",
    "M5":  "5min",
    "M15": "15min",
    "M30": "30min",
    "H1":  "1h",
    "H4":  "4h",
    "D1":  "1D",
}

_TF_MINUTES = {
    "M1": 1, "M5": 5, "M15": 15, "M30": 30,
    "H1": 60, "H4": 240, "D1": 1440,
}


def tf_minutes(tf: str) -> int:
    """Bar duration in minutes for a timeframe label (raises on unknown)."""
    try:
        return _TF_MINUTES[tf.upper()]
    except KeyError:
        raise ValueError(f"tf_minutes: unsupported timeframe '{tf}'") from None


def load_csv(path: str) -> pd.DataFrame:
    """
    Load a single OHLCV CSV. Returns a DataFrame indexed by tz-naive UTC
    timestamps with capitalised Open/High/Low/Close/Volume columns.

    Handles three common formats automatically:
      • Comma-delimited TradingView / generic export:
            timestamp,open,high,low,close,volume
      • Tab-delimited MT5 export (with angle-bracketed headers):
            <DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\t<VOL>\t<SPREAD>
      • Anything in between — delimiter is sniffed via pandas' python engine.
    """
    # ── Sniff delimiter — handles comma, tab, semicolon, pipe ────
    df = pd.read_csv(path, sep=None, engine="python")

    # ── Normalise column names ───────────────────────────────────
    # Strip whitespace, angle brackets (MT5 style), then lowercase.
    df.columns = [
        c.strip().strip("<>").strip().lower()
        for c in df.columns
    ]

    # ── Locate / build a timestamp column ────────────────────────
    ts_series: Optional[pd.Series] = None

    combined_col = next(
        (c for c in ("timestamp", "datetime") if c in df.columns), None,
    )
    if combined_col is not None:
        ts_series = pd.to_datetime(df[combined_col], utc=False, errors="coerce")
    elif "date" in df.columns and "time" in df.columns:
        # MT5 splits into two columns — combine them.
        ts_series = pd.to_datetime(
            df["date"].astype(str).str.strip() + " " +
            df["time"].astype(str).str.strip(),
            utc=False, errors="coerce",
        )
    elif "date" in df.columns:
        ts_series = pd.to_datetime(df["date"], utc=False, errors="coerce")

    if ts_series is None:
        raise ValueError(
            f"data_loader: no timestamp column found in {path}. "
            f"Expected one of: timestamp, datetime, (date+time), date. "
            f"Got columns: {list(df.columns)}"
        )

    df["__ts__"] = ts_series
    df = df.dropna(subset=["__ts__"]).set_index("__ts__").sort_index()

    # ── Map OHLC columns ─────────────────────────────────────────
    rename_map = {}
    for src, dst in [("open", "Open"), ("high", "High"),
                     ("low", "Low"),   ("close", "Close")]:
        if src not in df.columns:
            raise ValueError(
                f"data_loader: required column '{src}' missing in {path}. "
                f"Got columns: {list(df.columns)}"
            )
        rename_map[src] = dst

    # Retail gold/forex reports real volume ("vol"/"real_volume") as 0 — only
    # tick volume carries information, and the LIVE path (core/data_stream.py)
    # maps tick_volume → Volume. Prefer tick volume so the backtest matches
    # live; fall back to a generic "volume" column (e.g. TradingView tick-vol),
    # then real volume only if nothing else is present.
    if "tick_volume" in df.columns:
        rename_map["tick_volume"] = "Volume"
    elif "tickvol" in df.columns:
        rename_map["tickvol"] = "Volume"
    elif "volume" in df.columns:
        rename_map["volume"] = "Volume"
    elif "vol" in df.columns:
        rename_map["vol"] = "Volume"

    df = df.rename(columns=rename_map)

    if "Volume" not in df.columns:
        df["Volume"] = 0.0

    df = df[["Open", "High", "Low", "Close", "Volume"]].astype(float)
    df.index.name = None

    logger.info(
        f"data_loader: loaded {len(df)} bars from {path} "
        f"({df.index[0]} → {df.index[-1]})"
    )
    return df


def resample(df: pd.DataFrame, target_tf: str) -> pd.DataFrame:
    """
    Aggregate a base-TF dataframe up to a higher TF.
    No-op (returns df) when target_tf matches the base inferred from index spacing.
    """
    rule = _TF_RULES.get(target_tf.upper())
    if rule is None:
        raise ValueError(f"resample: unsupported timeframe '{target_tf}'")

    out = df.resample(rule, label="left", closed="left").agg({
        "Open":   "first",
        "High":   "max",
        "Low":    "min",
        "Close":  "last",
        "Volume": "sum",
    }).dropna(subset=["Open", "High", "Low", "Close"])
    return out


def build_multi_tf_frames(
    base_df:      pd.DataFrame,
    base_tf:      str,
    primary_tf:   str = "H1",
    secondary_tf: str = "M15",
    entry_tf:     str = "M5",
) -> dict[str, pd.DataFrame]:
    """
    Produce the {primary, secondary, entry} DataFrame triple expected by
    signal_builder. The base_df should be at-or-below the lowest TF in the
    triple (entry_tf) — typical input is M5 or M1.
    """
    base_min  = _TF_MINUTES[base_tf.upper()]
    entry_min = _TF_MINUTES[entry_tf.upper()]
    if base_min > entry_min:
        raise ValueError(
            f"build_multi_tf_frames: base TF ({base_tf}) is coarser than "
            f"entry TF ({entry_tf}); cannot upsample without future data."
        )

    return {
        "primary":   resample(base_df, primary_tf),
        "secondary": resample(base_df, secondary_tf),
        "entry":     resample(base_df, entry_tf),
    }


def slice_window(
    full_df:     pd.DataFrame,
    decision_ts: pd.Timestamp,
    bars:        int,
    bar_minutes: int,
) -> Optional[pd.DataFrame]:
    """
    Return the last `bars` candles of `full_df` that are FULLY CLOSED at
    `decision_ts` (harness-v2, Phase 0.1 lookahead fix).

    Contract:
      • `full_df` is indexed by bar OPEN time (resample() uses label="left";
        MT5 exports are open-time stamped). A bar with open time `o` and
        duration `bar_minutes` is closed iff  o + bar_minutes <= decision_ts.
      • `decision_ts` is the DECISION time — the close time of the entry-TF
        bar that triggered this evaluation (NOT the bar's open/label time).
      • Implementation: bars with open <= decision_ts - bar_minutes are
        exactly the bars with close <= decision_ts, so a single inclusive
        .loc slice enforces the rule for every row, not just the last one.

    History: the harness-v1 version sliced .loc[:ts] with ts = the entry
    bar's OPEN time, which kept the still-FORMING H1/M15 bar (its label is
    <= ts but it contains up to ~55 min of future data) — a genuine HTF
    lookahead. This version is slightly conservative vs live (live sees the
    partial forming bar; we exclude it entirely — no partial-bar
    reconstruction in v1, per the Phase 0 spec).

    Returns None if fewer than `bars` closed rows exist (signal_builder
    requires a full lookback window).
    """
    last_closed_open = decision_ts - pd.Timedelta(minutes=bar_minutes)
    cutoff = full_df.loc[:last_closed_open]
    if len(cutoff) < bars:
        return None
    return cutoff.iloc[-bars:].copy()
