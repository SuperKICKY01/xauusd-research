"""
main.py — System Entry Point

Boot sequence:
  1. Configure logging
  2. Connect to MT5 (fail-fast if credentials bad)
  3. Create shared shutdown_event
  4. Register OS signal handlers (Ctrl+C / SIGTERM → graceful shutdown)
  5. Launch core tasks concurrently:
       - DataStream (tick + OHLCV + account monitor)
       - EmergencyLiquidator (watches EMERGENCY_LIQUIDATE topic)
       - SystemMonitor (MT5 health check + reconnect loop)
  6. Wait for shutdown_event
  7. Cancel tasks → disconnect MT5 → exit

Run with:
    python main.py
"""

import asyncio
import atexit
import logging
import msvcrt
import os
import signal
import sys
from datetime import datetime
from pathlib import Path

import MetaTrader5 as mt5
import pandas as pd

from config import CFG
from core.event_bus import bus, Topics
from core.mt5_connector import connector
from core.data_stream import data_stream, TIMEFRAME_MAP
from quant.signal_builder import signal_builder
from quant.indicators import add_atr
from agents.risk_manager import risk_manager
from agents.macro_bias import macro_bias_agent
from execution.order_executor import order_executor
from execution.pending_order_manager import pending_order_manager
from execution.break_even_monitor import break_even_monitor
from execution.notifier import notifier
from memory.trade_logger import trade_logger
from news.calendar_scraper import calendar_scraper
from monitoring.status_exporter import status_exporter
from utils.logger import setup_logging

# Logger for this module — must be created after setup_logging() is called
logger: logging.Logger = None  # type: ignore  (assigned in main())


# ─────────────────────────────────────────────────────────────────
# EMERGENCY LIQUIDATOR TASK
# ─────────────────────────────────────────────────────────────────

async def emergency_liquidator(shutdown_event: asyncio.Event) -> None:
    """
    Subscribes to EMERGENCY_LIQUIDATE and DRAWDOWN_BREACH topics.
    When triggered:
      1. Closes ALL open positions at market price immediately.
      2. Sets the shutdown_event to halt the entire system.

    This is the most critical safety net — it must NEVER be blocked
    by other tasks. It runs independently on the event bus.
    """
    queue = await bus.subscribe(Topics.EMERGENCY_LIQUIDATE)
    dd_queue = await bus.subscribe(Topics.DRAWDOWN_BREACH)

    logger.info("🛡️  Emergency Liquidator armed and listening.")

    while not shutdown_event.is_set():
        # Check both queues each iteration
        for q, label in [(queue, "MARGIN"), (dd_queue, "DRAWDOWN")]:
            try:
                payload = q.get_nowait()
                logger.critical(
                    f"🚨 EMERGENCY TRIGGERED [{label}] | "
                    f"Reason: {payload.get('reason', label.lower())} | "
                    f"Equity: ${payload.get('equity', 0):,.2f} | "
                    f"Details: {payload}"
                )
                await _close_all_positions(reason=payload.get("reason", "emergency"))
                shutdown_event.set()
                return

            except asyncio.QueueEmpty:
                continue
            except Exception as e:
                logger.error(f"Emergency liquidator queue error: {e}", exc_info=True)

        await asyncio.sleep(0.1)  # 100ms polling — fast enough for safety


async def _close_all_positions(reason: str = "emergency") -> None:
    """
    Market-close every open position.
    Uses IOC (Immediate Or Cancel) filling to maximize fill probability.
    Logs success/failure per ticket.

    Cancels resting pending orders FIRST — a limit must not fill into a margin
    or drawdown breach while we are liquidating. The PendingOrderManager task
    is cancelled during shutdown, so emergency cancellation is owned here.
    """
    pending = await asyncio.to_thread(mt5.orders_get)
    if pending:
        logger.warning(
            f"🚨 EMERGENCY: cancelling {len(pending)} pending order(s). Reason: {reason}"
        )
        for o in pending:
            try:
                cancel_req = {"action": mt5.TRADE_ACTION_REMOVE, "order": o.ticket}
                res = await asyncio.to_thread(mt5.order_send, cancel_req)
                if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                    logger.info(f"✅ CANCELLED pending #{o.ticket}")
                else:
                    rc = res.retcode if res else "NO_RESULT"
                    logger.error(f"❌ FAILED to cancel pending #{o.ticket} | Retcode: {rc}")
            except Exception as e:
                logger.error(f"Exception cancelling pending #{o.ticket}: {e}", exc_info=True)

    positions = await asyncio.to_thread(mt5.positions_get)

    if not positions:
        logger.info("Emergency close called — no open positions found.")
        return

    logger.warning(
        f"🚨 EMERGENCY CLOSE: Closing {len(positions)} position(s). "
        f"Reason: {reason}"
    )

    for pos in positions:
        try:
            # Determine close direction (inverse of opening direction)
            is_buy = pos.type == mt5.ORDER_TYPE_BUY
            close_type = mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY

            tick = await asyncio.to_thread(mt5.symbol_info_tick, pos.symbol)
            if tick is None:
                logger.error(
                    f"Cannot fetch tick for {pos.symbol} | "
                    f"Skipping position #{pos.ticket}"
                )
                continue

            # Use bid for sell-to-close, ask for buy-to-close
            close_price = tick.bid if close_type == mt5.ORDER_TYPE_SELL else tick.ask

            request = {
                "action":       mt5.TRADE_ACTION_DEAL,
                "symbol":       pos.symbol,
                "volume":       pos.volume,
                "type":         close_type,
                "position":     pos.ticket,
                "price":        close_price,
                "deviation":    30,                     # 30 points max slippage tolerance
                "magic":        0,
                "comment":      f"EMERGENCY:{reason[:20]}",
                "type_time":    mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,  # Immediate or cancel
            }

            result = await asyncio.to_thread(mt5.order_send, request)

            if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                logger.info(
                    f"✅ CLOSED #{pos.ticket} | {pos.symbol} | "
                    f"Vol: {pos.volume} | Price: {close_price:.5f}"
                )
            else:
                retcode = result.retcode if result else "NO_RESULT"
                comment = result.comment if result else "N/A"
                logger.error(
                    f"❌ FAILED to close #{pos.ticket} | "
                    f"Retcode: {retcode} | Comment: {comment}"
                )

        except Exception as e:
            logger.error(
                f"Exception closing position #{pos.ticket}: {e}",
                exc_info=True
            )


# ─────────────────────────────────────────────────────────────────
# SHUTDOWN LISTENER TASK
# ─────────────────────────────────────────────────────────────────

async def shutdown_listener(shutdown_event: asyncio.Event) -> None:
    """
    Bridges the SYSTEM_SHUTDOWN event-bus topic to the shared shutdown_event.

    Any agent can request a graceful, halt-only shutdown via
    core.event_bus.request_shutdown(reason, source) — e.g. OrderExecutor on an
    unconfirmed fill (0B) or TradeLogger reconciliation on a state desync (0C).
    The first such request is logged CRITICAL and sets the shutdown_event, which
    main() is already awaiting. Positions are NOT flattened (broker SL/TP remain).
    """
    queue = await bus.subscribe(Topics.SYSTEM_SHUTDOWN)
    logger.info("🧯 Shutdown listener armed (SYSTEM_SHUTDOWN → graceful halt).")

    while not shutdown_event.is_set():
        try:
            payload = await asyncio.wait_for(queue.get(), timeout=60.0)
            logger.critical(
                f"🧯 SHUTDOWN REQUESTED [{payload.get('source', '?')}] | "
                f"Reason: {payload.get('reason', 'unspecified')} | "
                f"halt-only (positions left under broker SL/TP)."
            )
            shutdown_event.set()
            return
        except asyncio.TimeoutError:
            continue
        except Exception as e:
            logger.error(f"Shutdown listener error: {e}", exc_info=True)


# ─────────────────────────────────────────────────────────────────
# SYSTEM MONITOR TASK
# ─────────────────────────────────────────────────────────────────

async def system_monitor(shutdown_event: asyncio.Event) -> None:
    """
    Runs a health check on the MT5 connection every 30 seconds.
    If the connection drops, triggers the reconnect loop.
    If reconnect fails completely, sets shutdown_event.
    """
    logger.info("🔍 System Monitor started (MT5 health check every 30s).")

    while not shutdown_event.is_set():
        await asyncio.sleep(30)

        if shutdown_event.is_set():
            break

        is_alive = await connector.health_check()
        if not is_alive:
            logger.warning("System Monitor: MT5 connection lost. Starting reconnect...")
            reconnected = await connector.reconnect_loop()
            if not reconnected:
                logger.critical(
                    "System Monitor: All reconnect attempts exhausted. "
                    "Setting shutdown event."
                )
                shutdown_event.set()
            else:
                logger.info("System Monitor: Reconnected. Resuming health checks.")


# ─────────────────────────────────────────────────────────────────
# SINGLE-INSTANCE LOCK (boot guard)
# ─────────────────────────────────────────────────────────────────

_LOCK_PATH = Path(__file__).resolve().parent / "main.py.lock"
_lock_file = None  # open handle held for the process lifetime while we own the lock


def _acquire_instance_lock() -> None:
    """
    Acquire an exclusive OS-level lock so only ONE main.py runs per bot tree.

    Locks byte 0 of main.py.lock via msvcrt.locking(LK_NBLCK). The kernel
    releases the lock on ANY process death (crash, hard-kill, power loss), so a
    stale lock cannot block a restart — the lingering file is harmless; only a
    live process can hold the lock. Our PID is stored at offset 1 (byte 0 is
    the locked, unreadable byte) purely so a refused instance can name us.

    On conflict: log and exit(2). Never kills the existing instance.
    (2nd double-instance incident in a week — 2026-06-05 and 2026-06-12 — so
    this is enforced by code, not discipline.)
    """
    global _lock_file
    _LOCK_PATH.touch(exist_ok=True)
    f = open(_LOCK_PATH, "r+")
    try:
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        holder = "unknown"
        try:
            f.seek(1)
            holder = f.read().strip() or "unknown"
        except OSError:
            pass
        f.close()
        logger.critical(
            f"⛔ Another main.py instance is already running (PID {holder}) "
            f"— refusing to start."
        )
        sys.exit(2)

    f.seek(0)
    f.write(" " + str(os.getpid()))   # offset 0 = locked byte, PID from offset 1
    f.truncate()
    f.flush()
    os.fsync(f.fileno())
    _lock_file = f
    atexit.register(_release_instance_lock)
    logger.info(f"🔒 Single-instance lock acquired (PID {os.getpid()}).")


def _release_instance_lock() -> None:
    """Unlock, close, and best-effort delete the lockfile (graceful path).
    Ungraceful deaths are covered by the kernel auto-releasing the lock."""
    global _lock_file
    f, _lock_file = _lock_file, None
    if f is None:
        return
    try:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    f.close()
    try:
        _LOCK_PATH.unlink()
    except OSError:
        pass


# ─────────────────────────────────────────────────────────────────
# HMM REGIME DETECTOR WARM-START (live-only)
# ─────────────────────────────────────────────────────────────────

async def _seed_regime_detector() -> None:
    """
    Warm-start the HMM regime detector with historical PRIMARY-TF bars so it can
    classify from the first signal cycle, instead of accruing ~1 H1 bar per live
    cycle (~4 days to reach min_warmup from a 200-bar fetch).

    No lookahead — these are closed historical bars up to 'now'. LIVE-ONLY: the
    backtest path seeds the detector via strategy_runner's window replay and is
    unaffected. Fail-soft: on any error the detector simply warms up the slow way
    (fail-open, no veto, until ready). Must run AFTER connector.connect().
    """
    if not CFG.hmm.enabled:
        return

    det = signal_builder._regime
    tf_str = CFG.symbol.timeframe_primary
    tf_int = TIMEFRAME_MAP.get(tf_str)
    if tf_int is None:
        logger.warning(f"HMM seed: unknown primary timeframe '{tf_str}' — skipping seed.")
        return

    # Enough history for a full first training window plus headroom.
    n_bars = CFG.hmm.train_window_bars + CFG.hmm.min_warmup_bars
    try:
        rates = await asyncio.to_thread(
            mt5.copy_rates_from_pos, CFG.symbol.symbol, tf_int, 0, n_bars
        )
        if rates is None or len(rates) == 0:
            logger.warning(
                f"HMM seed: no {tf_str} history returned ({mt5.last_error()}). "
                "Detector will warm up over the live session instead."
            )
            return

        df = pd.DataFrame(rates)[["time", "open", "high", "low", "close"]].copy()
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df.set_index("time", inplace=True)
        df.rename(columns={"open": "Open", "high": "High",
                           "low": "Low", "close": "Close"}, inplace=True)
        df = add_atr(df, signal_builder.ATR_PERIOD)   # detector reads ATR_<period>

        regime = det.update(df)   # observe (drops forming bar) → fit → classify
        ready = len(det._close) >= CFG.hmm.min_warmup_bars
        logger.info(
            f"🔥 HMM seed: observed {len(df)} {tf_str} bars → "
            f"{len(det._close)} closed buffered | "
            f"{'READY' if ready else 'still warming'} | seed regime={regime.value}"
        )
    except Exception as e:
        logger.error(f"HMM seed failed ({e}); detector will warm up live.", exc_info=True)


# ─────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────

async def main() -> None:
    global logger

    # ── 1. Logging ────────────────────────────────────────────────
    setup_logging(level=CFG.log_level, log_file=CFG.log_file)
    logger = logging.getLogger(__name__)

    logger.info("=" * 65)
    logger.info("🚀  XAUUSD Multi-Agent Trading System")
    logger.info(f"    Started      : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"    Symbol       : {CFG.symbol.symbol}")
    logger.info(f"    Timeframes   : {CFG.symbol.timeframe_primary} / "
                f"{CFG.symbol.timeframe_secondary} / {CFG.symbol.timeframe_entry}")
    logger.info(f"    Risk/Trade   : {CFG.risk.max_risk_per_trade_pct}%")
    logger.info(f"    Daily DD Lim : {CFG.risk.max_daily_drawdown_pct}%")
    logger.info(f"    Total DD Lim : {CFG.risk.max_total_drawdown_pct}%")
    logger.info(f"    Emergency    : Margin < {CFG.risk.emergency_margin_level}%")
    logger.info(f"    Slippage Buf : {CFG.risk.slippage_buffer_pips} pips")
    logger.info(f"    HMM Regime   : {'ON' if CFG.hmm.enabled else 'OFF'} "
                f"(veto RANGING={CFG.hmm.veto_ranging} / VOLATILE={CFG.hmm.veto_volatile})")
    logger.info(f"    DD-Mod Sizing: {'ON' if CFG.dd_mod.enabled else 'OFF'} "
                f"(ruin={CFG.dd_mod.ruin_level_frac}, gamma={CFG.dd_mod.gamma})")
    logger.info(f"    Macro Bias   : {'ON' if CFG.macro.enabled else 'OFF'} "
                f"(veto={CFG.macro.veto_enabled}) | M5 path: deterministic (no LLM gates)")
    logger.info("=" * 65)

    # ── 1b. Single-instance lock (before MT5 / task launch) ───────
    _acquire_instance_lock()

    # ── 2. MT5 Connection ─────────────────────────────────────────
    if not await connector.connect():
        logger.critical(
            "❌ MT5 connection failed at startup. "
            "Check .env credentials and ensure terminal is running."
        )
        sys.exit(1)

    # ── 2b. Warm-start the HMM regime detector (live-only, fail-soft) ──
    await _seed_regime_detector()

    # ── 3. Shared Shutdown Event ──────────────────────────────────
    shutdown_event = asyncio.Event()

    # ── 4. OS Signal Handlers ─────────────────────────────────────
    loop = asyncio.get_running_loop()

    def _on_signal(sig: signal.Signals) -> None:
        logger.warning(
            f"📛 OS signal received: {sig.name}. "
            "Initiating graceful shutdown..."
        )
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal, sig)
        except (NotImplementedError, OSError):
            # Windows: add_signal_handler not fully supported for all signals
            signal.signal(sig, lambda s, f: shutdown_event.set())

    # ── 5. Launch Core Tasks ──────────────────────────────────────
    tasks = [
        asyncio.create_task(data_stream.start(),     name="DataStream"),
        asyncio.create_task(emergency_liquidator(shutdown_event), name="EmergencyLiquidator"),
        asyncio.create_task(shutdown_listener(shutdown_event),    name="ShutdownListener"),
        asyncio.create_task(system_monitor(shutdown_event),       name="SystemMonitor"),
        asyncio.create_task(signal_builder.start(),  name="SignalBuilder"),
        asyncio.create_task(risk_manager.start(),     name="RiskManager"),
        asyncio.create_task(pending_order_manager.start(), name="PendingOrderManager"),
        asyncio.create_task(order_executor.start(),    name="OrderExecutor"),
        asyncio.create_task(break_even_monitor.start(),name="BreakEvenMonitor"),
        asyncio.create_task(notifier.start(),          name="Notifier"),
        asyncio.create_task(trade_logger.start(),      name="TradeLogger"),
        asyncio.create_task(calendar_scraper.start(),  name="CalendarScraper"),
        asyncio.create_task(status_exporter.start(shutdown_event), name="StatusExporter"),
    ]

    # HTF macro-bias agent — OFF the M5 critical path, hourly. Only launched
    # when enabled (default off). See lessons/2026-06-05-llm-htf-demotion.md.
    if CFG.macro.enabled:
        tasks.append(
            asyncio.create_task(macro_bias_agent.start(), name="MacroBiasAgent")
        )

    logger.info(f"✅ {len(tasks)} core tasks launched. System is LIVE.\n")

    # ── 6. Wait for Shutdown ──────────────────────────────────────
    await shutdown_event.wait()

    # ── 7. Graceful Shutdown ──────────────────────────────────────
    logger.warning("🛑 Shutdown initiated. Stopping data stream...")
    await data_stream.stop()

    logger.info("Cancelling background tasks...")
    for task in tasks:
        if not task.done():
            task.cancel()

    # Wait for all tasks to finish cancellation (suppress CancelledError)
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for task, result in zip(tasks, results):
        if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
            logger.error(f"Task '{task.get_name()}' exited with error: {result}")

    await connector.disconnect()
    logger.info("✅ System shutdown complete. Goodbye.\n")


# ─────────────────────────────────────────────────────────────────
# Entry Point
# ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    asyncio.run(main())
