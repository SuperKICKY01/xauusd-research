"""
core/data_stream.py — Asynchronous Data Pipeline (Layer 1)

Three concurrent coroutines run independently:

  ┌─ _tick_streamer ──────────────────────────────────────────────┐
  │  0.5s poll | Publishes: TICK_DATA                             │
  │  ALSO: Real-time margin_level guard → EMERGENCY_LIQUIDATE      │
  └────────────────────────────────────────────────────────────────┘
  ┌─ _ohlcv_fetcher ──────────────────────────────────────────────┐
  │  5s poll   | Publishes: OHLCV_UPDATED (3 timeframes)          │
  │  Memory-safe: DataFrames explicitly deleted after publish      │
  └────────────────────────────────────────────────────────────────┘
  ┌─ _account_monitor ────────────────────────────────────────────┐
  │  1s poll   | Publishes: ACCOUNT_STATE (balance, DD%, margin)  │
  │  Soft DD warning at 80% of limit                              │
  └────────────────────────────────────────────────────────────────┘

CRITICAL DESIGN NOTES:
  - All mt5.* calls use asyncio.to_thread() → never blocks event loop
  - DataFrames are del'd + gc.collect() called after each OHLCV cycle
  - Margin guard fires BEFORE any other processing on every tick
"""

import asyncio
import gc
import logging
from datetime import date, datetime
from typing import Optional

import pandas as pd
import MetaTrader5 as mt5

from config import CFG
from core.event_bus import bus, Topics

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────
# MT5 Timeframe Mapping
# ─────────────────────────────────────────────────────────────────

TIMEFRAME_MAP: dict[str, int] = {
    "M1":  mt5.TIMEFRAME_M1,
    "M5":  mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30,
    "H1":  mt5.TIMEFRAME_H1,
    "H4":  mt5.TIMEFRAME_H4,
    "D1":  mt5.TIMEFRAME_D1,
    "W1":  mt5.TIMEFRAME_W1,
}


class DataStream:
    """
    Streams market data from MT5 and publishes events to the event bus.
    Start with: await data_stream.start()
    Stop with:  await data_stream.stop()
    """

    def __init__(self) -> None:
        self.symbol = CFG.symbol.symbol
        self._running: bool = False
        self._emergency_fired: bool = False  # Prevent duplicate emergency triggers
        # ── Start-of-day equity anchor (true daily DD math) ────────────
        # Snapshot of equity at the first ACCOUNT_STATE poll of each UTC
        # calendar day. Re-anchored on date change. Persisted across
        # poll iterations only — process restart re-anchors to the
        # then-current equity, hiding earlier intraday loss until the
        # next UTC midnight. Matches the "restart logic must re-anchor
        # SOD" note in references/veto-rules.md (VETO 2 edge case).
        # Disk persistence is a future hardening pass.
        self._sod_equity: Optional[float] = None
        self._sod_date:   Optional[date]  = None

    # ─────────────────────────────────────────────────────────────
    # Lifecycle
    # ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Launch all three data coroutines concurrently."""
        self._running = True
        logger.info(f"📡 DataStream starting — Symbol: {self.symbol}")
        await asyncio.gather(
            self._tick_streamer(),
            self._ohlcv_fetcher(),
            self._account_monitor(),
            return_exceptions=True,  # Don't let one crash kill all three
        )

    async def stop(self) -> None:
        """Signal all coroutines to exit their loops."""
        self._running = False
        logger.info("DataStream stopped.")

    def reset_emergency_flag(self) -> None:
        """Call this if you want the margin guard to re-arm after a test."""
        self._emergency_fired = False

    # ─────────────────────────────────────────────────────────────
    # COROUTINE 1: Tick Streamer + Margin Guard
    # ─────────────────────────────────────────────────────────────

    async def _tick_streamer(self) -> None:
        """
        Polls the latest tick every 0.5s (configurable via MARGIN_POLL_INTERVAL_SEC).

        On each iteration:
          1. Fetch latest tick → publish TICK_DATA
          2. Fetch account_info → check margin_level
          3. If margin_level < 500% AND positions are open → EMERGENCY_LIQUIDATE

        Using a separate account fetch here (not the account_monitor snapshot)
        ensures the margin check is current at tick-frequency, not 1s-old data.
        """
        logger.debug("Tick streamer coroutine started.")

        while self._running:
            try:
                # ── 1. Fetch latest tick ────────────────────────────────
                tick = await asyncio.to_thread(mt5.symbol_info_tick, self.symbol)

                if tick is None:
                    logger.warning(f"No tick data returned for {self.symbol}.")
                    await asyncio.sleep(CFG.margin_poll_interval_sec)
                    continue

                sym_info = await asyncio.to_thread(mt5.symbol_info, self.symbol)
                point = sym_info.point if sym_info else 0.01
                spread_points = round((tick.ask - tick.bid) / point, 1) if point > 0 else 0.0

                tick_payload = {
                    "symbol":   self.symbol,
                    "bid":      tick.bid,
                    "ask":      tick.ask,
                    "spread":   spread_points,      # In points
                    "volume":   tick.volume,
                    "time":     datetime.fromtimestamp(tick.time),
                    "time_msc": tick.time_msc,      # Millisecond precision
                }
                await bus.publish(Topics.TICK_DATA, tick_payload)

                # ── 2. Real-Time Margin Guard ───────────────────────────
                # LESSON LEARNED: MT5 margin_level = 0.0 when no positions are open.
                # We only trigger if margin > 0 (meaning positions ARE open).
                account = await asyncio.to_thread(mt5.account_info)

                if account is not None and account.margin > 0:
                    margin_level = account.margin_level

                    # Soft alert: approaching threshold
                    soft_threshold = CFG.risk.emergency_margin_level * 1.5
                    if margin_level < soft_threshold:
                        logger.warning(
                            f"⚠️  Margin level {margin_level:.1f}% approaching "
                            f"emergency threshold {CFG.risk.emergency_margin_level}%"
                        )
                        await bus.publish(Topics.MARGIN_ALERT, {
                            "margin_level": margin_level,
                            "threshold":    CFG.risk.emergency_margin_level,
                            "equity":       account.equity,
                        })

                    # HARD EMERGENCY: margin_level below absolute floor
                    if margin_level < CFG.risk.emergency_margin_level and not self._emergency_fired:
                        self._emergency_fired = True   # One-shot — prevent flood
                        logger.critical(
                            f"🚨 CRITICAL MARGIN BREACH: {margin_level:.1f}% < "
                            f"{CFG.risk.emergency_margin_level}% | "
                            f"Equity: ${account.equity:,.2f} | "
                            f"Publishing EMERGENCY_LIQUIDATE"
                        )
                        await bus.publish(Topics.EMERGENCY_LIQUIDATE, {
                            "reason":        "margin_level_breach",
                            "margin_level":  margin_level,
                            "equity":        account.equity,
                            "balance":       account.balance,
                            "timestamp":     datetime.now().isoformat(),
                        })

            except Exception as e:
                logger.error(f"Tick streamer error: {e}", exc_info=True)

            await asyncio.sleep(CFG.margin_poll_interval_sec)

    # ─────────────────────────────────────────────────────────────
    # COROUTINE 2: OHLCV Candle Fetcher
    # ─────────────────────────────────────────────────────────────

    async def _ohlcv_fetcher(self) -> None:
        """
        Fetches OHLCV candles for all three configured timeframes every 5s.

        Publishes a dict: { "primary": df, "secondary": df, "entry": df }
        to Topics.OHLCV_UPDATED.

        MEMORY SAFETY:
          - `candles` dict is explicitly deleted after publish
          - gc.collect() is called to immediately reclaim memory
          - DataFrames are NOT stored as instance variables
        """
        logger.debug("OHLCV fetcher coroutine started.")

        sym_cfg = CFG.symbol
        timeframe_map = {
            "primary":   sym_cfg.timeframe_primary,
            "secondary": sym_cfg.timeframe_secondary,
            "entry":     sym_cfg.timeframe_entry,
        }

        while self._running:
            candles: dict[str, pd.DataFrame] = {}

            try:
                for label, tf_str in timeframe_map.items():
                    tf_int = TIMEFRAME_MAP.get(tf_str)
                    if tf_int is None:
                        logger.error(f"Unknown timeframe string: '{tf_str}' — skipping.")
                        continue

                    rates = await asyncio.to_thread(
                        mt5.copy_rates_from_pos,
                        self.symbol,
                        tf_int,
                        0,                          # Start from current bar
                        sym_cfg.candles_lookback,   # Number of bars
                    )

                    if rates is None or len(rates) == 0:
                        logger.warning(
                            f"OHLCV: No data returned for {self.symbol} {tf_str}. "
                            f"MT5 error: {mt5.last_error()}"
                        )
                        continue

                    # Build DataFrame — keep it lean, only essential columns
                    df = pd.DataFrame(rates)[
                        ["time", "open", "high", "low", "close", "tick_volume", "real_volume"]
                    ].copy()
                    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
                    df.set_index("time", inplace=True)
                    df.rename(columns={
                        "open":        "Open",
                        "high":        "High",
                        "low":         "Low",
                        "close":       "Close",
                        "tick_volume": "Volume",
                        "real_volume": "RealVolume",
                    }, inplace=True)

                    candles[label] = df
                    logger.debug(
                        f"OHLCV [{label}/{tf_str}]: {len(df)} bars | "
                        f"Last close: {df['Close'].iloc[-1]:.2f}"
                    )

                if candles:
                    await bus.publish(Topics.OHLCV_UPDATED, candles)

            except Exception as e:
                logger.error(f"OHLCV fetcher error: {e}", exc_info=True)

            finally:
                # ── MEMORY CLEANUP ─────────────────────────────────────
                # Explicitly delete DataFrames and trigger GC.
                # This prevents MB-per-cycle accumulation over hours of runtime.
                del candles
                gc.collect()
                # ── END MEMORY CLEANUP ─────────────────────────────────

            await asyncio.sleep(CFG.signal_check_interval_sec)

    # ─────────────────────────────────────────────────────────────
    # COROUTINE 3: Account State Monitor
    # ─────────────────────────────────────────────────────────────

    async def _account_monitor(self) -> None:
        """
        Publishes a full account snapshot every 1s.
        Calculates:
          - daily_dd_pct: (start_of_day_equity - equity) / start_of_day_equity * 100
            Re-anchored to current equity at the first poll of each UTC
            calendar day. FTMO measures daily DD from session start, NOT
            from account inception (pre-2026-05-09 the formula used
            initial_balance — see lessons/2026-05-09-daily-dd-circuit-breaker.md).
            Total DD is computed in RiskManager VETO 3 against initial_balance.
          - start_of_day_equity: the SOD baseline used by daily_dd_pct
            and the RiskManager VETO 2b circuit breaker.

        Also emits a soft DRAWDOWN_BREACH warning when 80% of DD limit is reached.

        NOTE: Margin_level emergency is handled in _tick_streamer for speed.
              This monitor is for broader compliance checking.
        """
        logger.debug("Account monitor coroutine started.")

        while self._running:
            try:
                account = await asyncio.to_thread(mt5.account_info)
                if account is None:
                    await asyncio.sleep(CFG.data_poll_interval_sec)
                    continue

                # ── Start-of-day equity anchor ────────────────────────
                # Re-anchor on UTC date change. On first poll of the
                # process, anchor to current equity (re-anchor on
                # restart is the documented behaviour, not a bug).
                # Guard against equity=0 on a brief MT5 disconnect so
                # we don't lock the SOD baseline at zero for the rest
                # of the day.
                today_utc = datetime.utcnow().date()
                if self._sod_date != today_utc and account.equity > 0:
                    self._sod_equity = account.equity
                    self._sod_date   = today_utc
                    logger.info(
                        f"📅 SOD anchor set: equity ${account.equity:,.2f} "
                        f"on {today_utc.isoformat()} (UTC)"
                    )
                sod_equity = self._sod_equity if self._sod_equity else account.equity

                # ── Daily drawdown — measured from SOD equity ─────────
                daily_dd_pct = max(
                    0.0,
                    (sod_equity - account.equity) / sod_equity * 100
                ) if sod_equity > 0 else 0.0

                # Open positions count for the configured symbol
                positions = await asyncio.to_thread(
                    mt5.positions_get, symbol=self.symbol
                )
                open_positions = len(positions) if positions else 0

                state = {
                    "balance":         account.balance,
                    "equity":          account.equity,
                    "margin":          account.margin,
                    "free_margin":     account.margin_free,
                    "margin_level":    account.margin_level,
                    "profit":          account.profit,
                    "daily_dd_pct":    round(daily_dd_pct, 4),
                    "start_of_day_equity": round(sod_equity, 2),
                    "open_positions":  open_positions,
                    "timestamp":       datetime.now().isoformat(),
                }

                await bus.publish(Topics.ACCOUNT_STATE, state)

                # ── Soft Drawdown Warnings ─────────────────────────────
                daily_limit = CFG.risk.max_daily_drawdown_pct
                total_limit = CFG.risk.max_total_drawdown_pct

                if daily_dd_pct >= daily_limit * 0.8:
                    logger.warning(
                        f"⚠️  Daily DD: {daily_dd_pct:.2f}% | "
                        f"Limit: {daily_limit}% | "
                        f"At {daily_dd_pct/daily_limit*100:.0f}% of limit"
                    )

                # Hard daily limit breach (veto enforcement is in RiskManager)
                if daily_dd_pct >= daily_limit:
                    await bus.publish(Topics.DRAWDOWN_BREACH, {
                        "type":       "daily",
                        "current":    daily_dd_pct,
                        "limit":      daily_limit,
                        "equity":     account.equity,
                        "timestamp":  datetime.now().isoformat(),
                    })

            except Exception as e:
                logger.error(f"Account monitor error: {e}", exc_info=True)

            await asyncio.sleep(CFG.data_poll_interval_sec)


# ── Singleton ─────────────────────────────────────────────────────
data_stream = DataStream()
