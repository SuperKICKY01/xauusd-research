"""
core/event_bus.py — Async Pub/Sub Event Bus

Agents communicate exclusively through this bus — never by calling each other directly.
This decouples layers and makes the system non-blocking.

Usage:
    queue = await bus.subscribe(Topics.TICK_DATA)
    await bus.publish(Topics.TICK_DATA, {"bid": 2345.50, ...})
    data = await queue.get()
"""

import asyncio
import logging
from collections import defaultdict
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


class EventBus:
    """
    Lightweight async publish/subscribe message bus.
    Each subscriber gets its own asyncio.Queue — messages are never shared
    between subscribers (fan-out pattern).
    """

    def __init__(self) -> None:
        # topic -> list of subscriber queues
        self._subscribers: dict[str, list[asyncio.Queue]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def subscribe(self, topic: str, maxsize: int = 100) -> asyncio.Queue:
        """
        Subscribe to a topic. Returns a Queue that receives all future
        published events for that topic.

        Args:
            topic:   Topic name (use Topics.* constants).
            maxsize: Max queue depth before backpressure kicks in (default 100).
        """
        queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        async with self._lock:
            self._subscribers[topic].append(queue)
        logger.debug(f"[EventBus] New subscriber on '{topic}' "
                     f"(total: {len(self._subscribers[topic])})")
        return queue

    async def unsubscribe(self, topic: str, queue: asyncio.Queue) -> None:
        """Remove a subscriber queue from a topic."""
        async with self._lock:
            if topic in self._subscribers:
                try:
                    self._subscribers[topic].remove(queue)
                    logger.debug(f"[EventBus] Unsubscribed from '{topic}'")
                except ValueError:
                    pass

    async def publish(self, topic: str, data: Any) -> None:
        """
        Publish an event to all current subscribers of a topic.
        Uses put_nowait with a full-queue warning to avoid deadlocks.
        """
        async with self._lock:
            subscribers = list(self._subscribers.get(topic, []))

        if not subscribers:
            logger.debug(f"[EventBus] No subscribers for '{topic}' — event dropped.")
            return

        for queue in subscribers:
            try:
                queue.put_nowait(data)
            except asyncio.QueueFull:
                logger.warning(
                    f"[EventBus] Queue FULL for topic '{topic}' — "
                    "subscriber is lagging. Dropping oldest item."
                )
                try:
                    queue.get_nowait()   # Drop oldest
                    queue.put_nowait(data)  # Insert newest
                except Exception:
                    pass
            except Exception as e:
                logger.error(f"[EventBus] Publish error on '{topic}': {e}")

    def subscriber_count(self, topic: str) -> int:
        return len(self._subscribers.get(topic, []))


# ─────────────────────────────────────────────────────────────────
# Topic Registry — single source of truth for all event names
# ─────────────────────────────────────────────────────────────────

class Topics:
    # ── Data Layer ─────────────────────────────────────────────────
    TICK_DATA          = "tick_data"          # Latest bid/ask/spread per tick
    OHLCV_UPDATED      = "ohlcv_updated"      # Dict[str, DataFrame] per timeframe
    ACCOUNT_STATE      = "account_state"      # Balance, equity, margin, DD %

    # ── Risk & Safety ──────────────────────────────────────────────
    MARGIN_ALERT       = "margin_alert"       # Soft warning (approaching threshold)
    EMERGENCY_LIQUIDATE= "emergency_liquidate"# HARD: close all positions NOW
    DRAWDOWN_BREACH    = "drawdown_breach"    # Daily/total DD limit exceeded

    # ── Signal Pipeline ────────────────────────────────────────────
    SIGNAL_READY       = "signal_ready"       # Quant engine produced a SignalPacket
    FAST_GATE_DECISION = "fast_gate_decision" # Agent 1 output (LLM gate — demoted off M5 path 2026-06-05)
    CLOUD_GATE_DECISION= "cloud_gate_decision"# Agent 2 output (LLM gate — demoted off M5 path 2026-06-05)
    TRADE_DECISION     = "trade_decision"     # Final decision after Risk Manager veto
    MACRO_BIAS_UPDATED = "macro_bias_updated" # HTF macro agent → SignalBuilder bias cache (hourly)

    # ── Execution ──────────────────────────────────────────────────
    TRADE_EXECUTED       = "trade_executed"       # Position FILLED (pending limit triggered) — fires on fill, not placement
    TRADE_CLOSED         = "trade_closed"         # Position closed (TP/SL/manual)
    TRADE_REJECTED       = "trade_rejected"       # Risk Manager vetoed the trade
    BREAK_EVEN_TRIGGERED = "break_even_triggered" # TP2 leg SL moved to entry (capital preserved)
    POSITION_OPENED      = "position_opened"      # Lifecycle ping for one-trade-per-OB ledger
    ORDER_PENDING        = "order_pending"        # Pending limit placed — locks OB, awaits fill
    ORDER_CANCELLED      = "order_cancelled"      # Pending cancelled/expired BEFORE fill — releases OB lock
    MARKOUT_RECORDED     = "markout_recorded"     # Post-fill price markout telemetry (infra) → TradeLogger persists
    TIME_STOP_TRIGGERED  = "time_stop_triggered"  # Time-based stop flattened a trade → TradeLogger flags time_stopped

    # ── External Filters ───────────────────────────────────────────
    NEWS_BLOCK_ACTIVE  = "news_block_active"  # High-impact news window open
    NEWS_BLOCK_CLEAR   = "news_block_clear"   # News window closed, trading OK

    # ── System ─────────────────────────────────────────────────────
    SYSTEM_SHUTDOWN    = "system_shutdown"    # Graceful shutdown requested
    SYSTEM_ALERT       = "system_alert"       # Operational alert → Discord (red embed), NOT a liquidation


# ── Global singleton ──────────────────────────────────────────────
bus = EventBus()


async def request_shutdown(reason: str, source: str = "") -> None:
    """
    Request a graceful, system-wide shutdown from anywhere in the pipeline.

    Publishes Topics.SYSTEM_SHUTDOWN; main()'s _shutdown_listener picks it up and
    sets the shared shutdown_event, which unwinds tasks and disconnects MT5 via
    the normal graceful path. This is HALT-ONLY — open positions are left under
    their broker-side SL/TP (no flatten). Used by OrderExecutor (unconfirmed fill)
    and TradeLogger reconciliation (state desync).
    """
    await bus.publish(Topics.SYSTEM_SHUTDOWN, {
        "reason":    reason,
        "source":    source,
        "timestamp": datetime.now().isoformat(),
    })


async def send_alert(title: str, description: str,
                     fields: list[dict] | None = None) -> None:
    """
    Publish an operational alert to Topics.SYSTEM_ALERT. The Notifier renders it
    as a red Discord embed. This is a NOTIFICATION ONLY — it does not liquidate or
    shut anything down (cf. EMERGENCY_LIQUIDATE). Pair with request_shutdown() when
    the alert should also halt the system (0B unconfirmed fill, 0C state desync).
    """
    await bus.publish(Topics.SYSTEM_ALERT, {
        "title":       title,
        "description": description,
        "fields":      fields or [],
        "timestamp":   datetime.now().isoformat(),
    })
