"""
data/_fetch_dukascopy_2024.py — one-shot Dukascopy M1 fetcher.

Pulls XAUUSD M1 BID candles from Dukascopy's free historical feed for
2024-01-01 -> 2024-11-30 inclusive, resamples to M5, and writes them in
the same tab-delimited MT5 format as data/xauusd_m5.csv.

Outputs:
  data/xauusd_jan_nov2024_m5.csv     resampled M5 candles, just the new range
  data/xauusd_m5_full.csv            new + existing merged, de-duped, sorted

Original data/xauusd_m5.csv is NOT touched.

Run:  python data/_fetch_dukascopy_2024.py
"""

from __future__ import annotations

import logging
import lzma
import struct
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s | %(levelname)-5s | %(message)s",
)
log = logging.getLogger("dukascopy")

# ── Dukascopy feed spec ────────────────────────────────────────────────
INSTRUMENT     = "XAUUSD"
POINT_DIVISOR  = 1000.0            # XAUUSD on Dukascopy = 3 decimal places
URL_FMT = (
    "https://datafeed.dukascopy.com/datafeed/{ins}/"
    "{yyyy}/{mm:02d}/{dd:02d}/BID_candles_min_1.bi5"
)
# 5 BE uint32 (time-offset-sec from DAY start, open, close, low, high)
# + 1 BE float32 (volume). One file = 1440 minute candles per day.
CANDLE         = struct.Struct(">IIIIIf")
CANDLE_SIZE    = CANDLE.size       # 24 bytes
USER_AGENT     = "Mozilla/5.0 (compatible; one-shot-fetch/1.0)"

# ── Paths ──────────────────────────────────────────────────────────────
DATA_DIR       = Path("data")
OUT_M5_PATH    = DATA_DIR / "xauusd_jan_nov2024_m5.csv"
EXISTING_PATH  = DATA_DIR / "xauusd_m5.csv"
COMBINED_PATH  = DATA_DIR / "xauusd_m5_full.csv"

# ── Date range ─────────────────────────────────────────────────────────
START          = datetime(2024, 1,  1, tzinfo=timezone.utc)
END_EXCLUSIVE  = datetime(2024, 12, 1, tzinfo=timezone.utc)


def _day_url(day_ts: datetime) -> str:
    # Dukascopy uses 0-indexed months in the URL.
    return URL_FMT.format(
        ins  = INSTRUMENT,
        yyyy = day_ts.year,
        mm   = day_ts.month - 1,
        dd   = day_ts.day,
    )


def _fetch_day(
    session: requests.Session,
    day_ts: datetime,
    retries: int = 3,
) -> list[tuple[datetime, float, float, float, float, float]]:
    url = _day_url(day_ts)
    raw: bytes | None = None
    for attempt in range(retries):
        try:
            r = session.get(url, timeout=30)
        except requests.RequestException as e:
            if attempt == retries - 1:
                log.warning(f"  {day_ts.date()} network error: {e} — skipping")
                return []
            time.sleep(1.5 * (attempt + 1))
            continue

        if r.status_code == 404:
            # Market closed all day (weekend / holiday).
            return []
        if r.status_code != 200:
            if attempt == retries - 1:
                log.warning(f"  {day_ts.date()} HTTP {r.status_code} — skipping")
                return []
            time.sleep(1.5 * (attempt + 1))
            continue

        raw = r.content
        break

    if not raw:
        return []

    try:
        data = lzma.decompress(raw)
    except lzma.LZMAError:
        return []

    if not data or len(data) % CANDLE_SIZE != 0:
        return []

    out: list[tuple[datetime, float, float, float, float, float]] = []
    day_start = day_ts.replace(hour=0, minute=0, second=0, microsecond=0)
    for off in range(0, len(data), CANDLE_SIZE):
        t_off, o, c, l, h, v = CANDLE.unpack_from(data, off)
        # Dukascopy emits all 1440 minutes per day, including market-closed
        # ones, with O=H=L=C and v=0. Drop those — they bloat the file and
        # would distort the resampled M5 volume sum to zero on closed hours.
        if v == 0.0 and o == c == l == h:
            continue
        ts = day_start + timedelta(seconds=t_off)
        out.append((
            ts,
            o / POINT_DIVISOR,
            h / POINT_DIVISOR,
            l / POINT_DIVISOR,
            c / POINT_DIVISOR,
            float(v),
        ))
    return out


def fetch_range(start: datetime, end_exclusive: datetime) -> pd.DataFrame:
    days: list[datetime] = []
    cur = start
    while cur < end_exclusive:
        days.append(cur)
        cur += timedelta(days=1)

    log.info(
        f"Dispatching {len(days)} daily requests "
        f"({start.date()} -> {(end_exclusive - timedelta(days=1)).date()})"
    )

    rows: list[tuple] = []
    done = 0
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = {pool.submit(_fetch_day, session, d): d for d in days}
        for fut in as_completed(futures):
            chunk = fut.result()
            if chunk:
                rows.extend(chunk)
            done += 1
            if done % 50 == 0:
                log.info(f"  …{done}/{len(days)} days fetched, "
                         f"{len(rows):,} M1 candles so far")

    if not rows:
        raise RuntimeError("No data returned from Dukascopy.")

    df = pd.DataFrame(
        rows, columns=["time", "Open", "High", "Low", "Close", "Volume"],
    ).set_index("time").sort_index()

    # Sanity-check the divisor. XAUUSD in 2024 ranged roughly 1990 -> 2790.
    med = float(df["Close"].median())
    if not 1500 < med < 3500:
        raise RuntimeError(
            f"Sanity check failed: median Close = {med:.4f}. "
            f"POINT_DIVISOR={POINT_DIVISOR} is probably wrong for this instrument."
        )
    log.info(f"Fetched {len(df):,} M1 candles. Median Close={med:.2f} (sanity OK).")
    return df


def resample_to_m5(df_m1: pd.DataFrame) -> pd.DataFrame:
    out = df_m1.resample("5min", label="left", closed="left").agg({
        "Open":   "first",
        "High":   "max",
        "Low":    "min",
        "Close":  "last",
        "Volume": "sum",
    }).dropna(subset=["Open", "High", "Low", "Close"])
    return out


def write_mt5_csv(df_m5: pd.DataFrame, path: Path) -> None:
    """Write the tab-delimited <DATE>\\t<TIME>\\t... format used by data/xauusd_m5.csv."""
    idx = df_m5.index
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_convert("UTC").tz_localize(None)

    out = pd.DataFrame({
        "<DATE>":    idx.strftime("%Y.%m.%d"),
        "<TIME>":    idx.strftime("%H:%M:%S"),
        "<OPEN>":    df_m5["Open"].map(lambda x: f"{x:.2f}").values,
        "<HIGH>":    df_m5["High"].map(lambda x: f"{x:.2f}").values,
        "<LOW>":     df_m5["Low"].map(lambda x: f"{x:.2f}").values,
        "<CLOSE>":   df_m5["Close"].map(lambda x: f"{x:.2f}").values,
        # Dukascopy reports volume in lots (float, ~0.004-5.0/min). Scale to
        # roughly match the existing CSV's tick-count magnitudes so the column
        # is at least non-zero. Strategy doesn't consume volume meaningfully.
        "<TICKVOL>": df_m5["Volume"].map(lambda x: f"{int(round(x * 1000))}").values,
        "<VOL>":     "0",
        "<SPREAD>":  "0",
    })
    out.to_csv(path, sep="\t", index=False, lineterminator="\r\n")


def _load_mt5_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, sep="\t")
    ts = pd.to_datetime(
        df["<DATE>"].astype(str) + " " + df["<TIME>"].astype(str),
        format="%Y.%m.%d %H:%M:%S",
    )
    df.insert(0, "_ts", ts)
    return df.sort_values("_ts").reset_index(drop=True)


def merge(new_path: Path, existing_path: Path, combined_path: Path) -> None:
    new_df = _load_mt5_csv(new_path)
    old_df = _load_mt5_csv(existing_path)

    new_cols = [c for c in new_df.columns if c != "_ts"]
    old_cols = [c for c in old_df.columns if c != "_ts"]
    log.info("Column check:")
    log.info(f"  new      : {new_cols}")
    log.info(f"  existing : {old_cols}")
    if new_cols != old_cols:
        raise RuntimeError(f"Column mismatch: {new_cols} != {old_cols}")
    log.info("  -> columns match")

    log.info("Row counts BEFORE merge:")
    log.info(f"  new      : {len(new_df):>7,}  "
             f"({new_df['_ts'].iloc[0]} -> {new_df['_ts'].iloc[-1]})")
    log.info(f"  existing : {len(old_df):>7,}  "
             f"({old_df['_ts'].iloc[0]} -> {old_df['_ts'].iloc[-1]})")

    combined = pd.concat([new_df, old_df], ignore_index=True)
    before   = len(combined)
    combined = (combined
                .drop_duplicates(subset=["_ts"], keep="first")
                .sort_values("_ts")
                .reset_index(drop=True))
    dups     = before - len(combined)

    log.info("Row counts AFTER merge:")
    log.info(f"  combined : {len(combined):>7,}  "
             f"({combined['_ts'].iloc[0]} -> {combined['_ts'].iloc[-1]})")
    log.info(f"  duplicate timestamps removed at the join: {dups}")

    combined.drop(columns=["_ts"]).to_csv(
        combined_path, sep="\t", index=False, lineterminator="\r\n",
    )
    log.info(f"Wrote: {combined_path}")


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not EXISTING_PATH.exists():
        log.error(f"Existing CSV not found: {EXISTING_PATH}")
        return 1

    log.info("=" * 70)
    log.info("Step 1: fetch M1 from Dukascopy")
    log.info("=" * 70)
    df_m1 = fetch_range(START, END_EXCLUSIVE)
    log.info(f"M1 span: {df_m1.index[0]} -> {df_m1.index[-1]}")

    log.info("=" * 70)
    log.info("Step 2: resample to M5")
    log.info("=" * 70)
    df_m5 = resample_to_m5(df_m1)
    log.info(f"M5 span: {df_m5.index[0]} -> {df_m5.index[-1]} ({len(df_m5):,} rows)")

    log.info("=" * 70)
    log.info(f"Step 3: write {OUT_M5_PATH}")
    log.info("=" * 70)
    write_mt5_csv(df_m5, OUT_M5_PATH)
    log.info(f"Wrote: {OUT_M5_PATH}")

    log.info("=" * 70)
    log.info(f"Step 4: merge with {EXISTING_PATH} -> {COMBINED_PATH}")
    log.info("=" * 70)
    merge(OUT_M5_PATH, EXISTING_PATH, COMBINED_PATH)

    log.info("=" * 70)
    log.info("Done. Original CSV untouched.")
    log.info("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
