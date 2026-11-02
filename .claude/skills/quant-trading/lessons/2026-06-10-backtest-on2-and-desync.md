# 2026-06-10 — Backtest O(n²) blow-up & dual-TP reconciliation false-desync

Two separate incidents in one session. Both involve "the obvious culprit was
the wrong one," and one ended with the live bot self-halting on its normal win
path. Standard 4-section style, applied per incident.

---

## Incident 1 — Backtest O(n²) blow-up (full-window run never finishes)

### Symptom
A full-window backtest (100k M5 bars, Dec-2024→May-2026) burned ~9,877
CPU-seconds (multi-core) and produced no output; it had to be killed. Smaller
windows finished, so the cost was clearly super-linear in bar count.

### Root cause
- **FALSE lead:** `backtest/strategy_runner.py` calls `slice_window()` 3×/bar,
  and `slice_window` does `full_df.loc[:end_ts]` (`data_loader.py:186`). This
  *looked* quadratic ("`.loc` in a per-bar loop"). It is **not**: the frames
  come from `resample().agg().dropna()`, giving a **monotonic, unique
  DatetimeIndex**, so `.loc[:end_ts]` uses `searchsorted` and returns a *view* →
  **O(log n)** per call. Replacing it with a pointer would have saved nothing.
- **REAL quadratic:** `VirtualBroker.step()` ran the break-even pass as
  `for t in self.closed if t.close_ts == ts …` — scanning the **entire, growing
  closed-trade ledger every bar** → O(bars × total_trades). This matched the
  evidence exactly (cost trivial early, brutal late as the ledger grows).
- **Linear giants still dominating (DEFERRED):** even a 1-month window takes
  ~8–9 min (~88 ms/bar) — *not* from the O(n²) (only 2 trades there) but from
  **(Item B)** the per-bar SMC recompute (`find_structure_events` re-scans a
  200-bar window × 3 TFs every bar with scalar `.iloc[i]` in a Python loop),
  and **(Item C)** `gc.collect()` every bar in `signal_builder._process_candles`
  (super-linear as the heap grows, and on the **LIVE** path — needs its own
  analysis before touching). These are the real runtime hogs at the scales we
  tested; both are deferred.

### Fix
- **Fix A (applied):** record `_closed_mark = len(self.closed)` at the top of
  `step()` and scan only `self.closed[_closed_mark:]` in the BE pass. Tail items
  all have `close_ts == ts` (ts strictly increases per step), so the result set
  is unchanged.
- **Proven byte-identical** two ways: (1) analytical — the dropped rows
  (`self.closed[:_closed_mark]`) all have `close_ts < ts`, so none could satisfy
  the `close_ts == ts` filter; (2) empirical A/B on a Jan-2025 window, run
  **strictly sequentially** (pre-fix then post-fix): identical 2 trades, PF
  0.000, final $99,550.54, both losses, and every closed-trade row identical.
  Wall-clock 527 s → 486 s (run-to-run noise; Fix A is unmeasurable at 2 trades —
  its benefit only appears with thousands of trades).

### Process-hygiene failure (the real damage)
Attempting to reconstruct the full population, I fanned out **10 parallel
backtests without killing prior runs** — each loads the full 100k-row CSV → a
`MemoryError` cascade that exhausted CPU/RAM and forced a **manual machine
reboot**. Earlier 5-wide and the original O(n²) full run had already been
chewing resources. Cost the user real time and trust.
**Rule:** backtests run **strictly sequentially, one job**; never fan out;
kill prior runs before starting another; never load the full CSV in parallel.

---

## Incident 2 — Dual-TP reconciliation false-desync (live self-DoS)

### Symptom
Live bot opened a dual-TP SHORT (DB #26; TP1 leg #770631134, TP2 leg
#770631140) at 02:15. 05:10:53 break-even activated on the TP2 runner. 05:11:05
the TP1 leg closed `WIN_TP1` (+$219.06). 05:11:13 reconciliation reported
"STATE DESYNC — #770631140: live position not in trades.db (untracked open)"
and issued a **halt-only** shutdown. A later 06:56 restart **re-halted on the
same desync**. The DB had clearly known #770631140 at 05:10:53, yet declared it
untracked 20 s later.

### Root cause
A dual-TP trade is **one** trades.db row, both leg tickets in `order_ids`,
`order_id` = TP1 leg, and a **single** `outcome` column. Reconciliation's
"tracked" set is `_fetch_open_tickets_sync()` → `SELECT order_id, order_ids
FROM trades WHERE outcome = 'OPEN'`. When the TP1 leg closed, the position
monitor's `_resolve_closed_position` → `_update_outcome_sync(db_id=26,
'WIN_TP1', …)` overwrote the **shared row's** `outcome` to `WIN_TP1`, dropping
the whole row out of the `OPEN` set. The still-open TP2 runner #770631140 (live
in MT5, ~3 h old → past the Class-A age grace) was then in **no** tracked row →
Class-A "untracked open" → debounce → **halt**. This is the **normal dual-TP
win path** (TP1 banks partial, runner continues), so it is a **recurring
self-DoS**: the bot halts on essentially every dual-TP trade that books TP1
while the runner is still open. (Halt-only is the safe failure mode — positions
were left under broker SL/TP, not force-closed.)

### Fix
- **Surgical fix (APPLIED, UNTESTED live):** added `_fetch_all_known_tickets_sync()`
  (same union of `order_id` + `order_ids`, but over **all** rows, no
  `outcome='OPEN'` filter) and used it **only** at the Class-A membership check.
  A still-open dual-TP leg is now "known" (it's in `order_ids`), so it isn't
  mis-flagged; a genuine ghost (opened but never logged) is in no row → still
  caught; MT5 tickets are never reused → no aliasing. Class B and the
  outcome-recording/RAG paths are untouched. `py_compile` clean.
  **This was applied AFTER the 05:11 and 06:56 halts — neither halt tested it.
  Status: applied but not yet verified live.**
- **Proper follow-up (root fix, HIGH-priority, NOT done):** keep
  `outcome='OPEN'` until **all** `order_ids` legs have closed, accumulating
  per-leg pnl. This fixes both the false-desync *and* incomplete-pnl logging:
  DB #26 recorded only `WIN_TP1 +$219.06` and is **missing the TP2 runner's BE
  close (−$0.66)** — so every dual-TP win currently logs incomplete pnl. The
  surgical fix stops the self-DoS but does not fix the pnl gap; the semantic fix
  is required, not optional.

---

## Status (end of session)
- **Bot is OFFLINE.**
- **Restart is effectively BLOCKED until the surgical fix is confirmed.** The
  fix is applied but UNTESTED live; if it doesn't hold, the bot self-halts on
  the **next dual-TP TP1 win** (the normal win path). So a restart is **not**
  "resume normal operation" — it is a **deliberate test**: restart specifically
  TO TEST the fix and **watch the first dual-TP TP1 win closely**. If it halts
  again → implement the semantic (`outcome='OPEN'`-until-all-legs-close,
  accumulate per-leg pnl) fix before relying on the bot for normal operation.
- The dual-TP runner #770631140 has since hit its BE/SL stop (closing deal
  `[sl 4087.28]`, −$0.66); **0 open positions** on the account now — nothing to
  manage manually.
- Backtest perf Items **B** (per-bar SMC recompute) and **C** (per-bar
  `gc.collect()` on the live path) remain deferred.

---

## Generalizable rules
1. **Verify algorithmic complexity from the actual data structure before
   optimizing.** `.loc[:ts]` on a monotonic index is O(log n), not O(n); the
   real quadratic was a `for x in <growing_list>` scan per event. Any
   growing-list scan inside a per-event loop is quadratic — scan only the new
   tail.
2. **Run backtests strictly sequentially.** Parallel fan-out where each job
   loads the full dataset will OOM the machine; kill prior runs first.
3. **One trades.db row per multi-leg trade conflates leg state with trade
   state.** A shared terminal `outcome` lets the first leg's close orphan the
   still-open sibling — both for reconciliation *and* for pnl. Track per-leg
   open/closed, or (interim) reconcile against all-known tickets, not just OPEN.
4. **"Applied" ≠ "verified live."** Record fixes as UNTESTED until exercised;
   a failure that predates a fix is not evidence the fix failed.
