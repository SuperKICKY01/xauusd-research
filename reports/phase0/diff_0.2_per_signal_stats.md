# Phase 0.2 — Per-signal statistics: exact change log

No git baseline exists (repo has zero commits — flagged in the G0 report), so
this file records every 0.2 edit verbatim. The 0.1 lookahead fix has a true
machine diff (`diff_0.1_lookahead_fix.patch`) because the pre-0.1 tree was
snapshotted for the calibration run; 0.2 predates that snapshot, hence the
edit log form. REVIEWER: the snapshot tree at
`<scratchpad>/calib_v1_tree/backtest/` CONTAINS the 0.2 changes (they are
selection-neutral accounting), so `git diff --no-index` of snapshot vs main
shows 0.1 only — by design (calibration isolates the lookahead fix).

## backtest/virtual_broker.py

1. `Position` dataclass — added field:
```python
    # One id per SIGNAL: the TP1+TP2 legs spawned from a single SignalPacket
    # share it. 0 = unassigned (never emitted by _spawn_legs; kept as the
    # sentinel so metrics can fall back to tuple-key grouping on old data).
    signal_id:    int = 0
```

2. `ClosedTrade` dataclass — added field:
```python
    # Signal identity: both legs of one dual-TP signal carry the same id, so
    # metrics can aggregate per SIGNAL (the statistically independent unit —
    # per-leg t-stats double-count correlated legs, inflating t up to ~sqrt(2)).
    signal_id:         int  = 0
```

3. `__init__` — added counter next to `_next_ticket`:
```python
        self._next_signal_id: int             = 1
```

4. `_spawn_legs` — mints one id per call (= one signal; both market path
   `open_dual_tp` and limit path `_process_pending` construct legs ONLY here):
```python
        signal_id = self._next_signal_id
        self._next_signal_id += 1
```
   and both `Position(...)` constructions gained `signal_id = signal_id`.

5. `_close_position` — `ClosedTrade(...)` gained `signal_id = pos.signal_id`.

## backtest/metrics.py

1. New helpers (full text in file): `_t_stat(values)` (mean / (stdev(ddof=1)/sqrt(n)),
   None when n<2 or sd==0), `_group_by_signal(trades)` (key = signal_id when >0,
   else the `(open_ts, direction, round(entry,3), round(sl,3))` tuple — same
   convention as tools/ftmo_challenge_mc.py::_signals_from_report),
   `_per_signal_metrics(broker, trades)`.
2. Per-signal net R denominator: `risk$ = Σ legs lot × (|entry − sl_initial| +
   slippage_buffer_price) × contract_size` — identical to the lot-sizing
   formula's risk target and to the FTMO-MC convention. `ClosedTrade.sl` IS
   sl_initial (virtual_broker.py `_close_position`).
3. `compute_metrics` return gained:
   `"stats_note"`, `"per_signal": _per_signal_metrics(...)`. The legacy
   per-leg computation replaced by the shared `_t_stat` helper —
   IDENTICAL math (verified by unit test `test_legacy_per_leg_unchanged`:
   hand-computed t=0.736 on the 5-leg fixture).
4. `print_report` — added PER-SIGNAL section labelled
   "(canonical — all gates use these)"; per-leg block labelled
   "[per-leg = legacy, comparison only]".

## backtest/walk_forward.py

1. `_filter_broker_to_window` — the window's duck-typed broker now carries
   `contract_size`, `point`, `slippage_buffer_pips` copied from the real
   broker (per-signal R denominator must not fall back to silent defaults).
2. New `_gate_t(metrics)` — PASS gate reads `per_signal.t_stat_pnl`;
   falls back to legacy per-leg t ONLY when per-signal stats are absent,
   with a loud warning. Used in `run_wfv`, `_parallel_run`, `print_report`.
3. Log lines + report table header now show `t/sig` (and per-leg t in
   parentheses for comparison).

## Unit test

`tests/test_per_signal_metrics.py` — 3/3 PASS (fixture: 3 signals / 5 legs;
hand-computed expectations: per-signal t=0.480, PF=2.5, E[R]=+0.05;
fallback tuple-key grouping identical incl. the adversarial case of two
opposite-direction signals sharing open_ts; legacy per-leg t=0.736 unchanged).
