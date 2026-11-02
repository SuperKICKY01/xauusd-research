"""
backtest/metrics.py — Trade & equity-curve statistics.

Pure functions over the VirtualBroker's `closed` list and `equity_curve`.
No I/O except the print_report helper.

Reported stats:
  • Total trades, win rate, expectancy per trade
  • Gross PnL, profit factor, average R:R achieved
  • Max drawdown (peak-to-trough on equity curve)
  • Longest losing streak
  • Outcome breakdown (WIN_TP1 / WIN_TP2 / LOSS / BE_HIT / TIMEOUT / TIME_STOP)
  • Score-bucket win rate (lets us see if confluence score predicts outcome)

Statistics levels (harness-v2, Phase 0.2):
  • PER-LEG  (legacy)    — every closed TP1/TP2 leg is one observation. The
    two legs of one dual-TP signal share entry/SL and are strongly correlated,
    so the per-leg t-stat overstates significance by up to ~sqrt(2). Kept for
    comparison with historical reports ONLY.
  • PER-SIGNAL (canonical) — legs grouped back into their originating signal
    (signal_id minted in VirtualBroker._spawn_legs); one net PnL / net R
    observation per signal. ALL pass/fail gates use per-signal stats.
"""

import json
import logging
import math
from collections import defaultdict
from dataclasses import asdict
from statistics import stdev
from typing import Optional

import pandas as pd

from backtest.virtual_broker import ClosedTrade, VirtualBroker

logger = logging.getLogger(__name__)

_TP_OUTCOMES = ("WIN_TP1", "WIN_TP2")
_LOSS_OUTCOMES = ("LOSS",)


def _t_stat(values: list[float]) -> Optional[float]:
    """t-statistic of mean vs H0: mean = 0. Sample stdev (ddof=1).
    None when n < 2 or the sample stdev is 0."""
    n = len(values)
    if n < 2:
        return None
    sd = stdev(values)          # statistics.stdev IS the ddof=1 estimator
    if sd <= 0:
        return None
    return (sum(values) / n) / (sd / math.sqrt(n))


def _group_by_signal(trades: list) -> dict:
    """
    Group closed legs into signals. Primary key: signal_id (> 0, minted by
    VirtualBroker._spawn_legs — one per signal, shared by its legs). Fallback
    for pre-harness-v2 data (signal_id absent/0): the tuple key
    (open_ts, direction, entry, sl) — legs of one signal share all four, and
    the duplicate-direction VETO guarantees two distinct same-direction
    signals can never be open simultaneously, so the tuple cannot collide.
    (Same convention as tools/ftmo_challenge_mc.py::_signals_from_report.)
    """
    groups: dict = defaultdict(list)
    for t in trades:
        sid = getattr(t, "signal_id", 0)
        key = sid if sid else (
            t.open_ts, t.direction,
            round(float(t.entry_price), 3), round(float(t.sl), 3),
        )
        groups[key].append(t)
    return groups


def _per_signal_metrics(broker, trades: list) -> dict:
    """
    Canonical per-SIGNAL statistics (Phase 0.2). One observation per signal:
      net PnL  = sum of its legs' pnl (already net of commission + swap)
      net R    = net PnL / risk$ where risk$ = Σ legs lot × (|entry − sl| +
                 slippage buffer) × contract_size — the same denominator the
                 lot-sizing formula targets, so mean net R is directly
                 comparable to the configured risk-per-trade.
    """
    groups = _group_by_signal(trades)

    contract  = float(getattr(broker, "contract_size", 100.0))
    point     = float(getattr(broker, "point", 0.01))
    slip_pips = float(getattr(broker, "slippage_buffer_pips", 0.0))
    slip_price = slip_pips * point * 10

    pnls:   list[float] = []
    net_rs: list[float] = []
    n_legs_covered = 0
    for legs in groups.values():
        pnl  = sum(float(t.pnl) for t in legs)
        risk = sum(
            float(t.lot) * (abs(float(t.entry_price) - float(t.sl)) + slip_price) * contract
            for t in legs
        )
        pnls.append(pnl)
        if risk > 0:
            net_rs.append(pnl / risk)
        n_legs_covered += len(legs)

    n_sig  = len(pnls)
    if n_sig == 0:
        return {"signals": 0}

    wins   = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p < 0)
    gross_win  = sum(p for p in pnls if p > 0)
    gross_loss = sum(p for p in pnls if p < 0)      # negative
    pf = abs(gross_win / gross_loss) if gross_loss != 0 else float("inf")

    t_pnl = _t_stat(pnls)
    t_r   = _t_stat(net_rs)

    return {
        "signals":            n_sig,
        "legs_covered":       n_legs_covered,
        "wins":               wins,
        "losses":             losses,
        "flat":               n_sig - wins - losses,
        "win_rate_pct":       round(100.0 * wins / n_sig, 2),
        "profit_factor":      round(pf, 3) if pf != float("inf") else None,
        "expectancy_usd":     round(sum(pnls) / n_sig, 2),
        "expectancy_r":       round(sum(net_rs) / len(net_rs), 4) if net_rs else None,
        "t_stat_pnl":         round(t_pnl, 3) if t_pnl is not None else None,
        "t_stat_r":           round(t_r, 3)   if t_r   is not None else None,
        "signals_with_risk":  len(net_rs),
    }


def compute_metrics(broker: VirtualBroker, initial_balance: float) -> dict:
    """
    Aggregate stats from a populated VirtualBroker.

    Top-level wins / losses / t_stat are PER-LEG (legacy — kept so old
    reports stay comparable). TIMEOUT and BE_HIT are not counted as either —
    they're tracked separately in the outcome breakdown.

    The "per_signal" sub-dict is the CANONICAL statistics level: legs grouped
    by signal_id, one net PnL / net R observation per signal. All gates use it.
    """
    trades = broker.closed
    n      = len(trades)

    if n == 0:
        return {
            "trades":          0,
            "final_balance":   broker.balance,
            "net_pnl":         0.0,
            "note":            "No trades closed.",
        }

    wins   = [t for t in trades if t.outcome in _TP_OUTCOMES]
    losses = [t for t in trades if t.outcome in _LOSS_OUTCOMES]
    be_exits = [t for t in trades if t.outcome == "BE_HIT"]
    timeouts = [t for t in trades if t.outcome == "TIMEOUT"]

    gross_win   = sum(t.pnl for t in wins)
    gross_loss  = sum(t.pnl for t in losses)            # negative
    net_pnl     = sum(t.pnl for t in trades)
    win_rate    = 100.0 * len(wins) / n
    avg_rr      = sum(t.rr_realised for t in trades) / n
    expectancy  = net_pnl / n

    profit_factor = (
        abs(gross_win / gross_loss) if gross_loss != 0 else float("inf")
    )

    # PER-LEG t-statistic on per-trade PnL (H0: mean = 0). Sample stdev
    # (ddof=1). None when n<2 or sd==0. LEGACY / comparison only — legs of one
    # signal are correlated, so this t is inflated up to ~sqrt(2). Gates use
    # the per-signal stats below.
    t_stat = _t_stat([t.pnl for t in trades])

    # ── Drawdown on equity curve ───────────────────────────────────
    max_dd_pct = 0.0
    peak = initial_balance
    for _, eq in broker.equity_curve:
        if eq > peak:
            peak = eq
        if peak > 0:
            dd = (peak - eq) / peak * 100.0
            if dd > max_dd_pct:
                max_dd_pct = dd

    # ── Streaks (chronological order) ──────────────────────────────
    chronological = sorted(trades, key=lambda t: t.close_ts)
    longest_loss_streak = 0
    longest_win_streak  = 0
    cur_loss = cur_win  = 0
    for t in chronological:
        if t.outcome in _LOSS_OUTCOMES:
            cur_loss += 1; cur_win = 0
        elif t.outcome in _TP_OUTCOMES:
            cur_win  += 1; cur_loss = 0
        else:
            cur_loss = cur_win = 0
        longest_loss_streak = max(longest_loss_streak, cur_loss)
        longest_win_streak  = max(longest_win_streak,  cur_win)

    # ── Score-bucket win rate ──────────────────────────────────────
    buckets: dict[str, dict] = {}
    for t in chronological:
        b = _bucket(t.score)
        s = buckets.setdefault(b, {"n": 0, "wins": 0, "pnl": 0.0})
        s["n"]   += 1
        s["pnl"] += t.pnl
        if t.outcome in _TP_OUTCOMES:
            s["wins"] += 1
    score_buckets = {
        b: {
            "trades":   s["n"],
            "win_rate": round(100.0 * s["wins"] / s["n"], 1) if s["n"] else 0.0,
            "pnl":      round(s["pnl"], 2),
        }
        for b, s in sorted(buckets.items())
    }

    return {
        "trades":              n,
        "stats_note":          "top-level = per-LEG (legacy, comparison only); "
                               "'per_signal' = canonical, all gates use it",
        "per_signal":          _per_signal_metrics(broker, trades),
        "wins":                len(wins),
        "losses":              len(losses),
        "be_exits":            len(be_exits),
        "timeouts":            len(timeouts),
        "win_rate_pct":        round(win_rate, 2),
        "net_pnl":             round(net_pnl, 2),
        "gross_win":           round(gross_win, 2),
        "gross_loss":          round(gross_loss, 2),
        "profit_factor":       round(profit_factor, 3) if profit_factor != float("inf") else None,
        "expectancy_per_trade":round(expectancy, 2),
        "t_stat":              round(t_stat, 3) if t_stat is not None else None,
        "avg_rr_realised":     round(avg_rr, 3),
        "max_drawdown_pct":    round(max_dd_pct, 3),
        "longest_win_streak":  longest_win_streak,
        "longest_loss_streak": longest_loss_streak,
        "initial_balance":     round(initial_balance, 2),
        "final_balance":       round(broker.balance, 2),
        "return_pct":          round(100.0 * (broker.balance - initial_balance) / initial_balance, 3)
                                if initial_balance else 0.0,
        # Transaction costs (0.0 when apply_costs=False or on a duck-typed
        # broker that doesn't track them, e.g. walk_forward's window slice).
        "commission_paid":     round(getattr(broker, "total_commission_paid", 0.0), 2),
        "swap_paid":           round(getattr(broker, "total_swap_paid", 0.0), 2),
        "total_costs":         round(getattr(broker, "total_commission_paid", 0.0)
                                     + getattr(broker, "total_swap_paid", 0.0), 2),
        "score_buckets":       score_buckets,
    }


def _bucket(score: int) -> str:
    if score < 55:  return "<55"
    if score < 65:  return "55-64"
    if score < 75:  return "65-74"
    if score < 85:  return "75-84"
    return "85-100"


def print_report(metrics: dict) -> None:
    """Pretty-print to stdout. Uses plain ASCII so it works in any terminal."""
    print()
    print("=" * 64)
    print("  BACKTEST RESULTS")
    print("=" * 64)

    if metrics.get("trades", 0) == 0:
        print(f"  {metrics.get('note', 'No trades.')}")
        print("=" * 64)
        return

    print(f"  Trades (legs)     : {metrics['trades']}   [per-leg = legacy, comparison only]")
    print(f"  Wins / Losses     : {metrics['wins']} / {metrics['losses']}")
    print(f"  BE exits          : {metrics['be_exits']}")
    print(f"  Timeouts          : {metrics['timeouts']}")
    print(f"  Win rate          : {metrics['win_rate_pct']}%")
    pf = metrics.get("profit_factor")
    pf_str = f"{pf:.3f}" if pf is not None else "inf"
    print(f"  Profit factor     : {pf_str}")
    print(f"  Expectancy / trade: ${metrics['expectancy_per_trade']:+,.2f}")
    t = metrics.get("t_stat")
    t_str = f"{t:+.3f}" if t is not None else "n/a"
    print(f"  t-stat (mean=0)   : {t_str}")
    ps = metrics.get("per_signal") or {}
    if ps.get("signals"):
        def _fmt(v, spec="+.3f"):
            return format(v, spec) if v is not None else "n/a"
        print("-" * 64)
        print(f"  PER-SIGNAL (canonical — all gates use these)")
        print(f"  Signals           : {ps['signals']}  "
              f"(legs covered: {ps['legs_covered']})")
        print(f"  Win / Loss / Flat : {ps['wins']} / {ps['losses']} / {ps['flat']}")
        print(f"  Win rate          : {ps['win_rate_pct']}%")
        print(f"  Profit factor     : {_fmt(ps.get('profit_factor'), '.3f')}")
        print(f"  Expectancy / sig  : ${ps['expectancy_usd']:+,.2f}")
        print(f"  Expectancy (R)    : {_fmt(ps.get('expectancy_r'), '+.4f')}")
        print(f"  t-stat PnL / R    : {_fmt(ps.get('t_stat_pnl'))} / {_fmt(ps.get('t_stat_r'))}")
    print(f"  Avg R:R realised  : {metrics['avg_rr_realised']:+.3f}")
    print(f"  Max drawdown      : {metrics['max_drawdown_pct']}%")
    print(f"  Longest win streak: {metrics['longest_win_streak']}")
    print(f"  Longest loss streak: {metrics['longest_loss_streak']}")
    print("-" * 64)
    print(f"  Initial balance   : ${metrics['initial_balance']:,.2f}")
    print(f"  Final balance     : ${metrics['final_balance']:,.2f}")
    print(f"  Net PnL           : ${metrics['net_pnl']:+,.2f}")
    print(f"  Return            : {metrics['return_pct']:+.2f}%")
    tc = metrics.get("total_costs", 0.0)
    if tc:
        print("-" * 64)
        print(f"  Commission paid   : ${metrics.get('commission_paid', 0.0):,.2f}")
        print(f"  Swap paid         : ${metrics.get('swap_paid', 0.0):,.2f}")
        print(f"  Total costs       : ${tc:,.2f}")
    print("-" * 64)
    print(f"  Score-bucket performance:")
    print(f"    {'Bucket':<10}{'Trades':>10}{'Win%':>10}{'PnL':>14}")
    for b, s in metrics["score_buckets"].items():
        print(f"    {b:<10}{s['trades']:>10}{s['win_rate']:>9.1f}%"
              f"{s['pnl']:>14,.2f}")
    print("=" * 64)
    print()


def write_json_report(
    metrics:        dict,
    broker:         VirtualBroker,
    out_path:       str,
    include_trades: bool = True,
) -> None:
    """Persist a full report to disk. Trades + equity curve included."""
    payload = {
        "summary": metrics,
        "equity_curve": [
            {"timestamp": ts.isoformat(), "equity": round(eq, 2)}
            for ts, eq in broker.equity_curve
        ],
    }
    if include_trades:
        payload["trades"] = [
            _trade_to_dict(t) for t in broker.closed
        ]
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)
    logger.info(f"metrics: JSON report written → {out_path}")


def _trade_to_dict(t: ClosedTrade) -> dict:
    d = asdict(t)
    # Convert pandas Timestamps → ISO strings so json.dump doesn't blow up.
    for k, v in d.items():
        if isinstance(v, pd.Timestamp):
            d[k] = v.isoformat()
    return d
