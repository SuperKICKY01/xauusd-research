"""
memory/trade_logger.py — SQLite Trade Logger

Subscribes to:
  TRADE_EXECUTED  → inserts a row into the `trades` table
  TRADE_REJECTED  → inserts a row into the `rejections` table

A third background coroutine (_position_monitor) polls MT5 every 30s to
detect when logged positions close, then fetches deal history to record
the actual outcome (WIN_TP1 / WIN_TP2 / LOSS / BE_HIT / TIME_STOP /
MANUAL_CLOSE / UNRESOLVED) and PnL.
This closes the feedback loop that enables Phase 5 RAG learning.

Database path: memory/trades.db  (created on first run)

All sqlite3 calls run inside asyncio.to_thread() — never block the event loop.
"""

import asyncio
import gc
import json
import logging
import os
import sqlite3
from datetime import datetime
from typing import Optional

import MetaTrader5 as mt5

from config import CFG
from core.event_bus import bus, Topics, request_shutdown, send_alert
from agents.risk_manager import TradeDecision
from execution.order_executor import ORDER_MAGIC, ORDER_COMMENT
from memory.dual_tp_outcome import accumulate_leg_close

logger = logging.getLogger(__name__)

_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trades.db")

# ─────────────────────────────────────────────────────────────────
# Schema DDL
# ─────────────────────────────────────────────────────────────────

_DDL_TRADES = """
CREATE TABLE IF NOT EXISTS trades (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id         INTEGER,
    order_ids        TEXT,
    timestamp        TEXT    NOT NULL,
    symbol           TEXT    NOT NULL,
    direction        TEXT    NOT NULL,
    bias             TEXT    NOT NULL,
    confluence_score INTEGER,
    market_state     TEXT,
    atr              REAL,
    atr_multiplier   REAL,
    ema_13           REAL,
    ema_21           REAL,
    ema_50           REAL,
    lot_size         REAL,
    entry_price      REAL,
    stop_loss        REAL,
    take_profit_1    REAL,
    take_profit_2    REAL,
    sl_distance      REAL,
    rr_ratio         REAL,
    risk_amount      REAL,
    balance_at_entry REAL,
    equity_at_entry  REAL,
    daily_dd_pct     REAL,
    agent            TEXT,
    was_escalated    INTEGER DEFAULT 0,
    agent_reasoning  TEXT,
    regime           TEXT,
    break_even       INTEGER DEFAULT 0,
    time_stopped     INTEGER DEFAULT 0,
    fill_time_ms     INTEGER,
    fill_price       REAL,
    markout_100ms    REAL,
    markout_1s       REAL,
    markout_5s       REAL,
    markout_30s      REAL,
    outcome          TEXT    DEFAULT 'OPEN',
    outcome_price    REAL,
    outcome_pnl      REAL,
    close_timestamp  TEXT,
    closed_legs      TEXT
);
"""

# Columns added after the table first shipped. _init_db_sync ALTERs them onto an
# existing trades.db (CREATE TABLE IF NOT EXISTS will not add columns to a table
# that already exists). Keep in sync with the DDL above.
#   regime     (0D) — HMM regime label at entry (TRENDING/RANGING/VOLATILE/UNKNOWN)
#   break_even (0D) — 0 = SL at original, 1 = TP2 leg SL moved to break-even
#   time_stopped — 0 = normal, 1 = trade flattened by the time-based stop
#       (set by the TIME_STOP_TRIGGERED listener; authoritative TIME_STOP signal).
#   fill_time_ms / fill_price / markout_* — post-fill markout telemetry, written
#       ~30s after the fill by the MARKOUT_RECORDED listener (infra-only).
#   closed_legs — per-leg close accumulator (JSON list of {ticket,outcome,pnl,
#       price,ts}); a dual-TP row stays OPEN until all order_ids legs land here,
#       then the combined outcome + summed pnl are written. See dual_tp_outcome.
_MIGRATIONS: list[tuple[str, str]] = [
    ("regime",     "ALTER TABLE trades ADD COLUMN regime TEXT"),
    ("break_even", "ALTER TABLE trades ADD COLUMN break_even INTEGER DEFAULT 0"),
    ("time_stopped", "ALTER TABLE trades ADD COLUMN time_stopped INTEGER DEFAULT 0"),
    ("fill_time_ms",  "ALTER TABLE trades ADD COLUMN fill_time_ms INTEGER"),
    ("fill_price",    "ALTER TABLE trades ADD COLUMN fill_price REAL"),
    ("markout_100ms", "ALTER TABLE trades ADD COLUMN markout_100ms REAL"),
    ("markout_1s",    "ALTER TABLE trades ADD COLUMN markout_1s REAL"),
    ("markout_5s",    "ALTER TABLE trades ADD COLUMN markout_5s REAL"),
    ("markout_30s",   "ALTER TABLE trades ADD COLUMN markout_30s REAL"),
    ("closed_legs",   "ALTER TABLE trades ADD COLUMN closed_legs TEXT"),
]

_DDL_REJECTIONS = """
CREATE TABLE IF NOT EXISTS rejections (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp        TEXT    NOT NULL,
    symbol           TEXT,
    reason           TEXT    NOT NULL,
    agent            TEXT,
    confluence_score INTEGER,
    bias             TEXT,
    entry_price      REAL,
    atr              REAL
);
"""

_DDL_INDICES = [
    "CREATE INDEX IF NOT EXISTS idx_trades_symbol    ON trades (symbol);",
    "CREATE INDEX IF NOT EXISTS idx_trades_direction ON trades (direction);",
    "CREATE INDEX IF NOT EXISTS idx_trades_outcome   ON trades (outcome);",
    "CREATE INDEX IF NOT EXISTS idx_trades_ts        ON trades (timestamp DESC);",
]

# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────

def _classify_outcome(
    close_deal,
    stop_loss:    float,
    tp1:          float,
    tp2:          float,
    atr:          float,
    break_even:   int = 0,
    time_stopped: int = 0,
) -> str:
    """
    Classify a closed position. PRIMARY signal is the broker's deal.reason
    (authoritative for SL/TP/EXPERT/CLIENT); price + the break_even / time_stopped
    flags disambiguate the cases reason can't (TP1 vs TP2, LOSS vs BE_HIT, and our
    time-stop). Price-tolerance matching is the fallback when reason is absent.
    DEAL_REASON_* are read via getattr so a constant the installed MetaTrader5
    package omits degrades to the fallback rather than raising.
    """
    price  = close_deal.price
    reason = getattr(close_deal, "reason", None)

    R_SL = getattr(mt5, "DEAL_REASON_SL", None)
    R_TP = getattr(mt5, "DEAL_REASON_TP", None)
    R_SO = getattr(mt5, "DEAL_REASON_SO", None)
    manual_reasons = {
        r for r in (
            getattr(mt5, "DEAL_REASON_CLIENT", None),
            getattr(mt5, "DEAL_REASON_MOBILE", None),
            getattr(mt5, "DEAL_REASON_WEB",    None),
            getattr(mt5, "DEAL_REASON_EXPERT", None),
        ) if r is not None
    }

    # 1. Time-stop flag is authoritative (set by the TIME_STOP_TRIGGERED listener).
    if time_stopped:
        return "TIME_STOP"

    # 2. Broker reason — primary signal.
    if reason is not None:
        if R_TP is not None and reason == R_TP:
            return "WIN_TP2" if abs(price - tp2) <= abs(price - tp1) else "WIN_TP1"
        if R_SL is not None and reason == R_SL:
            return "BE_HIT" if break_even else "LOSS"
        if R_SO is not None and reason == R_SO:
            return "LOSS"
        if reason in manual_reasons:
            return "MANUAL_CLOSE"

    # 3. Fallback: reason unavailable → price tolerance match (BE only if flagged).
    tolerance = max(atr * 0.3, 0.50)
    if tp2 and abs(price - tp2) <= tolerance:
        return "WIN_TP2"
    if tp1 and abs(price - tp1) <= tolerance:
        return "WIN_TP1"
    if stop_loss and abs(price - stop_loss) <= tolerance:
        return "BE_HIT" if break_even else "LOSS"
    if break_even:
        return "BE_HIT"
    return "MANUAL_CLOSE"


# ─────────────────────────────────────────────────────────────────
# TradeLogger
# ─────────────────────────────────────────────────────────────────

class TradeLogger:
    """
    Async SQLite trade log with live outcome tracking.

    _tracked maps MT5 ticket → trade context dict so the position monitor
    can look up DB ids and price levels when a position closes.
    Multiple tickets (TP1 + TP2 legs) share one db_id; each leg's close is
    accumulated into closed_legs and the row stays 'OPEN' until ALL legs
    close, then one combined outcome + summed pnl is written.
    """

    def __init__(self) -> None:
        # ticket_int → {db_id, timestamp, stop_loss, take_profit_1, take_profit_2, atr}
        self._tracked: dict[int, dict] = {}

    async def start(self) -> None:
        logger.info(f"📝 TradeLogger online — DB: {_DB_PATH}")
        await self._init_db()
        await self._resolve_orphaned_opens()
        await asyncio.gather(
            self._executed_listener(),
            self._rejected_listener(),
            self._break_even_listener(),
            self._time_stop_listener(),
            self._markout_listener(),
            self._position_monitor(),
            self._reconciliation_monitor(),
            return_exceptions=True,
        )

    # ─────────────────────────────────────────────────────────────
    # DB Init
    # ─────────────────────────────────────────────────────────────

    async def _init_db(self) -> None:
        await asyncio.to_thread(self._init_db_sync)

    def _init_db_sync(self) -> None:
        os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
        conn = sqlite3.connect(_DB_PATH)
        try:
            conn.execute(_DDL_TRADES)
            conn.execute(_DDL_REJECTIONS)
            for idx in _DDL_INDICES:
                conn.execute(idx)

            # Idempotent column migration for an existing trades.db.
            existing = {row[1] for row in conn.execute("PRAGMA table_info(trades)")}
            for col, ddl in _MIGRATIONS:
                if col not in existing:
                    conn.execute(ddl)
                    logger.info(f"📝 TradeLogger schema migration: added column '{col}'.")

            conn.commit()
        finally:
            conn.close()

    # ─────────────────────────────────────────────────────────────
    # Event Listeners
    # ─────────────────────────────────────────────────────────────

    async def _executed_listener(self) -> None:
        queue = await bus.subscribe(Topics.TRADE_EXECUTED)
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=60.0)
                await self._log_executed(event)
            except asyncio.TimeoutError:
                pass
            except Exception as e:
                logger.error(f"TradeLogger executed listener: {e}", exc_info=True)

    async def _rejected_listener(self) -> None:
        queue = await bus.subscribe(Topics.TRADE_REJECTED)
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=60.0)
                await self._log_rejected(event)
            except asyncio.TimeoutError:
                pass
            except Exception as e:
                logger.error(f"TradeLogger rejected listener: {e}", exc_info=True)

    async def _break_even_listener(self) -> None:
        """
        Persists break-even state (0D). BreakEvenMonitor publishes
        BREAK_EVEN_TRIGGERED with the TP2 leg's `ticket` once its SL moves to
        break-even; we flag the owning trade row so the move survives a hard-kill
        + reboot (a restored position must not be re-managed from scratch).
        """
        queue = await bus.subscribe(Topics.BREAK_EVEN_TRIGGERED)
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=60.0)
                ticket = event.get("ticket")
                if ticket is None:
                    continue
                db_id = await asyncio.to_thread(self._mark_break_even_sync, int(ticket))
                if db_id is not None:
                    logger.info(
                        f"📝 Break-even persisted | DB #{db_id} | TP2 ticket #{ticket}"
                    )
                else:
                    logger.warning(
                        f"TradeLogger: BREAK_EVEN_TRIGGERED for ticket #{ticket} "
                        "matched no OPEN trade row."
                    )
            except asyncio.TimeoutError:
                pass
            except Exception as e:
                logger.error(f"TradeLogger break-even listener: {e}", exc_info=True)

    def _mark_break_even_sync(self, ticket: int) -> Optional[int]:
        """
        Set break_even=1 on the OPEN trade row that owns `ticket`. The TP2 ticket
        lives in the order_ids JSON array (order_id holds the primary/TP1 leg), so
        we parse the JSON for an exact integer match rather than a fuzzy LIKE.
        Restart-safe: works whether or not the ticket is still in _tracked.
        Returns the matched db id, else None.
        """
        try:
            conn = sqlite3.connect(_DB_PATH)
            try:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT id, order_id, order_ids FROM trades WHERE outcome = 'OPEN'"
                ).fetchall()
                for r in rows:
                    tickets: set[int] = set()
                    if r["order_id"] is not None:
                        tickets.add(int(r["order_id"]))
                    if r["order_ids"]:
                        try:
                            tickets.update(int(t) for t in json.loads(r["order_ids"]))
                        except (ValueError, TypeError, json.JSONDecodeError):
                            pass
                    if ticket in tickets:
                        conn.execute(
                            "UPDATE trades SET break_even = 1 WHERE id = ?", (r["id"],)
                        )
                        conn.commit()
                        return r["id"]
                return None
            finally:
                conn.close()
        except Exception as e:
            logger.error(f"TradeLogger break-even update error: {e}")
            return None

    async def _time_stop_listener(self) -> None:
        """
        Flag trades flattened by the time-based stop. BreakEvenMonitor publishes
        TIME_STOP_TRIGGERED with the leg tickets the moment it flattens; we set
        time_stopped=1 on the owning row so the close resolver labels it TIME_STOP
        (authoritative, broker-independent — mirrors the break-even mechanism).
        """
        queue = await bus.subscribe(Topics.TIME_STOP_TRIGGERED)
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=60.0)
                ticket = event.get("ticket")
                if ticket is None:
                    continue
                db_id = await asyncio.to_thread(self._mark_time_stopped_sync, int(ticket))
                if db_id is not None:
                    logger.info(f"⏱️  Time-stop persisted | DB #{db_id} | ticket #{ticket}")
                else:
                    logger.warning(
                        f"TradeLogger: TIME_STOP_TRIGGERED for ticket #{ticket} "
                        "matched no trade row."
                    )
            except asyncio.TimeoutError:
                pass
            except Exception as e:
                logger.error(f"TradeLogger time-stop listener: {e}", exc_info=True)

    def _mark_time_stopped_sync(self, ticket: int) -> Optional[int]:
        """
        Set time_stopped=1 on the row owning `ticket` (order_id or the order_ids
        JSON array). NOT restricted to OPEN rows: the flatten closes the position,
        so the 30s monitor may resolve the close in the same window — the flag must
        land whether the row is still OPEN or already resolved.
        """
        try:
            conn = sqlite3.connect(_DB_PATH)
            try:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT id, order_id, order_ids FROM trades ORDER BY id DESC"
                ).fetchall()
                for r in rows:
                    tickets: set[int] = set()
                    if r["order_id"] is not None:
                        tickets.add(int(r["order_id"]))
                    if r["order_ids"]:
                        try:
                            tickets.update(int(t) for t in json.loads(r["order_ids"]))
                        except (ValueError, TypeError, json.JSONDecodeError):
                            pass
                    if ticket in tickets:
                        conn.execute("UPDATE trades SET time_stopped = 1 WHERE id = ?", (r["id"],))
                        conn.commit()
                        return r["id"]
                return None
            finally:
                conn.close()
        except Exception as e:
            logger.error(f"TradeLogger time-stop update error: {e}")
            return None

    async def _markout_listener(self) -> None:
        """
        Persist post-fill markout telemetry (infra-only). OrderExecutor samples
        the mid price at 100ms/1s/5s/30s after a confirmed fill and publishes
        MARKOUT_RECORDED keyed by the primary ticket; we write it onto the owning
        trade row. Same late-update-by-ticket pattern as break-even, but matches
        regardless of outcome (a fast SL can close the trade inside the 30s window).
        """
        queue = await bus.subscribe(Topics.MARKOUT_RECORDED)
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=60.0)
                ticket = event.get("ticket")
                if ticket is None:
                    continue
                db_id = await asyncio.to_thread(self._record_markout_sync, int(ticket), event)
                if db_id is not None:
                    logger.info(f"📈 Markout persisted | DB #{db_id} | ticket #{ticket}")
                else:
                    logger.warning(
                        f"TradeLogger: MARKOUT_RECORDED for ticket #{ticket} "
                        "matched no trade row."
                    )
            except asyncio.TimeoutError:
                pass
            except Exception as e:
                logger.error(f"TradeLogger markout listener: {e}", exc_info=True)

    def _record_markout_sync(self, ticket: int, event: dict) -> Optional[int]:
        """
        Write the markout columns onto the trade row that owns `ticket` (matched
        on order_id or the order_ids JSON array, same as break-even). NOT limited
        to OPEN rows — markout describes the fill, valid even if the trade already
        closed within the 30s sampling window. Returns the matched db id, else None.
        """
        try:
            conn = sqlite3.connect(_DB_PATH)
            try:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT id, order_id, order_ids FROM trades ORDER BY id DESC"
                ).fetchall()
                for r in rows:
                    tickets: set[int] = set()
                    if r["order_id"] is not None:
                        tickets.add(int(r["order_id"]))
                    if r["order_ids"]:
                        try:
                            tickets.update(int(t) for t in json.loads(r["order_ids"]))
                        except (ValueError, TypeError, json.JSONDecodeError):
                            pass
                    if ticket in tickets:
                        conn.execute(
                            "UPDATE trades SET "
                            "  fill_time_ms=?, fill_price=?, markout_100ms=?, "
                            "  markout_1s=?, markout_5s=?, markout_30s=? "
                            "WHERE id=?",
                            (
                                event.get("fill_time_ms"),
                                event.get("fill_price"),
                                event.get("markout_100ms"),
                                event.get("markout_1s"),
                                event.get("markout_5s"),
                                event.get("markout_30s"),
                                r["id"],
                            ),
                        )
                        conn.commit()
                        return r["id"]
                return None
            finally:
                conn.close()
        except Exception as e:
            logger.error(f"TradeLogger markout update error: {e}")
            return None

    # ─────────────────────────────────────────────────────────────
    # Log Writers
    # ─────────────────────────────────────────────────────────────

    async def _log_executed(self, event: dict) -> None:
        decision: TradeDecision = event.get("decision")
        orders      = event.get("orders", [])
        entry_price = event.get("entry_price", 0.0)

        if decision is None or not orders:
            return

        signal        = decision.gate_decision.signal
        was_escalated = decision.gate_decision.agent == "CloudGate"
        primary_ticket = orders[0]["order"]
        order_ids_json = json.dumps([o["order"] for o in orders])

        row = (
            primary_ticket,
            order_ids_json,
            decision.timestamp.isoformat(),
            decision.symbol,
            decision.direction,
            signal.bias.value          if signal else "UNKNOWN",
            signal.score               if signal else 0,
            signal.market_state.value  if signal else "",
            signal.atr                 if signal else 0.0,
            signal.atr_multiplier      if signal else 1.0,
            signal.ema_13              if signal else None,
            signal.ema_21              if signal else None,
            signal.ema_50              if signal else None,
            decision.lot_size,
            entry_price,
            decision.stop_loss,
            decision.take_profit_1,
            decision.take_profit_2,
            decision.sl_distance,
            decision.rr_ratio,
            decision.risk_amount,
            decision.account_balance,
            decision.account_equity,
            decision.daily_dd_pct,
            decision.gate_decision.agent,
            1 if was_escalated else 0,
            (decision.gate_decision.reasoning or "")[:500],
            signal.regime if signal else None,
        )

        db_id = await asyncio.to_thread(self._insert_trade_sync, row)
        if db_id is None:
            return

        trade_context = {
            "db_id":        db_id,
            "timestamp":    decision.timestamp.isoformat(),
            "stop_loss":    decision.stop_loss,
            "take_profit_1":decision.take_profit_1,
            "take_profit_2":decision.take_profit_2,
            "atr":          signal.atr if signal else 0.5,
        }
        for order in orders:
            self._tracked[order["order"]] = trade_context

        logger.info(
            f"📝 Trade logged | DB #{db_id} | "
            f"{decision.direction} {decision.symbol} | tickets={order_ids_json}"
        )

    def _insert_trade_sync(self, row: tuple) -> Optional[int]:
        sql = (
            "INSERT INTO trades ("
            "  order_id, order_ids, timestamp, symbol, direction, bias,"
            "  confluence_score, market_state, atr, atr_multiplier,"
            "  ema_13, ema_21, ema_50, lot_size, entry_price,"
            "  stop_loss, take_profit_1, take_profit_2, sl_distance,"
            "  rr_ratio, risk_amount, balance_at_entry, equity_at_entry,"
            "  daily_dd_pct, agent, was_escalated, agent_reasoning, regime"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        )
        try:
            conn = sqlite3.connect(_DB_PATH)
            try:
                cur = conn.execute(sql, row)
                conn.commit()
                return cur.lastrowid
            finally:
                conn.close()
        except Exception as e:
            logger.error(f"TradeLogger insert error: {e}")
            return None

    async def _log_rejected(self, event: dict) -> None:
        signal = event.get("signal")
        row = (
            event.get("timestamp", datetime.now().isoformat()),
            CFG.symbol.symbol,
            str(event.get("reason", ""))[:500],
            str(event.get("agent",  ""))[:50],
            signal.score           if signal else None,
            signal.bias.value      if signal else None,
            signal.suggested_entry if signal else None,
            signal.atr             if signal else None,
        )
        await asyncio.to_thread(self._insert_rejection_sync, row)

    def _insert_rejection_sync(self, row: tuple) -> None:
        sql = (
            "INSERT INTO rejections ("
            "  timestamp, symbol, reason, agent,"
            "  confluence_score, bias, entry_price, atr"
            ") VALUES (?,?,?,?,?,?,?,?)"
        )
        try:
            conn = sqlite3.connect(_DB_PATH)
            try:
                conn.execute(sql, row)
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            logger.error(f"TradeLogger rejection insert error: {e}")

    # ─────────────────────────────────────────────────────────────
    # Position Monitor
    # ─────────────────────────────────────────────────────────────

    async def _position_monitor(self) -> None:
        """
        Polls MT5 every 30s. When a tracked ticket disappears from open positions,
        fetches deal history to classify and record the final outcome.

        Uses mt5.DEAL_ENTRY_OUT to find the closing deal for the position.
        Each leg's close is accumulated (see _record_leg_close_sync); a dual-TP
        trade stays 'OPEN' until both legs close, then records one combined
        outcome with summed pnl.
        """
        while True:
            try:
                await asyncio.sleep(30)

                if not self._tracked:
                    continue

                open_positions = await asyncio.to_thread(mt5.positions_get) or []
                open_tickets   = {p.ticket for p in open_positions}

                for ticket in list(self._tracked.keys()):
                    if ticket not in open_tickets:
                        context = self._tracked.pop(ticket)
                        await self._resolve_closed_position(ticket, context)

            except Exception as e:
                logger.error(f"TradeLogger position monitor: {e}", exc_info=True)

    async def _resolve_closed_position(self, ticket: int, context: dict) -> None:
        """Fetch the closing deal from MT5 history and update the DB row.

        Filters by `position=ticket` directly. The previous implementation
        used a (from_dt, to_dt) window built from naive local datetimes,
        which mt5.history_deals_get interprets as broker server time —
        the offset between local time and the broker (typically a few
        hours) pushed the window past every real close, so 100% of closes
        fell through to the MANUAL_CLOSE/$0 fallback below.
        """
        raw_deals = await asyncio.to_thread(mt5.history_deals_get, position=ticket)
        deals = raw_deals if raw_deals is not None else []

        close_deal = next(
            (d for d in deals if d.entry == mt5.DEAL_ENTRY_OUT),
            None,
        )

        if close_deal:
            flags = await asyncio.to_thread(self._fetch_flags_sync, context["db_id"])
            leg_outcome = _classify_outcome(
                close_deal,
                context["stop_loss"],
                context["take_profit_1"],
                context["take_profit_2"],
                context["atr"],
                break_even   = flags["break_even"],
                time_stopped = flags["time_stopped"],
            )
            leg = {
                "ticket":  ticket,
                "outcome": leg_outcome,
                "pnl":     float(close_deal.profit),
                "price":   float(close_deal.price),
                "ts":      datetime.now().isoformat(),
            }
        else:
            leg = {
                "ticket":  ticket,
                "outcome": "UNRESOLVED",
                "pnl":     0.0,
                "price":   0.0,
                "ts":      datetime.now().isoformat(),
            }

        # Accumulate this leg; the row stays OPEN until ALL order_ids legs close.
        is_final, combined, summed, n_closed, n_total = await asyncio.to_thread(
            self._record_leg_close_sync, context["db_id"], leg
        )
        if is_final:
            logger.info(
                f"📝 Trade finalized | DB #{context['db_id']} | {combined} | "
                f"net=${summed:+.2f} | last leg #{ticket} {leg['outcome']} "
                f"({n_closed}/{n_total} legs)"
            )
        else:
            logger.info(
                f"📝 Leg closed | DB #{context['db_id']} | #{ticket} {leg['outcome']} "
                f"pnl=${leg['pnl']:+.2f} | trade still OPEN ({n_closed}/{n_total} legs)"
            )

    def _record_leg_close_sync(
        self,
        db_id: int,
        leg:   dict,
    ) -> tuple[bool, Optional[str], float, int, int]:
        """Fold one leg's close into the trade row in ONE transaction.

        Read-modify-write under BEGIN IMMEDIATE (autocommit conn + busy_timeout
        so a concurrent leg-close on the same row serialises rather than racing):
        append the leg to closed_legs (idempotent by ticket via the pure
        accumulator), sum pnl across legs, keep outcome='OPEN' until every
        order_ids leg has closed — only then write the combined outcome + summed
        pnl + final close_timestamp. Accumulation logic lives in
        memory.dual_tp_outcome.accumulate_leg_close.

        Returns (is_final, combined_outcome|None, summed_pnl, n_closed, n_total).
        """
        try:
            conn = sqlite3.connect(_DB_PATH, isolation_level=None)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA busy_timeout=5000")
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT order_ids, closed_legs FROM trades WHERE id=?", (db_id,)
                ).fetchone()
                if row is None:
                    conn.execute("ROLLBACK")
                    logger.error(f"TradeLogger: db_id {db_id} not found recording leg close.")
                    return (False, None, 0.0, 0, 0)

                order_ids = json.loads(row["order_ids"]) if row["order_ids"] else []
                prior     = json.loads(row["closed_legs"]) if row["closed_legs"] else []

                closed, is_final, combined, summed = accumulate_leg_close(order_ids, prior, leg)

                if is_final:
                    conn.execute(
                        "UPDATE trades SET closed_legs=?, outcome=?, outcome_pnl=?, "
                        "outcome_price=?, close_timestamp=? WHERE id=?",
                        (json.dumps(closed), combined, summed,
                         leg["price"], leg["ts"], db_id),
                    )
                else:
                    conn.execute(
                        "UPDATE trades SET closed_legs=?, outcome_pnl=? WHERE id=?",
                        (json.dumps(closed), summed, db_id),
                    )
                conn.execute("COMMIT")
                return (is_final, combined, summed, len(closed), len(order_ids))
            finally:
                conn.close()
        except Exception as e:
            logger.error(f"TradeLogger leg-close record error (db_id={db_id}): {e}")
            return (False, None, 0.0, 0, 0)

    def _fetch_flags_sync(self, db_id: int) -> dict:
        """Read the break_even / time_stopped flags for a row (for classification)."""
        try:
            conn = sqlite3.connect(_DB_PATH)
            conn.row_factory = sqlite3.Row
            try:
                r = conn.execute(
                    "SELECT break_even, time_stopped FROM trades WHERE id = ?", (db_id,)
                ).fetchone()
                if r is None:
                    return {"break_even": 0, "time_stopped": 0}
                return {"break_even": r["break_even"] or 0, "time_stopped": r["time_stopped"] or 0}
            finally:
                conn.close()
        except Exception as e:
            logger.error(f"TradeLogger flag fetch error: {e}")
            return {"break_even": 0, "time_stopped": 0}

    # ─────────────────────────────────────────────────────────────
    # Orphan recovery — runs once on startup
    # ─────────────────────────────────────────────────────────────

    async def _resolve_orphaned_opens(self) -> None:
        """Reconcile rows still marked OPEN against MT5 reality.

        A shutdown between a position closing on the broker and the next
        30s position-monitor poll leaves the DB row stranded as OPEN
        forever (e.g. DB #5 / ticket 739249650 from 2026-05-11). On boot
        we sweep:
          - position still alive in MT5 → re-add to _tracked, monitor takes over
          - position gone from MT5      → call _resolve_closed_position
        """
        rows = await asyncio.to_thread(self._fetch_open_rows_sync)
        if not rows:
            return

        open_positions = await asyncio.to_thread(mt5.positions_get) or []
        live_tickets   = {p.ticket for p in open_positions}

        recovered = resolved = 0
        for row in rows:
            # Item D: recover EVERY leg of this still-OPEN trade, not just the
            # primary order_id. A dual-TP whose TP1 already closed (row kept OPEN
            # by the accumulator) must re-track its open runner AND re-resolve the
            # closed TP1 leg, or the runner goes unmonitored and the row sticks
            # OPEN forever. order_ids is authoritative; fall back to [order_id]
            # for legacy rows written before it existed.
            try:
                legs = json.loads(row["order_ids"]) if row["order_ids"] else []
            except (ValueError, TypeError, json.JSONDecodeError):
                legs = []
            if not legs and row["order_id"] is not None:
                legs = [row["order_id"]]

            context = {
                "db_id":         row["id"],
                "timestamp":     row["timestamp"],
                "stop_loss":     row["stop_loss"],
                "take_profit_1": row["take_profit_1"],
                "take_profit_2": row["take_profit_2"],
                "atr":           row["atr"] or 0.5,
            }

            for ticket in legs:
                if ticket in live_tickets:
                    self._tracked[ticket] = context
                    logger.info(
                        f"♻️  TradeLogger RECOVERED tracking | DB #{row['id']} | "
                        f"leg #{ticket} (still open on broker)"
                    )
                    recovered += 1
                else:
                    # Already closed while we were down → accumulate it now
                    # (idempotent: a leg already in closed_legs is a no-op).
                    await self._resolve_closed_position(ticket, context)
                    resolved += 1

        logger.info(
            f"📝 TradeLogger orphan sweep complete | "
            f"recovered={recovered} | resolved={resolved} | scanned={len(rows)}"
        )

    def _fetch_open_rows_sync(self) -> list[dict]:
        sql = (
            "SELECT id, order_id, order_ids, timestamp, stop_loss, "
            "       take_profit_1, take_profit_2, atr "
            "FROM trades WHERE outcome = 'OPEN' AND order_id IS NOT NULL"
        )
        try:
            conn = sqlite3.connect(_DB_PATH)
            conn.row_factory = sqlite3.Row
            try:
                return [dict(r) for r in conn.execute(sql).fetchall()]
            finally:
                conn.close()
        except Exception as e:
            logger.error(f"TradeLogger orphan fetch error: {e}")
            return []

    # ─────────────────────────────────────────────────────────────
    # State Reconciliation Heartbeat (0C)
    # ─────────────────────────────────────────────────────────────

    async def _reconciliation_monitor(self) -> None:
        """
        Continuously reconcile MT5 reality against trades.db (0C).

        Every CFG.reconcile_interval_sec, compare our open MT5 positions (filtered
        by ORDER_MAGIC + comment) against the set of OPEN db tickets. Two desync
        classes:
          A — a live position with no OPEN db row (untracked open).
          B — an OPEN db row whose position is gone AND has no closing deal.
        A desync must persist for CFG.reconcile_debounce_cycles consecutive cycles
        before it escalates → Discord alert + HALT-ONLY shutdown. The debounce, the
        Class-A position-age grace (≥ fill-confirm window), and the Class-B
        DEAL_ENTRY_OUT guard together keep this from racing the in-flight fill
        confirmation (0B) or the 30s close-monitor.
        """
        interval = CFG.reconcile_interval_sec
        debounce = CFG.reconcile_debounce_cycles
        streak: dict[int, int] = {}   # ticket → consecutive desync cycles

        logger.info(
            f"🔁 Reconciliation heartbeat online — every {interval:.0f}s, "
            f"debounce {debounce} cycle(s)."
        )

        while True:
            try:
                await asyncio.sleep(interval)

                desynced = await self._scan_desync()   # {ticket: reason}

                # Clear streaks for tickets that recovered; advance the rest.
                for t in list(streak.keys()):
                    if t not in desynced:
                        streak.pop(t, None)
                for t in desynced:
                    streak[t] = streak.get(t, 0) + 1

                confirmed = {t: desynced[t] for t in desynced if streak[t] >= debounce}
                if not confirmed:
                    continue

                detail = " | ".join(f"#{t}: {reason}" for t, reason in confirmed.items())
                logger.critical(f"🔁 STATE DESYNC confirmed — {detail}")
                await send_alert(
                    "🔁 STATE DESYNC — halting",
                    "MT5 positions and trades.db disagree after debounce. "
                    "Halting (positions left under broker SL/TP) for manual review.",
                    fields=[{"name": f"#{t}", "value": reason[:200], "inline": False}
                            for t, reason in confirmed.items()],
                )
                await request_shutdown(f"state desync: {detail}", "Reconciliation")
                return

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"TradeLogger reconciliation monitor: {e}", exc_info=True)

    async def _scan_desync(self) -> dict[int, str]:
        """One reconciliation pass → {ticket: reason} for current desyncs."""
        out: dict[int, str] = {}

        positions = await asyncio.to_thread(mt5.positions_get) or []
        ours = [p for p in positions
                if p.magic == ORDER_MAGIC and p.comment.startswith(ORDER_COMMENT)]
        broker_tickets = {p.ticket for p in ours}

        db_tickets = await asyncio.to_thread(self._fetch_open_tickets_sync)
        # All tickets the bot ever logged (any outcome). A dual-TP leg whose
        # sibling already closed flips its row off 'OPEN', but the leg is still
        # in this set — so Class A won't mis-flag a still-open runner as an
        # untracked open. (Class B below still uses the OPEN set.)
        known_tickets = await asyncio.to_thread(self._fetch_all_known_tickets_sync)

        # Class A — live position the bot never logged (referenced by NO db row).
        for p in ours:
            if p.ticket in known_tickets:
                continue
            # Grace: a just-opened position may still be inside the fill-confirm /
            # TRADE_EXECUTED → DB-write window. Only flag once it has been open
            # longer than the max confirm window, by which point its row must exist.
            age = await self._position_age_sec(p)
            if age is not None and age < CFG.execution.fill_confirm_timeout_sec:
                continue
            out[p.ticket] = "live position not in trades.db (untracked open)"

        # Class B — OPEN db row whose position is gone and not a normal close.
        for ticket in db_tickets:
            if ticket in broker_tickets:
                continue
            deals = await asyncio.to_thread(mt5.history_deals_get, position=ticket) or []
            if any(d.entry == mt5.DEAL_ENTRY_OUT for d in deals):
                continue   # genuine close — the 30s monitor will resolve it.
            out[ticket] = "trades.db OPEN but no live position and no closing deal"

        return out

    async def _position_age_sec(self, position) -> Optional[float]:
        """Seconds since the position opened, in broker server time (tick.time −
        position.time). Returns None if the tick is unavailable."""
        tick = await asyncio.to_thread(mt5.symbol_info_tick, position.symbol)
        if tick is None or not getattr(tick, "time", None):
            return None
        return float(tick.time) - float(position.time)

    def _fetch_open_tickets_sync(self) -> set[int]:
        """Every ticket referenced by an OPEN trade row (order_id + order_ids)."""
        tickets: set[int] = set()
        try:
            conn = sqlite3.connect(_DB_PATH)
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT order_id, order_ids FROM trades WHERE outcome = 'OPEN'"
                ).fetchall()
            finally:
                conn.close()
        except Exception as e:
            logger.error(f"TradeLogger open-tickets fetch error: {e}")
            return tickets

        for r in rows:
            if r["order_id"] is not None:
                tickets.add(int(r["order_id"]))
            if r["order_ids"]:
                try:
                    tickets.update(int(t) for t in json.loads(r["order_ids"]))
                except (ValueError, TypeError, json.JSONDecodeError):
                    pass
        return tickets

    def _fetch_all_known_tickets_sync(self) -> set[int]:
        """Every ticket EVER referenced by a trade row (order_id + order_ids),
        regardless of outcome. Used ONLY by reconciliation Class-A to decide
        whether a live position is one the bot opened+logged. A dual-TP leg
        whose sibling already closed (row flipped off 'OPEN') is still 'known'
        here, so its still-open runner is not mis-flagged as an untracked open.
        A genuine ghost (opened but never logged) appears in NO row → still
        caught. MT5 tickets are never reused, so a long-closed ticket cannot
        alias a new position. Class B keeps using the OPEN-only set."""
        tickets: set[int] = set()
        try:
            conn = sqlite3.connect(_DB_PATH)
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT order_id, order_ids FROM trades"
                ).fetchall()
            finally:
                conn.close()
        except Exception as e:
            logger.error(f"TradeLogger known-tickets fetch error: {e}")
            return tickets

        for r in rows:
            if r["order_id"] is not None:
                tickets.add(int(r["order_id"]))
            if r["order_ids"]:
                try:
                    tickets.update(int(t) for t in json.loads(r["order_ids"]))
                except (ValueError, TypeError, json.JSONDecodeError):
                    pass
        return tickets

    # ─────────────────────────────────────────────────────────────
    # Query Interface — used by rag_retriever
    # ─────────────────────────────────────────────────────────────

    async def get_similar_trades(
        self,
        symbol:       str,
        direction:    str,
        price_center: float,
        price_range:  float,
        limit:        int = 5,
    ) -> list[dict]:
        """
        Return closed trades that match symbol + direction + price zone.
        Price zone: entry_price within [price_center ± price_range].
        Only returns trades with known outcomes (outcome != 'OPEN').
        """
        return await asyncio.to_thread(
            self._query_similar_sync,
            symbol, direction, price_center, price_range, limit,
        )

    def _query_similar_sync(
        self,
        symbol:       str,
        direction:    str,
        price_center: float,
        price_range:  float,
        limit:        int,
    ) -> list[dict]:
        sql = (
            "SELECT timestamp, direction, entry_price, stop_loss,"
            "       take_profit_1, take_profit_2, confluence_score,"
            "       market_state, atr, rr_ratio,"
            "       outcome, outcome_price, outcome_pnl, agent_reasoning "
            "FROM   trades "
            "WHERE  symbol     = ? "
            "  AND  direction  = ? "
            "  AND  outcome   != 'OPEN' "
            "  AND  entry_price BETWEEN ? AND ? "
            "ORDER  BY timestamp DESC "
            "LIMIT  ?"
        )
        try:
            conn = sqlite3.connect(_DB_PATH)
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    sql,
                    (symbol, direction,
                     price_center - price_range,
                     price_center + price_range,
                     limit),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()
        except Exception as e:
            logger.error(f"TradeLogger query error: {e}")
            return []


# ── Singleton ─────────────────────────────────────────────────────
trade_logger = TradeLogger()
