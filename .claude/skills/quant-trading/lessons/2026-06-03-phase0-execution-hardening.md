# 2026-06-03 — Phase 0: live execution-path hardening

## Symptom / what we built
The live path is MARKET execution, flagged in PROJECT_STATUS as "reconstructed
from spec, never confirmed against a real broker fill." Before risking the
30-trade FTMO gate on it, we hardened four gaps:

- **0B** — `OrderExecutor._handle_market_decision` treated `order_send` retcode
  `10009` (DONE) as a confirmed fill and published `TRADE_EXECUTED` /
  `POSITION_OPENED` off `result.price`. A 10009 only means MT5 *accepted* the
  request; the deal may not have posted (and `positions_get()` lags right after a
  send). A phantom fill silently corrupted state.
- **0D** — `trades.db` did not persist the HMM `regime` or `break_even` status, so
  a hard-kill + reboot lost break-even state and regime context.
- **0C** — no continuous DB-vs-broker reconciliation; only a 30s close-poller and a
  one-shot boot orphan sweep.
- **0-PRE** — `Topics.SYSTEM_SHUTDOWN` existed but was unwired, so no agent could
  request a graceful shutdown.

## Root cause / design rationale
"The broker said DONE" is an *acceptance*, not a *confirmation*. The only
authoritative proof a market deal executed is a `DEAL_ENTRY_IN` row in deal
history (`history_deals_get(position=ticket)`) — the same source the close-path
already trusted for `DEAL_ENTRY_OUT`. Everything downstream (DB rows, BE tracking,
analytics) was being built on the weaker signal.

Shutdown is **halt-only** by deliberate choice: on an unconfirmed fill or a state
desync we stop trading and leave positions under their broker-side SL/TP. A
false-positive desync must never market-close a good trade. Flattening is reserved
for the margin/drawdown emergency path.

## Fix / final design
- `core.event_bus.request_shutdown(reason, source)` publishes `SYSTEM_SHUTDOWN`;
  `main.shutdown_listener` sets the shared `shutdown_event` (existing graceful
  unwind — no flatten). New `send_alert()` + `SYSTEM_ALERT` topic → Notifier red
  embed (reuses the webhook code; does **not** imply liquidation).
- `OrderExecutor._confirm_deal()` polls `history_deals_get` for `DEAL_ENTRY_IN`
  (timeout `CFG.execution.fill_confirm_timeout_sec`). Confirmed → publish with the
  *authoritative* deal price; timeout → `send_alert` + `request_shutdown`, never a
  fabricated `TRADE_EXECUTED`.
- `TradeLogger`: idempotent `ALTER TABLE` migration for `regime` + `break_even`;
  `_break_even_listener` flips `break_even` on `BREAK_EVEN_TRIGGERED` (matched by
  parsing the `order_ids` JSON — restart-safe); `_reconciliation_monitor` (5s)
  with per-ticket debounce, a Class-A position-age grace (≥ fill-confirm window,
  to ignore the legitimate in-flight window), and a Class-B `DEAL_ENTRY_OUT` guard
  (so a normal close isn't mistaken for a desync).
- `tools/chaos_harness.py` drives the real executor against the demo with a
  `mt5.order_send` wrapper (latency + synthetic 10030/10004), `os._exit` hard-kill,
  and a `--phase verify` that runs the real recovery paths and asserts Class-B == 0
  (a row is only written on a *confirmed* fill).

## Generalizable rule
A broker "request accepted" code (MT5 10009 / TRADE_RETCODE_DONE) is not proof of
execution — confirm the resulting deal in history before recording state, and on
failure to confirm, halt (alert) rather than fabricate the outcome. (Same spirit
as anti-pattern #5: an infra failure to *verify* is not a negative result.)
