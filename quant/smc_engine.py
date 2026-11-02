"""
quant/smc_engine.py — Smart Money Concepts (SMC) Engine

Mathematically identifies the four core SMC structures:

  ┌──────────────────────────────────────────────────────────────┐
  │  1. ORDER BLOCKS (OB)                                        │
  │     The last opposing candle before a BOS impulse move.      │
  │     Bullish OB = last bearish candle before a bullish BOS.   │
  │     Bearish OB = last bullish candle before a bearish BOS.   │
  │     Represents institutional accumulation / distribution.    │
  ├──────────────────────────────────────────────────────────────┤
  │  2. FAIR VALUE GAPS (FVG / Imbalance)                        │
  │     3-candle pattern: candle[i-2].high < candle[i].low       │
  │     (bullish) or candle[i-2].low > candle[i].high (bearish). │
  │     Price is "missing" in this range — magnets for fills.    │
  ├──────────────────────────────────────────────────────────────┤
  │  3. BREAK OF STRUCTURE (BOS)                                 │
  │     Price closes BEYOND the last confirmed swing high/low    │
  │     in the direction of the existing trend.                  │
  │     Confirms trend continuation.                             │
  ├──────────────────────────────────────────────────────────────┤
  │  4. CHANGE OF CHARACTER (CHoCH)                              │
  │     Price closes BEYOND the last swing high/low AGAINST      │
  │     the existing structure. First sign of reversal.          │
  │     BOS = continuation; CHoCH = potential reversal.          │
  └──────────────────────────────────────────────────────────────┘

All functions:
  - Accept a plain pandas DataFrame (OHLCV + EMA + ATR pre-calculated)
  - Return typed dataclasses — no raw dicts
  - Are pure and stateless
  - Operate only on the most recent `lookback` bars for performance
"""

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import pandas as pd
import numpy as np

from quant.indicators import find_swing_highs, find_swing_lows

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────
# Shared Enums
# ─────────────────────────────────────────────────────────────────

class Direction(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"


class OBStatus(str, Enum):
    UNTOUCHED  = "UNTOUCHED"   # Price hasn't returned to the OB yet
    TESTED     = "TESTED"      # Price has entered but not fully closed through
    BROKEN     = "BROKEN"      # Price closed fully through — OB invalidated


# ─────────────────────────────────────────────────────────────────
# Result Dataclasses
# ─────────────────────────────────────────────────────────────────

@dataclass
class OrderBlock:
    """
    An Order Block zone with top, bottom, and validity status.

    top    : High of the OB candle (resistance for bearish OB,
             ceiling of the demand zone for bullish OB).
    bottom : Low of the OB candle.
    mid    : (top + bottom) / 2 — useful for 50% retrace entries.
    """
    direction:   Direction
    top:         float
    bottom:      float
    mid:         float
    bar_index:   int            # iloc index of the OB candle
    bar_time:    pd.Timestamp
    status:      OBStatus = OBStatus.UNTOUCHED
    impulse_pct: float = 0.0
    # One-trade-per-OB enforcement. Authoritative filter is the `traded`
    # set passed to nearest_*_ob() (survives per-cycle OB list rebuilds);
    # this flag is a debug hint that nearest_*_ob() may set when it
    # excludes a candidate.
    is_traded:   bool = False    # % price moved in the impulse after this OB

    def __repr__(self) -> str:
        return (
            f"OB({self.direction.value} | "
            f"{self.bottom:.2f}–{self.top:.2f} | "
            f"{self.status.value} | {self.bar_time.strftime('%m-%d %H:%M')})"
        )

    @property
    def proximal(self) -> float:
        """
        Proximal edge — the price the retrace touches FIRST. This is the
        limit-order entry edge (highest fill probability of the OB's edges).
          Bullish OB (demand, below price): price descends → touches TOP first.
          Bearish OB (supply, above price): price ascends → touches BOTTOM first.
        """
        return self.top if self.direction == Direction.BULLISH else self.bottom

    @property
    def distal(self) -> float:
        """Distal (far) edge — the side the stop-loss sits beyond."""
        return self.bottom if self.direction == Direction.BULLISH else self.top


@dataclass
class FairValueGap:
    """
    An unmitigated Fair Value Gap (imbalance zone).

    top    : Upper bound of the gap.
    bottom : Lower bound of the gap.
    A gap is "mitigated" when price retraces into this range.
    """
    direction:   Direction
    top:         float
    bottom:      float
    mid:         float
    bar_index:   int            # iloc index of the THIRD candle (candle[i])
    bar_time:    pd.Timestamp
    mitigated:   bool = False
    gap_size:    float = 0.0    # Absolute price distance of the gap

    def __repr__(self) -> str:
        return (
            f"FVG({self.direction.value} | "
            f"{self.bottom:.2f}–{self.top:.2f} | "
            f"{'MITIGATED' if self.mitigated else 'OPEN'} | "
            f"{self.bar_time.strftime('%m-%d %H:%M')})"
        )


@dataclass
class StructureEvent:
    """
    A confirmed BOS or CHoCH event.

    price_level : The swing level that was broken.
    close_price : The candle close that confirmed the break.
    is_choch    : True = CHoCH (reversal signal), False = BOS (continuation).
    """
    event_type:  str            # "BOS" or "CHoCH"
    direction:   Direction      # Direction of the BREAK (not the previous trend)
    price_level: float          # The swing H/L that was breached
    close_price: float          # Close that confirmed the break
    bar_index:   int
    bar_time:    pd.Timestamp
    is_choch:    bool = False

    def __repr__(self) -> str:
        tag = "CHoCH" if self.is_choch else "BOS"
        return (
            f"{tag}({self.direction.value} | "
            f"Break: {self.price_level:.2f} | "
            f"Close: {self.close_price:.2f} | "
            f"{self.bar_time.strftime('%m-%d %H:%M')})"
        )


@dataclass
class SMCAnalysis:
    """
    Complete SMC picture for one timeframe at one moment.
    This is what gets handed to the Signal Builder.
    """
    # Most recent / most relevant structures (already filtered & sorted)
    bullish_obs:    list[OrderBlock]    = field(default_factory=list)
    bearish_obs:    list[OrderBlock]    = field(default_factory=list)
    bullish_fvgs:   list[FairValueGap]  = field(default_factory=list)
    bearish_fvgs:   list[FairValueGap]  = field(default_factory=list)
    structure_events: list[StructureEvent] = field(default_factory=list)

    # Convenience flags (latest event)
    latest_bos:     Optional[StructureEvent] = None
    latest_choch:   Optional[StructureEvent] = None

    # Current price context
    current_close:  float = 0.0
    current_high:   float = 0.0
    current_low:    float = 0.0

    def nearest_bullish_ob(
        self,
        traded: Optional[set[tuple[str, float, float]]] = None,
    ) -> Optional[OrderBlock]:
        """
        Returns the UNTOUCHED bullish OB closest BELOW current price.

        `traded` is an optional set of `(direction, top, bottom)` tuples for
        OBs that have already been traded (one-trade-per-OB policy — see
        `lessons/2026-05-06-signal-spam-and-ob-reentry-spec.md`). OBs with
        a matching identity are excluded. The OB list is rebuilt fresh by
        find_order_blocks every cycle, so a set-based filter is the only
        thing that survives those rebuilds.
        """
        traded = traded or set()
        candidates = [
            ob for ob in self.bullish_obs
            if ob.status == OBStatus.UNTOUCHED
            and ob.top < self.current_close
            and (ob.direction.value, ob.top, ob.bottom) not in traded
        ]
        return max(candidates, key=lambda ob: ob.top) if candidates else None

    def nearest_bearish_ob(
        self,
        traded: Optional[set[tuple[str, float, float]]] = None,
    ) -> Optional[OrderBlock]:
        """Returns the UNTOUCHED bearish OB closest ABOVE current price."""
        traded = traded or set()
        candidates = [
            ob for ob in self.bearish_obs
            if ob.status == OBStatus.UNTOUCHED
            and ob.bottom > self.current_close
            and (ob.direction.value, ob.top, ob.bottom) not in traded
        ]
        return min(candidates, key=lambda ob: ob.bottom) if candidates else None

    def nearest_bullish_fvg(self) -> Optional[FairValueGap]:
        """Returns the nearest unmitigated bullish FVG below current price."""
        candidates = [
            fvg for fvg in self.bullish_fvgs
            if not fvg.mitigated and fvg.top < self.current_close
        ]
        return max(candidates, key=lambda f: f.top) if candidates else None

    def nearest_bearish_fvg(self) -> Optional[FairValueGap]:
        """Returns the nearest unmitigated bearish FVG above current price."""
        candidates = [
            fvg for fvg in self.bearish_fvgs
            if not fvg.mitigated and fvg.bottom > self.current_close
        ]
        return min(candidates, key=lambda f: f.bottom) if candidates else None


# ─────────────────────────────────────────────────────────────────
# Order Block Detection
# ─────────────────────────────────────────────────────────────────

def find_order_blocks(df: pd.DataFrame,
                      atr: float,
                      lookback: int = 100,
                      swing_lookback: int = 5,
                      min_impulse_atr: float = 1.5,
                      max_obs: int = 5) -> tuple[list[OrderBlock], list[OrderBlock]]:
    """
    Identifies the most recent VALID bullish and bearish Order Blocks.

    Algorithm:
      1. Find all swing highs and swing lows on the chart.
      2. Scan FORWARD from each swing point to find a BOS (price closing
         beyond the swing level).
      3. The candle immediately BEFORE the BOS impulse starts is the OB.
         - For bullish OB: last bearish candle before upward BOS
         - For bearish OB: last bullish candle before downward BOS
      4. Filter: Impulse must be at least `min_impulse_atr * ATR` in size.
      5. Mark status: TESTED if current price has entered, BROKEN if closed through.

    Args:
        df:             OHLCV DataFrame (must have High, Low, Close, Open).
        atr:            Current ATR value (for impulse size filter).
        lookback:       Bars to scan backwards (default 100).
        swing_lookback: Half-window for swing point detection (default 5).
        min_impulse_atr:Minimum impulse size in ATR multiples (default 1.5).
        max_obs:        Maximum OBs to return per direction (default 5).

    Returns:
        (bullish_obs, bearish_obs) — lists sorted by recency (newest first).
    """
    if len(df) < lookback + swing_lookback * 2:
        logger.debug("Insufficient bars for OB detection.")
        return [], []

    working_df = df.iloc[-lookback:].copy().reset_index()
    n = len(working_df)

    swing_highs = find_swing_highs(working_df.rename(
        columns={"High": "High"}), lookback=swing_lookback)
    swing_lows  = find_swing_lows(working_df.rename(
        columns={"Low": "Low"}),  lookback=swing_lookback)

    current_close = float(df["Close"].iloc[-1])
    bullish_obs: list[OrderBlock] = []
    bearish_obs: list[OrderBlock] = []
    min_impulse_size = min_impulse_atr * atr if atr and atr > 0 else 0

    # ── BULLISH OB SCAN ────────────────────────────────────────────
    # Look for swing lows followed by upward impulse (bullish BOS)
    swing_low_indices = working_df.index[swing_lows].tolist()

    for sl_idx in swing_low_indices:
        if sl_idx + 3 >= n:
            continue  # Need at least 3 candles after the swing

        swing_low_price = float(working_df["Low"].iloc[sl_idx])

        # Find the highest close after this swing low (impulse target)
        future_slice = working_df.iloc[sl_idx + 1:]
        if future_slice.empty:
            continue

        impulse_top = float(future_slice["High"].max())
        impulse_size = impulse_top - swing_low_price

        if impulse_size < min_impulse_size:
            continue  # Too small — not institutional

        # Walk forward to find FIRST bearish candle after the swing low
        # (that bearish candle is the institutional bearish OB / demand zone)
        ob_candle_idx = None
        for j in range(sl_idx + 1, min(sl_idx + 15, n)):
            candle_open  = float(working_df["Open"].iloc[j])
            candle_close = float(working_df["Close"].iloc[j])
            if candle_close < candle_open:   # Bearish candle = body going down
                ob_candle_idx = j
                break

        if ob_candle_idx is None:
            continue

        ob_candle = working_df.iloc[ob_candle_idx]
        ob_top    = float(ob_candle["High"])
        ob_bottom = float(ob_candle["Low"])
        ob_time   = ob_candle["time"] if "time" in ob_candle else ob_candle.name

        # Status reflects HISTORY between the OB candle and now, not just
        # current_close. Without history, an OB price wicked through 30
        # bars ago can stay UNTOUCHED and re-emit signals every cycle.
        # See lessons/2026-05-25-ob-invalidation-fix.md.
        future = working_df.iloc[ob_candle_idx + 1:]
        if not future.empty and (future["Close"] < ob_bottom).any():
            status = OBStatus.BROKEN     # Any later close below the OB body = invalidated
        elif not future.empty and (
            (future["Low"] >= ob_bottom) & (future["Low"] <= ob_top)
        ).any():
            status = OBStatus.TESTED     # Any later low dipped into the zone = tested
        elif current_close < ob_bottom:
            status = OBStatus.BROKEN     # Fallback when OB is the latest bar (no future)
        elif ob_bottom <= current_close <= ob_top:
            status = OBStatus.TESTED
        else:
            status = OBStatus.UNTOUCHED  # No intervening test/break — price never returned

        impulse_pct = round(impulse_size / swing_low_price * 100, 3)

        bullish_obs.append(OrderBlock(
            direction   = Direction.BULLISH,
            top         = ob_top,
            bottom      = ob_bottom,
            mid         = round((ob_top + ob_bottom) / 2, 5),
            bar_index   = ob_candle_idx,
            bar_time    = pd.Timestamp(ob_time) if not isinstance(ob_time, pd.Timestamp) else ob_time,
            status      = status,
            impulse_pct = impulse_pct,
        ))

    # ── BEARISH OB SCAN ────────────────────────────────────────────
    # Look for swing highs followed by downward impulse (bearish BOS)
    swing_high_indices = working_df.index[swing_highs].tolist()

    for sh_idx in swing_high_indices:
        if sh_idx + 3 >= n:
            continue

        swing_high_price = float(working_df["High"].iloc[sh_idx])
        future_slice = working_df.iloc[sh_idx + 1:]
        if future_slice.empty:
            continue

        impulse_bottom = float(future_slice["Low"].min())
        impulse_size   = swing_high_price - impulse_bottom

        if impulse_size < min_impulse_size:
            continue

        # Walk forward to find FIRST bullish candle after the swing high
        ob_candle_idx = None
        for j in range(sh_idx + 1, min(sh_idx + 15, n)):
            candle_open  = float(working_df["Open"].iloc[j])
            candle_close = float(working_df["Close"].iloc[j])
            if candle_close > candle_open:   # Bullish candle = body going up
                ob_candle_idx = j
                break

        if ob_candle_idx is None:
            continue

        ob_candle = working_df.iloc[ob_candle_idx]
        ob_top    = float(ob_candle["High"])
        ob_bottom = float(ob_candle["Low"])
        ob_time   = ob_candle["time"] if "time" in ob_candle else ob_candle.name

        # Mirror of the bullish OB history scan above. See
        # lessons/2026-05-25-ob-invalidation-fix.md.
        future = working_df.iloc[ob_candle_idx + 1:]
        if not future.empty and (future["Close"] > ob_top).any():
            status = OBStatus.BROKEN     # Any later close above the OB body = invalidated
        elif not future.empty and (
            (future["High"] >= ob_bottom) & (future["High"] <= ob_top)
        ).any():
            status = OBStatus.TESTED     # Any later high entered the zone = tested
        elif current_close > ob_top:
            status = OBStatus.BROKEN
        elif ob_bottom <= current_close <= ob_top:
            status = OBStatus.TESTED
        else:
            status = OBStatus.UNTOUCHED

        impulse_pct = round(impulse_size / swing_high_price * 100, 3)

        bearish_obs.append(OrderBlock(
            direction   = Direction.BEARISH,
            top         = ob_top,
            bottom      = ob_bottom,
            mid         = round((ob_top + ob_bottom) / 2, 5),
            bar_index   = ob_candle_idx,
            bar_time    = pd.Timestamp(ob_time) if not isinstance(ob_time, pd.Timestamp) else ob_time,
            status      = status,
            impulse_pct = impulse_pct,
        ))

    # Sort newest first; filter out BROKEN OBs; keep max_obs each
    bullish_obs = sorted(
        [ob for ob in bullish_obs if ob.status != OBStatus.BROKEN],
        key=lambda ob: ob.bar_index,
        reverse=True,
    )[:max_obs]

    bearish_obs = sorted(
        [ob for ob in bearish_obs if ob.status != OBStatus.BROKEN],
        key=lambda ob: ob.bar_index,
        reverse=True,
    )[:max_obs]

    logger.debug(
        f"OBs found: {len(bullish_obs)} bullish, {len(bearish_obs)} bearish"
    )
    return bullish_obs, bearish_obs


# ─────────────────────────────────────────────────────────────────
# Fair Value Gap Detection
# ─────────────────────────────────────────────────────────────────

def find_fvgs(df: pd.DataFrame,
              atr: float,
              lookback: int = 100,
              min_gap_atr: float = 0.3,
              max_fvgs: int = 5) -> tuple[list[FairValueGap], list[FairValueGap]]:
    """
    Scans for Fair Value Gaps (imbalances) in the last `lookback` bars.

    FVG Pattern (3 candles: i-2, i-1, i):
      Bullish FVG: candle[i].Low  > candle[i-2].High  (gap above i-2)
      Bearish FVG: candle[i].High < candle[i-2].Low   (gap below i-2)

    The FVG zone is the gap between candle[i-2] and candle[i]:
      Bullish zone: bottom = candle[i-2].High, top = candle[i].Low
      Bearish zone: bottom = candle[i].High,   top = candle[i-2].Low

    Status: mitigated = True when current price has ENTERED the gap.

    Args:
        df:          OHLCV DataFrame.
        atr:         Current ATR (for minimum gap size filter).
        lookback:    Bars to scan (default 100).
        min_gap_atr: FVG must be at least this many ATR units wide (default 0.3).
        max_fvgs:    Max FVGs per direction to return (default 5).
    """
    if len(df) < max(10, lookback):
        return [], []

    working = df.iloc[-lookback:].reset_index()
    n = len(working)
    current_close = float(df["Close"].iloc[-1])
    min_gap_size  = min_gap_atr * atr if atr and atr > 0 else 0

    bullish_fvgs: list[FairValueGap] = []
    bearish_fvgs: list[FairValueGap] = []

    for i in range(2, n):
        prev_prev = working.iloc[i - 2]
        candle    = working.iloc[i]

        # ── Bullish FVG ────────────────────────────────────────────
        # Gap UP: i's Low is ABOVE i-2's High
        gap_bottom = float(prev_prev["High"])
        gap_top    = float(candle["Low"])

        if gap_top > gap_bottom:
            gap_size = gap_top - gap_bottom
            if gap_size >= min_gap_size:
                bar_time = candle["time"] if "time" in candle.index else candle.name
                mitigated = current_close <= gap_top  # Price returned into gap

                bullish_fvgs.append(FairValueGap(
                    direction  = Direction.BULLISH,
                    top        = round(gap_top, 5),
                    bottom     = round(gap_bottom, 5),
                    mid        = round((gap_top + gap_bottom) / 2, 5),
                    bar_index  = i,
                    bar_time   = pd.Timestamp(bar_time) if not isinstance(bar_time, pd.Timestamp) else bar_time,
                    mitigated  = mitigated,
                    gap_size   = round(gap_size, 5),
                ))

        # ── Bearish FVG ────────────────────────────────────────────
        # Gap DOWN: i's High is BELOW i-2's Low
        gap_bottom_b = float(candle["High"])
        gap_top_b    = float(prev_prev["Low"])

        if gap_top_b > gap_bottom_b:
            gap_size = gap_top_b - gap_bottom_b
            if gap_size >= min_gap_size:
                bar_time = candle["time"] if "time" in candle.index else candle.name
                mitigated = current_close >= gap_bottom_b  # Price returned into gap

                bearish_fvgs.append(FairValueGap(
                    direction  = Direction.BEARISH,
                    top        = round(gap_top_b, 5),
                    bottom     = round(gap_bottom_b, 5),
                    mid        = round((gap_top_b + gap_bottom_b) / 2, 5),
                    bar_index  = i,
                    bar_time   = pd.Timestamp(bar_time) if not isinstance(bar_time, pd.Timestamp) else bar_time,
                    mitigated  = mitigated,
                    gap_size   = round(gap_size, 5),
                ))

    # Keep only un-mitigated FVGs, newest first
    bullish_fvgs = sorted(
        [f for f in bullish_fvgs if not f.mitigated],
        key=lambda f: f.bar_index,
        reverse=True,
    )[:max_fvgs]

    bearish_fvgs = sorted(
        [f for f in bearish_fvgs if not f.mitigated],
        key=lambda f: f.bar_index,
        reverse=True,
    )[:max_fvgs]

    logger.debug(
        f"FVGs found: {len(bullish_fvgs)} bullish (open), "
        f"{len(bearish_fvgs)} bearish (open)"
    )
    return bullish_fvgs, bearish_fvgs


# ─────────────────────────────────────────────────────────────────
# BOS & CHoCH Detection
# ─────────────────────────────────────────────────────────────────

def find_structure_events(df: pd.DataFrame,
                           lookback: int = 100,
                           swing_lookback: int = 5,
                           max_events: int = 10) -> list[StructureEvent]:
    """
    Detects Break of Structure (BOS) and Change of Character (CHoCH).

    Algorithm:
      1. Build a running list of swing highs and lows (left-to-right).
      2. Track the dominant trend direction (Higher Highs + Higher Lows = UP).
      3. When price closes ABOVE the last swing high:
           - If trend = UP  → BOS (continuation)
           - If trend = DOWN → CHoCH (reversal signal)
      4. When price closes BELOW the last swing low:
           - If trend = DOWN → BOS (continuation)
           - If trend = UP   → CHoCH (reversal signal)

    Args:
        df:             OHLCV DataFrame.
        lookback:       Bars to scan (default 100).
        swing_lookback: Window for swing detection (default 5).
        max_events:     Maximum events to return (default 10).

    Returns:
        List of StructureEvent sorted by recency (newest first).
    """
    if len(df) < lookback:
        return []

    working = df.iloc[-lookback:].copy().reset_index()
    n = len(working)

    swing_high_mask = find_swing_highs(working, lookback=swing_lookback)
    swing_low_mask  = find_swing_lows(working,  lookback=swing_lookback)

    events: list[StructureEvent] = []

    # Track last confirmed swing levels
    last_swing_high: Optional[float] = None
    last_swing_high_idx: int = 0
    last_swing_low:  Optional[float] = None
    last_swing_low_idx: int = 0

    # Track trend direction: 1 = bullish, -1 = bearish, 0 = unknown
    trend: int = 0
    prev_swing_high: Optional[float] = None
    prev_swing_low:  Optional[float] = None

    for i in range(n):
        close = float(working["Close"].iloc[i])
        high  = float(working["High"].iloc[i])
        low   = float(working["Low"].iloc[i])
        bar_time = working["time"].iloc[i] if "time" in working.columns else working.index[i]

        # ── Update swings ──────────────────────────────────────────
        if swing_high_mask.iloc[i]:
            if last_swing_high is not None:
                prev_swing_high = last_swing_high
            last_swing_high = high
            last_swing_high_idx = i

            # Update trend from swing high context
            if prev_swing_high is not None:
                if high > prev_swing_high:
                    # Higher high — trend may be bullish
                    if trend != 1:
                        trend = 1

        if swing_low_mask.iloc[i]:
            if last_swing_low is not None:
                prev_swing_low = last_swing_low
            last_swing_low = low
            last_swing_low_idx = i

            # Update trend from swing low context
            if prev_swing_low is not None:
                if low < prev_swing_low:
                    if trend != -1:
                        trend = -1

        # ── Check for BOS / CHoCH ──────────────────────────────────
        # We only detect on CLOSED candles (not the last forming bar)
        if i == n - 1:
            break  # Skip current candle

        # Bullish break: close above last swing high
        if last_swing_high is not None and i > last_swing_high_idx:
            if close > last_swing_high:
                is_choch = trend == -1  # Breaking up while trend was DOWN = CHoCH
                events.append(StructureEvent(
                    event_type  = "CHoCH" if is_choch else "BOS",
                    direction   = Direction.BULLISH,
                    price_level = last_swing_high,
                    close_price = close,
                    bar_index   = i,
                    bar_time    = pd.Timestamp(bar_time) if not isinstance(bar_time, pd.Timestamp) else bar_time,
                    is_choch    = is_choch,
                ))
                # Update trend after break
                trend = 1
                last_swing_high = close   # New reference level
                prev_swing_high = None

        # Bearish break: close below last swing low
        if last_swing_low is not None and i > last_swing_low_idx:
            if close < last_swing_low:
                is_choch = trend == 1   # Breaking down while trend was UP = CHoCH
                events.append(StructureEvent(
                    event_type  = "CHoCH" if is_choch else "BOS",
                    direction   = Direction.BEARISH,
                    price_level = last_swing_low,
                    close_price = close,
                    bar_index   = i,
                    bar_time    = pd.Timestamp(bar_time) if not isinstance(bar_time, pd.Timestamp) else bar_time,
                    is_choch    = is_choch,
                ))
                trend = -1
                last_swing_low = close
                prev_swing_low = None

    # Sort newest first
    events.sort(key=lambda e: e.bar_index, reverse=True)
    result = events[:max_events]

    logger.debug(
        f"Structure events: "
        f"{sum(1 for e in result if not e.is_choch)} BOS, "
        f"{sum(1 for e in result if e.is_choch)} CHoCH"
    )
    return result


# ─────────────────────────────────────────────────────────────────
# Master SMC Runner
# ─────────────────────────────────────────────────────────────────

def run_smc_analysis(df: pd.DataFrame, atr: float) -> SMCAnalysis:
    """
    Run all SMC detections in one call.
    Returns a fully populated SMCAnalysis object.

    Args:
        df:  Prepared OHLCV DataFrame (with EMA + ATR columns already added).
        atr: Current ATR value (float) for impulse filtering.
    """
    if df is None or len(df) < 20:
        logger.warning("run_smc_analysis: insufficient data (need 20+ bars).")
        return SMCAnalysis()

    current_close = float(df["Close"].iloc[-1])
    current_high  = float(df["High"].iloc[-1])
    current_low   = float(df["Low"].iloc[-1])

    # Run all detections
    bullish_obs, bearish_obs   = find_order_blocks(df, atr=atr)
    bullish_fvgs, bearish_fvgs = find_fvgs(df, atr=atr)
    struct_events              = find_structure_events(df)

    # Extract latest BOS and CHoCH
    latest_bos   = next((e for e in struct_events if not e.is_choch), None)
    latest_choch = next((e for e in struct_events if e.is_choch),     None)

    analysis = SMCAnalysis(
        bullish_obs      = bullish_obs,
        bearish_obs      = bearish_obs,
        bullish_fvgs     = bullish_fvgs,
        bearish_fvgs     = bearish_fvgs,
        structure_events = struct_events,
        latest_bos       = latest_bos,
        latest_choch     = latest_choch,
        current_close    = current_close,
        current_high     = current_high,
        current_low      = current_low,
    )

    # Quick summary log
    logger.debug(
        f"SMC Analysis | Close: {current_close:.2f} | "
        f"OBs: {len(bullish_obs)}B/{len(bearish_obs)}S | "
        f"FVGs: {len(bullish_fvgs)}B/{len(bearish_fvgs)}S | "
        f"Latest: {latest_bos or latest_choch or 'None'}"
    )

    return analysis
