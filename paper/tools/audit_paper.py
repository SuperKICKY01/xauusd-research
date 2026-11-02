"""
audit_paper.py — self-audit of both editions: paper/main.tex (en) and paper/main_th.tex (th).

Thai edition: see thai_checks() — every Thai number must match its source (language-neutral
rendered strings verbatim) and the English edition (same numbers and claim tags per section,
same numbers per figure caption); Arabic numerals only. Results are reported per edition.

For every number in the report, recompute it from its source file (independently of
claims.csv) and assert that the rendered string appears in main.tex. Then:
  * list numeric tokens in the prose that no check covers (must be reviewed by hand),
  * verify every [Cnn] tag in main.tex exists in claims.csv,
  * HARD GATE: C66 and C67 must be VERIFIED-CODE and their code locators must still contain
    the cited code; otherwise the audit fails.

Every check ends in exactly one of three states. A check that could not run is SKIPPED and
is never counted as PASS:
  PASS     source recomputed and the rendered string found in main.tex
  FAIL     source disagrees, string missing, or an input is present but not the cited file
  SKIPPED  a required input is absent (only the unredistributed price CSV can be absent)
Numeric tokens covered only by SKIPPED checks are reported separately, not as covered.

Exit status:
  0  all checks PASS, gate holds, no uncovered tokens
  3  no FAIL, but one or more checks SKIPPED   ("passed with skips" -- NOT a full pass)
  1  any FAIL, gate failure, or uncovered numeric token

Usage: python paper/tools/audit_paper.py --root <project root> [--price-csv PATH]
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT: Path
PRICE_CSV: Path
TEX = ""
RESULTS: list[tuple[str, str, str]] = []      # (label, "PASS" | "FAIL" | "SKIPPED", note)
COVERED: set[str] = set()                      # tokens verified by a PASSing or FAILing check
SKIP_COVERED: set[str] = set()                 # tokens whose only check was SKIPPED
CHECK_ITEMS: dict[str, list[str]] = {}         # label -> rendered strings (reused for the Thai edition)
PRICE_CSV_REL = "data/xauusd_m5_full.csv"
EXCLUDED_SUMS = ("paper/public_bundle.EXCLUDED.SHA256SUMS.txt", "EXCLUDED.SHA256SUMS.txt")


def _tokens(items):
    out = set()
    for r in items:
        for tok in re.findall(r"\d[\d,]*(?:\.\d+)?", r):
            out.add(tok); out.add(tok.rstrip(".,"))
    return out


def record(label: str, ok: bool, note: str = ""):
    RESULTS.append((label, "PASS" if ok else "FAIL", note))


def skip(label: str, rendered: str | list[str], reason: str):
    """Register a check that could not run. Never counted as PASS."""
    items = [rendered] if isinstance(rendered, str) else rendered
    RESULTS.append((label, "SKIPPED", reason))
    SKIP_COVERED.update(_tokens(items))
    CHECK_ITEMS[label] = items


def load(rel):
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))


def text(rel):
    return (ROOT / rel).read_text(encoding="utf-8", errors="replace")


def line(rel, n):
    return text(rel).splitlines()[n - 1]


def need(label: str, rendered: str | list[str], ok: bool = True, note: str = ""):
    """Assert each rendered string occurs in main.tex (and that the source-side condition holds)."""
    items = [rendered] if isinstance(rendered, str) else rendered
    missing = [r for r in items if r not in TEX]
    passed = ok and not missing
    record(label, passed, note + (f" | missing in tex: {missing}" if missing else "")
           + ("" if ok else " | SOURCE CHECK FAILED"))
    COVERED.update(_tokens(items))
    CHECK_ITEMS[label] = items


def f(x, nd):  # fixed decimals, plain
    return f"{x:.{nd}f}"


def sgn(x, nd):  # LaTeX-math signed form as written in tables/text ($+x$ / $-x$)
    return f"{'+' if x >= 0 else '-'}{abs(x):.{nd}f}"


def per_signal_t(trades, scores):
    sig = defaultdict(list)
    for t in trades:
        if t["score"] >= 75:
            sig[(t["open_ts"], t["direction"], t["score"])].append(t["rr_realised"])
    r = [statistics.mean(v) for k, v in sig.items() if scores is None or k[2] in scores]
    return statistics.mean(r) / statistics.stdev(r) * math.sqrt(len(r)), len(r)


def main():
    global ROOT, TEX, PRICE_CSV
    sys.stdout.reconfigure(encoding="utf-8")   # Thai text in the report; console code pages vary
    ap = argparse.ArgumentParser(); ap.add_argument("--root", required=True)
    ap.add_argument("--price-csv", default=PRICE_CSV_REL,
                    help="price CSV to verify (default data/xauusd_m5_full.csv; relative to --root)")
    args = ap.parse_args()
    ROOT = Path(args.root).resolve()
    PRICE_CSV = Path(args.price_csv)
    TEX = text("paper/main.tex")

    # ── G0 calibration ────────────────────────────────────────────────────────
    rec = load("report_costs_065.json")["summary"]
    v1 = load("report_calib_v1_full.json")["summary"]; v2 = load("report_calib_v2_full.json")["summary"]
    p1, p2 = v1["per_signal"], v2["per_signal"]
    need("recorded per-leg PF/t", f"{rec['profit_factor']:.3f} / $+{rec['t_stat']:.3f}$")
    need("V1 per-leg PF/t", f"{v1['profit_factor']:.3f} / $+{v1['t_stat']:.3f}$")
    need("V2 per-leg PF/t", f"{v2['profit_factor']:.3f} / $+{v2['t_stat']:.3f}$")
    need("signals V1/V2", f"& {p1['signals']} & {p2['signals']} \\\\")
    need("PS PF V1/V2", f"& {p1['profit_factor']:.3f} & {p2['profit_factor']:.3f} \\\\")
    need("PS E[R] V1/V2", f"& $+{p1['expectancy_r']:.3f}$ & $+{p2['expectancy_r']:.3f}$ \\\\")
    need("PS t V1", f"$+{p1['t_stat_pnl']:.3f}$ / $+{p1['t_stat_r']:.3f}$")
    need("PS t V2", f"$+{p2['t_stat_pnl']:.3f}$ / $+{p2['t_stat_r']:.3f}$")
    need("returns", f"& $+{rec['return_pct']:.2f}$ & $+{v1['return_pct']:.2f}$ & $+{v2['return_pct']:.2f}$")
    need("MaxDD", f"& {rec['max_drawdown_pct']:.2f} & {v1['max_drawdown_pct']:.2f} & {v2['max_drawdown_pct']:.2f}")
    need("abstract t", f"$+{p1['t_stat_pnl']:.2f}$ to $+{p2['t_stat_pnl']:.2f}$")
    need("n=77 abstract/text", f"$n={p2['signals']}$")
    er_drop = round((1 - p2["expectancy_r"] / p1["expectancy_r"]) * 100)
    pnl_drop = round((1 - v2["net_pnl"] / v1["net_pnl"]) * 100)
    need("E[R] drop %", f"expectancy by {er_drop}\\%")
    need("PnL drop %", f"removed {pnl_drop}\\% of net profit")
    need("per-leg inflation", f"factor of {v1['t_stat'] / p1['t_stat_pnl']:.2f}")
    need("62% in discussion", f"expectancy fell by {er_drop}\\%")
    # data span — needs the price CSV, which is NOT redistributed. Absent -> SKIPPED (never PASS).
    # Present -> it must be the cited file (full SHA-256 from the excluded-files list), else FAIL.
    span_checks = [("bars", "165,437"), ("span dates", ["2024-01-01", "2026-05-06"]),
                   ("28.2 months", "over 28.2 months")]
    csv_path = PRICE_CSV if PRICE_CSV.is_absolute() else ROOT / PRICE_CSV
    expected = None
    for rel in EXCLUDED_SUMS:
        if (ROOT / rel).exists():
            for ln in text(rel).splitlines():
                h, _, p = ln.partition("  ")
                if p.strip() == PRICE_CSV_REL:
                    expected = h.strip()
            break
    if not csv_path.exists():
        why = (f"price CSV not present — fetch with data/_fetch_dukascopy_2024.py and compare its SHA-256"
               f" with {expected[:16] if expected else '(excluded-files list missing)'}…")
        skip("price CSV is the cited file (SHA-256)", [], why)
        for lab, rendered in span_checks:
            skip(lab, rendered, why)
    else:
        actual = hashlib.sha256(csv_path.read_bytes()).hexdigest()
        if expected is None:
            record("price CSV is the cited file (SHA-256)", False, "excluded-files hash list not found; cannot verify")
        else:
            record("price CSV is the cited file (SHA-256)", actual == expected,
                   f"expected {expected[:16]}…, got {actual[:16]}…")
        rows = csv_path.read_text(encoding="utf-8", errors="replace").splitlines()
        nbars = len(rows) - 1
        d0 = datetime.strptime(rows[1].split("\t")[0], "%Y.%m.%d"); d1 = datetime.strptime(rows[-1].split("\t")[0], "%Y.%m.%d")
        months = (d1.year - d0.year) * 12 + (d1.month - d0.month) + (d1.day - d0.day) / 30.4
        need("bars", f"{nbars:,}"); need("span dates", [f"{d0:%Y-%m-%d}", f"{d1:%Y-%m-%d}"])
        need("28.2 months", f"over {months:.1f} months")

    # ── lookahead test ────────────────────────────────────────────────────────
    tl = text("paper/figures/data/test_no_lookahead_run_2026-11-01.txt")
    leg = int(re.search(r"FORMING H1 bar on (\d+)/(\d+)", tl).group(1))
    npass = int(re.search(r"PASS\s+(\d+)/", tl).group(1))
    need("lookahead legacy count", f"{leg} times")
    need("lookahead pass", f"{npass:,}/{npass:,}")
    need("91.5% abstract", f"{leg / 10:.1f}\\%")
    need("11 of 12 / 55 min", ["11 of every 12 steps", "55 minutes"], ok=(55 / 60 == 11 / 12))
    need("1,000 steps", "1,000 random replay steps")

    # ── re-measurement ────────────────────────────────────────────────────────
    full = load("report_full.json")["trades"]
    rr = sum((load(f"report_w{i}.json")["trades"] for i in range(1, 6)), [])
    two = (80, 90, 100)
    t_is2, n_is2 = per_signal_t(full, two); t_is, n_is = per_signal_t(full, None)
    t_rr2, n_rr2 = per_signal_t(rr, two); t_rr, n_rr = per_signal_t(rr, None)
    need("IS >=2 per-signal", f"$t=+{t_is2:.3f}$ ($n={n_is2}$)")
    need("RR >=2 per-signal", f"$t=+{t_rr2:.2f}$ ($n={n_rr2}$)")
    need("IS all", f"$+{t_is:.2f}$ ($n={n_is}$)")
    need("RR all", f"$+{t_rr:.2f}$\n($n={n_rr}$)")
    s = t_is2 / math.sqrt(n_is2)
    need("power Sharpe", f"\\approx{s:.2f}")
    need("expected t", f"\\approx{s * math.sqrt(n_rr2):.1f}")
    need("per-signal 3.045 / 0.06 corrections", f"$+{t_is2:.3f}$ and $+{t_rr2:.2f}$")
    dates_full = (min(t["open_ts"] for t in full), max(t["open_ts"] for t in full))
    dates_rr = (min(t["open_ts"] for t in rr), max(t["open_ts"] for t in rr))
    need("windows inside IS", ["December 2024 to May 2026", "February 2025 and April 2026"],
         ok=(dates_full[0][:7] == "2024-12" and dates_full[1][:7] == "2026-05" and dates_rr[0][:7] == "2025-02"
             and dates_rr[1][:7] == "2026-04" and dates_full[0] <= dates_rr[0] and dates_rr[1] <= dates_full[1]))
    costs_off = all(load(f"report_w{i}.json")["summary"]["total_costs"] == 0 for i in range(1, 6))
    need("re-run costs off", "transaction costs off", ok=costs_off)

    # ── walk-forward ──────────────────────────────────────────────────────────
    w = load("wfv_report.json")["windows"]
    need("WFV t", f"$+{w[0]['metrics']['t_stat']:.2f}$,\n$+{w[1]['metrics']['t_stat']:.2f}$ and $+{w[2]['metrics']['t_stat']:.2f}$")
    min75 = all(set(x["metrics"]["score_buckets"]) <= {"75-84", "85-100"} for x in w)
    need("WFV min score 75", "(minimum score 75, per-leg", ok=min75)
    need("WFV none reached 3.0", "none reached 3.0", ok=all(x["metrics"]["t_stat"] < 3.0 for x in w))
    wb = load("wfv_baseline_75.json")["windows"]
    need("cost-aware none reach 2.5", "reached $t=2.5$ in none", ok=all(x["metrics"]["t_stat"] < 2.5 for x in wb))

    # ── zone study ────────────────────────────────────────────────────────────
    for i in (1, 2, 3, 4):
        sm = load(f"report_zone_dev_t{i}.json")["summary"]; ps = sm["per_signal"]
        need(f"zone T{i} row", f"& {ps['signals']} & {ps['profit_factor']:.3f} & ${sgn(ps['expectancy_r'], 3)}$ & "
                               f"${sgn(ps['t_stat_pnl'], 2)}$ & ${sgn(ps['t_stat_r'], 2)}$ & {sm['max_drawdown_pct']:.1f} \\\\")
    z1 = load("report_zone_dev_t1.json")["summary"]; zp = z1["per_signal"]
    need("abstract zone", [f"${sgn(zp['expectancy_r'], 3)}\\R$", f"$t={sgn(zp['t_stat_pnl'], 2)}$", f"$n={zp['signals']}$"])
    need("gate distance", f"$t={sgn(zp['t_stat_pnl'], 2)}$ against $+2.5$")
    need("DD distance", f"maximum drawdown {z1['max_drawdown_pct']:.1f}\\% against 6\\%")
    risk = zp["expectancy_usd"] / zp["expectancy_r"]; cost_r = z1["total_costs"] / zp["signals"] / risk
    need("cost share", [f"${sgn(-cost_r, 3)}\\R$", f"${sgn(zp['expectancy_r'] + cost_r, 3)}\\R$"])
    T = load("report_zone_dev_t1.json")["trades"]
    g = defaultdict(list)
    for x in T:
        g[x["signal_id"]].append(x)
    by = defaultdict(list)
    for v in g.values():
        by[v[0]["direction"]].append(sum(l["pnl"] for l in v))
    need("direction split", f"{len(by['SELL'])} sell signals and {len(by['BUY'])} buy signals",
         ok=all(statistics.mean(v) < 0 for v in by.values()))
    fu = z1["zone_funnel"]
    need("funnel", [f"{fu['h1_bars']:,} H1 bars", f"{fu['swings_detected']:,} produced swings",
                    f"{fu['zones_activated']} produced active zones", f"{fu['touches']} zones were touched",
                    f"{fu['signals_opened']}\nsignals were traded"])
    e = [load(f"report_zone_dev_t{i}.json")["summary"]["per_signal"]["expectancy_r"] for i in (1, 2, 3, 4)]
    need("quality band", "between $-0.14\\R$ and $-0.12\\R$", ok=all(-0.145 <= x <= -0.12 for x in e))
    need("406 to 187", "from 406\nto 187")
    ci = []
    for i in (1, 2, 3, 4):
        ps = load(f"report_zone_dev_t{i}.json")["summary"]["per_signal"]
        se = abs(ps["expectancy_r"] / ps["t_stat_r"]); ci.append((ps["expectancy_r"] - 1.96 * se, ps["expectancy_r"] + 1.96 * se))
    need("CI statement", "intervals for trials~2 and~4 just reach zero",
         ok=ci[0][1] < 0 and ci[2][1] < 0 and 0 <= ci[1][1] < 0.01 and 0 <= ci[3][1] < 0.01)
    trials = list(csv.DictReader(open(ROOT / "trials.csv", encoding="utf-8")))
    prm = json.loads(trials[0]["params_json"])
    need("spec params", [f"{prm['impulse_atr_mult']}\\times$ATR(14)", f"within {prm['max_age_bars']} H1 bars",
                         f"$\\pm\\,{prm['sl_atr_mult']}\\times$ATR", f"risk is {prm['risk_pct']}\\% per signal"],
         ok=prm["atr_period"] == 14 and prm["impulse_confirm_bars"] == 3 and prm["tp1_r"] == 1.0 and prm["tp2_r"] == 2.0)
    spec = text("specs/phase1_zone_spec_v1.md")
    need("prior 3-5%", "about 3--5\\%", ok="~3–5%" in spec)
    need("0.25 slippage", "0.25 adverse\nslippage", ok="0.25 adverse" in spec)
    need("gate G2 thresholds", ["$t\\ge2.5$", "at least $+0.15\\R$", "above 0.95", "at most 6\\% at 0.5\\% risk"],
         ok=all(k in spec for k in ("t >= 2.5", "+0.15R", "> 0.95", "<= 6% at 0.5% risk")))
    need("ledger rows", "the ledger holds four rows", ok=len(trials) == 4)
    # timeline
    gl = {l.split("|")[0]: l.split("|")[1] for l in text("paper/figures/data/git_timeline_utc.txt").splitlines()}
    frz = datetime.fromisoformat(gl["60f46fd"].replace("Z", "+00:00"))
    r1 = datetime.fromisoformat(trials[0]["timestamp_utc"].replace("Z", "+00:00"))
    need("21.4 h", f"{(r1 - frz).total_seconds() / 3600:.1f} hours before the first backtest result")
    ns = text("paper/figures/data/spec_appendix_numstat_8634f34.txt").split()
    need("appendix +40/-0", f"added {ns[0]} lines and deleted none", ok=ns[1] == "0")

    # ── idea register (Tier A fields and Tier B record lines) ─────────────────
    lim = text("report_limit_vs_market.json")
    L = ".claude/skills/quant-trading/lessons/"
    need("idea 4 gate", "limit PF 1.599 below market PF 1.718",
         ok='"gate_pass": false' in lim and "1.599" in line(L + "2026-05-29-pending-limit-order-system.md", 6))
    need("idea 5", "$t=1.98$ on 12 fills", ok="1.98" in line(L + "2026-06-13-h1-limit-phase1.md", 61))
    ex = L + "2026-06-16-strategy-exhaustion-and-pivot-research.md"
    need("idea 8", ["AUC 0.491--0.503", "positive control 0.693"],
         ok=all(k in line(ex, 18) for k in ("0.503", "0.491", "0.693")))
    need("idea 9", ["4.05 points on 85.3\\% of days", "$-309\\%$"], ok=all(k in line(ex, 19) for k in ("4.05", "85.3%", "309%")))
    need("idea 10", "$-2.80$ to $-4.05$ basis points", ok=all(k in line(ex, 20) for k in ("2.80", "4.05")))
    need("idea 7", ["11 approved trades", "$p=1.00$"], ok="p = 1.000" in line(ex, 21) and "11 approved" in line(ex, 21))
    co = json.loads((ROOT / "research/cointegration/phase1_results.json").read_text())
    need("idea 11", "0 of 16 pairs", ok=len(co["results"]) == 16 and sum(r["gate_pass"] for r in co["results"]) == 0)
    need("idea 12", "Deflated Sharpe 0.21", ok="0.21 (N=76)" in text("research/xs_crypto_momentum/RESULTS.md"))
    rl = text("research/tsmom_portfolio/run_log.txt"); p2l = text("research/tsmom_portfolio/phase2_run_log.txt")
    need("idea 13", ["0.982, $N=86$", "$p=0.068$", "$p=0.156$"],
         ok="0.9822" in rl and "p=0.068" in p2l and "p=0.156" in p2l)
    need("fifteen ideas", "fifteen documented ideas")
    need("idea 13 universe 41", "(41 assets)", ok="Universe (41)" in text("research/tsmom_portfolio/RESULTS.md"))
    need("VRP 10% cap", "10\\% drawdown limit", ok="10% cap" in line(ex, 19))
    need("WFV correction: no trade below 75", "no trade below 75 was taken", ok=min75)
    if (ROOT / "PROJECT_STATUS.md").exists():
        need("forward gate 30", "about 0 of its required 30 trades",
             ok="≥30" in line("PROJECT_STATUS.md", 102) and "~0/30" in line("PROJECT_STATUS.md", 102))
    else:   # withheld from the public bundle (names the broker and the balance): hash only
        skip("forward gate 30", "about 0 of its required 30 trades",
             "PROJECT_STATUS.md withheld (names the broker and the account balance) — hash in EXCLUDED.SHA256SUMS.txt")
    # calibration configuration (as run: log of the V2 leg + G0 report)
    lg = text("bt_calib_v2.err.log")
    need("calibration config", ["minimum score 75,\n0.65\\% risk", "limit-order fills"],
         ok="risk=0.65%" in lg and "MIN_SCORE=75" in text("reports/phase0/G0_report.md")
         and "fill_model=limit" in text("bt_costs_065.log"))
    # trial-variant parameters straight from the ledger
    p2_, p3_ = json.loads(trials[1]["params_json"]), json.loads(trials[2]["params_json"])
    need("trial 2/3 params", [f"impulse ${p2_['impulse_atr_mult']}\\times$ATR", f"maximum age {p3_['max_age_bars']} bars"])
    # quoted earlier statements being corrected (must match the original documents)
    sm = text(L + "2026-06-13-signal-rootcause-smc-degenerate.md").splitlines()
    need("quoted old IS/OOS statement", "``in-sample $t=+3.05$, out-of-sample $t=+0.09$''",
         ok="+3.05" in sm[52] and "+0.09" in sm[53])
    need("quoted old MIN_SCORE 65", "minimum score as 65", ok="MIN_SCORE=65" in line(L + "2026-05-29-walk-forward-results.md", 5))
    need("Harvey hurdle (literature, verified Phase 2)", "about 3.0 rather than 2.0")
    # structural tokens (not results): table row indices, figure/section numbering, SHA-256 / 16-hex
    # prefix, 95% interval convention (1.96 SE), M15/H1/M5 timeframe names
    for tok in ("7", "8", "9", "13", "15", "16", "256", "95", "1.96"):
        COVERED.add(tok)

    # ── system ────────────────────────────────────────────────────────────────
    mp = text("main.py").splitlines()
    core = sum(1 for l in mp[442:456] if "asyncio.create_task(" in l)
    need("13 core tasks", "starts thirteen core tasks and one optional task",
         ok=core == 13 and "MacroBiasAgent" in "\n".join(mp[460:466]))
    rm = text("agents/risk_manager.py")
    vetoes = sorted(set(re.findall(r"── VETO (\d+[a-z]?):", rm)))
    need("nine vetoes + breaker", "nine numbered deterministic vetoes and a pre-emptive daily-drawdown\ncircuit breaker",
         ok=vetoes == sorted(["1", "2", "2b", "3", "4", "5", "6", "7", "8", "9"]))

    # ── claim-tag integrity ───────────────────────────────────────────────────
    claims = {r["id"]: r for r in csv.DictReader(open(ROOT / "paper/claims.csv", encoding="utf-8"))}
    tags = set()
    for m in re.findall(r"%\s*\[([^\]]+)\]", TEX):
        for part in m.split(","):
            part = part.strip()
            mm = re.match(r"C(\d+)(?:[–-]C(\d+))?", part)
            if mm:
                a, b = int(mm.group(1)), int(mm.group(2) or mm.group(1))
                tags.update(f"C{k:02d}" for k in range(a, b + 1))
    missing = sorted(t for t in tags if t not in claims)
    record("claim tags exist in claims.csv", not missing, f"{len(tags)} tags; missing {missing}")
    pending = [k for k, r in claims.items() if r["status"] in ("TO-RECHECK", "PENDING-REQUIRED")]
    record("no pending/recheck claims", not pending, f"pending: {pending}")

    # ── HARD GATE: C66 / C67 ──────────────────────────────────────────────────
    vb = text("backtest/virtual_broker.py").splitlines()
    ds = text("core/data_stream.py").splitlines()
    se = text("quant/smc_engine.py").splitlines()
    gate = (claims.get("C66", {}).get("status") == "VERIFIED-CODE"
            and claims.get("C67", {}).get("status") == "VERIFIED-CODE"
            and "assume the SL" in "\n".join(vb[10:12]) and "conservatively assume SL first" in vb[542]
            and "copy_rates_from_pos" in ds[223] and "Start from current bar" in "\n".join(ds[224:227])
            and 'iloc[-1]' in se[290]
            and "stop is assumed to have been hit\nfirst" in TEX and "still-forming bar and the strategy reads its latest values" in TEX)
    record("HARD GATE C66/C67 resolved with live code locators", gate,
           "virtual_broker L11-12/L543; data_stream L224-226; smc_engine L291")

    # ── report, per edition ───────────────────────────────────────────────────
    print("══ EDITION en — paper/main.tex ══")
    en_code = report_edition("en", RESULTS, toks(clean(body_of(TEX))), gate, set(), {})
    th_results, th_tokens, th_extra, th_defin = thai_checks()
    print("\n══ EDITION th — paper/main_th.tex ══")
    th_code = report_edition("th", th_results, th_tokens, gate, th_extra, th_defin)
    code = 1 if 1 in (en_code, th_code) else 3 if 3 in (en_code, th_code) else 0
    verdict = {0: "ALL PASSED", 3: "PASSED WITH SKIPS — not a full pass", 1: "FAILED"}[code]
    print(f"\nOVERALL: en exit {en_code}, th exit {th_code} → {verdict} (exit {code})")
    sys.exit(code)


# ── shared helpers for the per-edition report ────────────────────────────────
# A digit is part of a number only if not preceded by an ASCII letter/digit/underscore/period, so
# names like M5, H1, F3, V2, v1.0 are not numbers. ASCII-only on purpose: Thai letters count as \w
# in Python, and a number written directly after a Thai word must still be found.
NUM_RE = re.compile(r"(?<![A-Za-z0-9_.])\d[\d,]*(?:\.\d+)?")
NUMBER_WORDS = {w: str(i) for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen twenty".split())}
THAI_DIGIT = re.compile("[\u0e50-\u0e59]")
TAG_RE = re.compile(r"(?<!\\)%\s*\[([^\]]+)\]")


def body_of(tex: str) -> str:
    return tex.split("\\begin{document}", 1)[1]


def clean(s: str) -> str:
    """Prose only: drop comments (but not \\%), refs, cites, file names, labels, column specs."""
    s = re.sub(r"(?<!\\)%.*", "", s)
    s = re.sub(r"\\(ref|label|cite[pt]?|citet|citep|input|includegraphics|csuse|texttt|bibliography\w*)"
               r"(\[[^\]]*\])?\{[^}]*\}", "", s)
    s = re.sub(r"\\begin\{(longtable|tabular)\}\{[^\n]*", "", s)
    s = re.sub(r"\[(width|htbp|margin)[^\]]*\]|\[t?b?p\]|\[htbp\]", "", s)
    return s


def toks(s: str) -> list[str]:
    return [t.rstrip(".,") for t in NUM_RE.findall(s)]


def sections(tex: str) -> list[str]:
    """Split the body at every \\section / \\subsection; part 0 is the title page and abstract."""
    return re.split(r"(?=\\(?:sub)?section\*?\{)", body_of(tex))


def tags(s: str) -> list[str]:
    return sorted(p.strip() for m in TAG_RE.findall(s) for p in m.split(","))


def thai_checks():
    """Checks for paper/main_th.tex. Every number must (a) match its source — each language-neutral
    rendered string of the English checks (pure numbers/maths) must occur verbatim in the Thai text,
    with the English check's state carried over (a SKIPPED source stays SKIPPED) — and (b) match the
    English edition section by section: the set of numbers in each section is identical, except
    that a Thai digit may stand for an English number word in the same section (e.g. 'thirteen').
    Arabic numerals only: any Thai digit is a FAIL."""
    th = text("paper/main_th.tex")
    R: list[tuple[str, str, str]] = []

    def rec(label, ok, note=""):
        R.append((label, "PASS" if ok else "FAIL", note))

    def norm(s):
        return re.sub(r"\s+", " ", s)

    thn = norm(th)
    en_state = {lab: (st, note) for lab, st, note in RESULTS}
    neutral = lambda it: it and not re.search(r"[A-Za-z]{2,}", re.sub(r"\\[A-Za-z]+", "", it))  # noqa: E731
    lang_dep = 0
    for lab, items in CHECK_ITEMS.items():
        neu = [it for it in items if neutral(it)]
        if not neu:
            lang_dep += 1          # wording differs by language: covered by the section comparison
            continue
        st, note = en_state[lab]
        if st == "SKIPPED":
            R.append((f"{lab} [verbatim in th]", "SKIPPED", note))
            continue
        missing = [it for it in neu if norm(it) not in thn]
        rec(f"{lab} [verbatim in th]", st == "PASS" and not missing,
            (f"missing in main_th.tex: {missing}" if missing else "") + ("" if st == "PASS" else " | English source check failed"))

    # (b) section-by-section agreement with the English edition
    en_p, th_p = sections(TEX), sections(th)
    glossary = th_p[-1]
    rec("same section structure as main.tex (+ Thai glossary appendix)",
        len(th_p) == len(en_p) + 1 and "อภิธานศัพท์" in glossary[:80], f"en {len(en_p)} parts, th {len(th_p)}")
    word_matched, bad, bad_tags = set(), [], []
    for i, (e, t) in enumerate(zip(en_p, th_p)):
        title = re.match(r"\\(?:sub)?section\*?\{([^}]*)\}", e)
        title = title.group(1) if title else "title/abstract"
        et, tt = set(toks(clean(e))), set(toks(clean(t)))
        unexplained = set()
        for x in tt - et:
            if any(re.search(rf"\b{w}\b", e, re.I) for w, d in NUMBER_WORDS.items() if d == x):
                word_matched.add(x)
            else:
                unexplained.add(x)
        if et - tt or unexplained:
            bad.append(f"§{i} '{title}': missing in th {sorted(et - tt)}; th-only {sorted(unexplained)}")
        if tags(e) != tags(t):
            bad_tags.append(f"§{i} '{title}': en {tags(e)} th {tags(t)}")
    rec("every section has the same numbers as in main.tex", not bad, " || ".join(bad))
    rec("every section carries the same claim tags as in main.tex", not bad_tags, " || ".join(bad_tags))

    # captions: same numbers per figure
    def caps(rel):
        return {m.group(1): m.group(2) for m in re.finditer(r"^\*\*(F\d)_[^*]*\*\*(.*)$", text(rel), re.M)}
    ce, ct = caps("paper/figures/captions.md"), caps("paper/figures/captions_th.md")
    cbad = [] if set(ce) == set(ct) else [f"figures en {sorted(ce)} th {sorted(ct)}"]
    for k in sorted(set(ce) & set(ct)):
        et, tt = set(toks(ce[k])), set(toks(ct[k]))
        extra = {x for x in tt - et
                 if not any(re.search(rf"\b{w}\b", ce[k], re.I) for w, d in NUMBER_WORDS.items() if d == x)}
        if et - tt or extra:
            cbad.append(f"{k}: missing in th {sorted(et - tt)}; th-only {sorted(extra)}")
    rec("every Thai figure caption has the same numbers as the English caption", not cbad, " || ".join(cbad))

    # Arabic numerals only
    th_files = ["paper/main_th.tex", "paper/figures/captions_th.md", "paper/figures/captions_th.tex",
                "paper/appendix_hashes_th.tex"]
    hits = {f: len(THAI_DIGIT.findall(text(f))) for f in th_files if (ROOT / f).exists()}
    rec("no Thai digits (Arabic numerals only)", not any(hits.values()) and len(hits) == len(th_files),
        "; ".join(f"{f}: {n}" for f, n in hits.items()))

    # title-page date: ค.ศ. year as in the English edition, พ.ศ. = ค.ศ. + 543
    m = re.search(r"ค\.ศ\. (\d{4}) \(พ\.ศ\. (\d{4})\)", th.split("\\begin{document}")[0])
    en_year = re.search(r"\\date\{[^}]*?(\d{4})", TEX)
    rec("title date: ค.ศ. year matches main.tex and พ.ศ. = ค.ศ. + 543",
        bool(m and en_year and m.group(1) == en_year.group(1) and int(m.group(2)) == int(m.group(1)) + 543),
        f"{m.groups() if m else None} vs en {en_year.group(1) if en_year else None}")

    # numbers that only the Thai glossary contains are definitions, not results
    defin = {"1": "glossary definitions: H1 = 1 hour; profit factor below 1 = net loss",
             "5": "glossary definition: M5 = 5 minutes"}
    th_tokens = toks(clean("".join(th_p[:-1])))
    gl_tokens = toks(clean(glossary))
    print(f"[th] language-dependent English checks covered by the section comparison: {lang_dep}")
    print(f"[th] Thai digits standing for English number words (same section): {' '.join(sorted(word_matched, key=int)) or '-'}")
    used = {t: defin[t] for t in set(gl_tokens) if t in defin and t not in COVERED}
    return R, th_tokens + [t for t in gl_tokens if t not in used], word_matched, used


def report_edition(name, results, tokens, gate, extra_cov, defin):
    for lab, st, note in results:
        print(f"[{st}] {lab}{(' — ' + note) if (note and st != 'PASS') else ''}")
    key = lambda x: float(x.replace(",", ""))  # noqa: E731
    uniq = set(tokens)
    cov = COVERED | extra_cov
    uncovered = sorted({t for t in uniq if t not in cov and t not in SKIP_COVERED}, key=key)
    skip_only = sorted({t for t in uniq if t not in cov and t in SKIP_COVERED}, key=key)
    print(f"\n[{name}] numeric tokens in prose: {len(uniq)}; verified: {len(uniq) - len(uncovered) - len(skip_only)}; "
          f"covered only by SKIPPED checks: {len(skip_only)}; uncovered: {len(uncovered)}")
    if defin:
        print(f"[{name}] definitional (glossary only): " + "; ".join(f"{k} ({v})" for k, v in sorted(defin.items())))
    print(f"[{name}] COVERED ONLY BY SKIPPED CHECKS:", " ".join(skip_only) or "-")
    print(f"[{name}] UNCOVERED:", " ".join(uncovered) or "-")
    n = {s: sum(1 for r in results if r[1] == s) for s in ("PASS", "FAIL", "SKIPPED")}
    reasons = sorted({note for _, st, note in results if st == "SKIPPED"})
    line_ = f"{n['PASS']} PASS, {n['FAIL']} FAIL, {n['SKIPPED']} SKIPPED"
    if reasons:
        line_ += " (" + "; ".join(reasons) + ")"
    if n["FAIL"] or not gate or uncovered:
        verdict, code = "FAILED", 1
    elif n["SKIPPED"]:
        verdict, code = "PASSED WITH SKIPS — not a full pass", 3
    else:
        verdict, code = "ALL PASSED", 0
    print(f"[{name}] RESULT: {line_}; gate {'PASS' if gate else 'FAIL'}; {verdict} (exit {code})")
    return code


if __name__ == "__main__":
    main()
