"""
quant/zone_engine.py — Supply/Demand zone construction + lifecycle (H1).

Implements EXACTLY the frozen Phase-1 spec (specs/phase1_zone_spec_v1.md
§1.1.2–§1.1.3). Pure, synchronous, incremental: feed CLOSED H1 bars one at a
time via on_h1_close(); read `active_zones` between feeds. No pandas inside
the hot path, no RNG — same input bars ⇒ byte-identical zone history.

Spec mapping (v1.0 frozen values are the constructor defaults):
  • Swing: 2-bar strict fractal — detectable once bar i+2 closes.
  • Impulse: some close within bars i+1..i+3 at ≥ 1.5×ATR[i] from the extreme.
  • ATR(H1,14), Wilder RMA: TR[0]=H−L; TR[i]=max(H−L,|H−pc|,|L−pc|);
    ATR[13]=mean(TR[0..13]); ATR[i]=(ATR[i−1]·13+TR[i])/14. ATR[i] is the
    value AT swing bar i, frozen onto the zone.
  • Activation bar a = max(k_first_confirm, i+2). The spec's §1.1.3 text puts
    activation at the close of the first confirming bar k and asserts k ≥ i+2
    is implicit; k = i+1 is in fact possible, but the fractal itself is not
    knowable until i+2 closes — so causality forces max(k, i+2). This is a
    spec-ambiguity resolution (documented in the Phase-2 report), not a
    tunable parameter.
  • Geometry: supply distal=high[i], proximal=max(open[i],close[i]);
    demand distal=low[i], proximal=min(open[i],close[i]).
  • Guards at activation: 0.05×ATR[i] ≤ height ≤ 2.0×ATR[i].
  • SL frozen at activation: distal ± sl_atr_mult×ATR[i] (never re-anchored).
  • Lifecycle: expiry when age ≥ 72 H1 bars from activation; invalidation on
    any H1 CLOSE beyond distal; first touch consumes (caller calls consume());
    newer overlapping same-side zone replaces the older (closed-interval
    price overlap).
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class Zone:
    side:            str      # "SUPPLY" | "DEMAND"
    distal:          float
    proximal:        float
    atr:             float    # ATR[i] at the swing bar (frozen)
    sl:              float    # structural stop, frozen at activation
    swing_idx:       int      # H1 bar index of the swing bar i
    swing_ts:        object   # open ts of the swing bar
    activation_idx:  int      # H1 bar index a = max(k, i+2)
    activation_ts:   object   # open ts of the activation bar
    consumed:        bool = False
    dead:            bool = False
    death_reason:    str  = ""    # "invalidated" | "expired" | "replaced"

    @property
    def lo(self) -> float:
        return min(self.distal, self.proximal)

    @property
    def hi(self) -> float:
        return max(self.distal, self.proximal)


@dataclass
class _Candidate:
    """Swing awaiting impulse confirmation (bars i+1..i+3)."""
    side:      str
    swing_idx: int
    extreme:   float   # high[i] (supply) / low[i] (demand)
    body_edge: float   # max(o,c) (supply) / min(o,c) (demand)
    atr:       float   # ATR[i]


class ZoneEngine:
    """Incremental zone tracker. Not thread-safe; feed bars sequentially."""

    def __init__(
        self,
        atr_period:           int   = 14,
        impulse_atr_mult:     float = 1.5,
        impulse_confirm_bars: int   = 3,
        min_height_atr:       float = 0.05,
        max_height_atr:       float = 2.0,
        max_age_bars:         int   = 72,
        sl_atr_mult:          float = 0.2,
    ) -> None:
        self.atr_period           = atr_period
        self.impulse_atr_mult     = impulse_atr_mult
        self.impulse_confirm_bars = impulse_confirm_bars
        self.min_height_atr       = min_height_atr
        self.max_height_atr       = max_height_atr
        self.max_age_bars         = max_age_bars
        self.sl_atr_mult          = sl_atr_mult

        # H1 bar history (parallel arrays; index = H1 bar count 0..n-1).
        self._ts:    list = []
        self._open:  list[float] = []
        self._high:  list[float] = []
        self._low:   list[float] = []
        self._close: list[float] = []
        self._atr:   list[Optional[float]] = []
        self._tr_sum_seed: float = 0.0     # running sum of TR[0..atr_period-1]

        self._candidates: list[_Candidate] = []
        self._zones:      list[Zone] = []   # all ever activated (audit trail)

        self.counters: dict[str, int] = {
            "h1_bars":             0,
            "swings_detected":     0,
            "candidates_expired":  0,   # no impulse within confirm window
            "rejected_min_height": 0,
            "rejected_max_height": 0,
            "zones_activated":     0,
            "zones_replaced":      0,
            "zones_invalidated":   0,
            "zones_expired":       0,
            "zones_consumed":      0,
        }

    # ─────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────

    @property
    def active_zones(self) -> list[Zone]:
        """Tradeable zones: activated, not consumed, not dead. Deterministic
        order: activation index, then side, then distal."""
        zs = [z for z in self._zones if not z.consumed and not z.dead]
        zs.sort(key=lambda z: (z.activation_idx, z.side, z.distal))
        return zs

    @property
    def all_zones(self) -> list[Zone]:
        """Every zone ever activated (audit trail, includes dead/consumed)."""
        return list(self._zones)

    def consume(self, zone: Zone) -> None:
        """First M5 touch consumes the zone permanently (spec §1.1.3),
        regardless of whether the resulting trade filled or was skipped."""
        if not zone.consumed:
            zone.consumed = True
            self.counters["zones_consumed"] += 1

    def on_h1_close(self, ts, o: float, h: float, l: float, c: float) -> None:
        """Feed one CLOSED H1 bar (open-time label `ts`). The caller is
        responsible for the harness-v2 rule: only call this once the bar's
        close time is ≤ the decision time."""
        n = len(self._close)          # index of the bar being appended
        self._ts.append(ts)
        self._open.append(o)
        self._high.append(h)
        self._low.append(l)
        self._close.append(c)
        self._update_atr(n)
        self.counters["h1_bars"] += 1

        self._detect_swing(n)
        self._process_candidates(n)
        self._invalidate(n)
        self._expire(n)

    # ─────────────────────────────────────────────────────────────────
    # Internals
    # ─────────────────────────────────────────────────────────────────

    def _update_atr(self, n: int) -> None:
        if n == 0:
            tr = self._high[0] - self._low[0]
        else:
            pc = self._close[n - 1]
            tr = max(self._high[n] - self._low[n],
                     abs(self._high[n] - pc),
                     abs(self._low[n] - pc))
        p = self.atr_period
        if n < p:
            self._tr_sum_seed += tr
            self._atr.append(self._tr_sum_seed / p if n == p - 1 else None)
        else:
            prev = self._atr[n - 1]
            self._atr.append((prev * (p - 1) + tr) / p)   # type: ignore[operator]

    def _detect_swing(self, n: int) -> None:
        """2-bar strict fractal at i = n−2 (needs i−2..i+2 = n−4..n)."""
        i = n - 2
        if i < 2:
            return
        atr_i = self._atr[i]
        if atr_i is None or atr_i <= 0:
            return   # ATR warmup — no zones until ATR[i] exists (spec §1.1.2)

        hs, ls = self._high, self._low
        if (hs[i] > hs[i - 1] and hs[i] > hs[i - 2] and
                hs[i] > hs[i + 1] and hs[i] > hs[i + 2]):
            self.counters["swings_detected"] += 1
            self._candidates.append(_Candidate(
                side="SUPPLY", swing_idx=i, extreme=hs[i],
                body_edge=max(self._open[i], self._close[i]), atr=atr_i,
            ))
        if (ls[i] < ls[i - 1] and ls[i] < ls[i - 2] and
                ls[i] < ls[i + 1] and ls[i] < ls[i + 2]):
            self.counters["swings_detected"] += 1
            self._candidates.append(_Candidate(
                side="DEMAND", swing_idx=i, extreme=ls[i],
                body_edge=min(self._open[i], self._close[i]), atr=atr_i,
            ))

    def _process_candidates(self, n: int) -> None:
        """Confirm/expire pending swings. A candidate created at n = i+2 has
        its impulse window i+1..i+3 fully known by n = i+3, so candidates
        never outlive one extra bar."""
        keep: list[_Candidate] = []
        for cand in self._candidates:
            i = cand.swing_idx
            last_k = i + self.impulse_confirm_bars
            k_confirm: Optional[int] = None
            for k in range(i + 1, min(last_k, n) + 1):
                dist = (cand.extreme - self._close[k]) if cand.side == "SUPPLY" \
                       else (self._close[k] - cand.extreme)
                if dist >= self.impulse_atr_mult * cand.atr:
                    k_confirm = k
                    break
            if k_confirm is not None:
                a = max(k_confirm, i + 2)   # causal activation bar (see module doc)
                if a == n:
                    self._activate(cand, a)
                elif a < n:
                    # Unreachable by construction (candidates are created at
                    # n = i+2 and processed every bar); guard for safety.
                    self._activate(cand, n)
                else:
                    keep.append(cand)       # confirmed early; activate at a
                continue
            if n >= last_k:
                self.counters["candidates_expired"] += 1   # window exhausted
                continue
            keep.append(cand)
        self._candidates = keep

    def _activate(self, cand: _Candidate, a: int) -> None:
        height = abs(cand.extreme - cand.body_edge)
        if height < self.min_height_atr * cand.atr:
            self.counters["rejected_min_height"] += 1
            return
        if height > self.max_height_atr * cand.atr:
            self.counters["rejected_max_height"] += 1
            return

        if cand.side == "SUPPLY":
            distal, proximal = cand.extreme, cand.body_edge
            sl = distal + self.sl_atr_mult * cand.atr
        else:
            distal, proximal = cand.extreme, cand.body_edge
            sl = distal - self.sl_atr_mult * cand.atr

        zone = Zone(
            side=cand.side, distal=distal, proximal=proximal,
            atr=cand.atr, sl=sl,
            swing_idx=cand.swing_idx, swing_ts=self._ts[cand.swing_idx],
            activation_idx=a, activation_ts=self._ts[a],
        )

        # Overlap rule: newer replaces older ACTIVE same-side zone on
        # closed-interval price overlap (spec §1.1.3).
        for old in self._zones:
            if old.dead or old.consumed or old.side != zone.side:
                continue
            if zone.lo <= old.hi and old.lo <= zone.hi:
                old.dead = True
                old.death_reason = "replaced"
                self.counters["zones_replaced"] += 1

        self._zones.append(zone)
        self.counters["zones_activated"] += 1

    def _invalidate(self, n: int) -> None:
        c = self._close[n]
        for z in self._zones:
            if z.dead or z.consumed:
                continue
            beyond = (c > z.distal) if z.side == "SUPPLY" else (c < z.distal)
            if beyond:
                z.dead = True
                z.death_reason = "invalidated"
                self.counters["zones_invalidated"] += 1

    def _expire(self, n: int) -> None:
        for z in self._zones:
            if z.dead or z.consumed:
                continue
            if n - z.activation_idx >= self.max_age_bars:
                z.dead = True
                z.death_reason = "expired"
                self.counters["zones_expired"] += 1
