"""
make_figures.py — figures for "Measuring Before Believing" (Phase 3).

Rules enforced here:
  * Every plotted number is read from an evidence file at run time (no typed-in results).
  * Every figure records its source files + SHA-256 (first 16 hex) in captions.md, and the
    4:5 portfolio tiles carry the same source line in-image.
  * Results are shown in R, %, or t only — never currency.

Usage (figure venv; see paper/requirements-figures.txt):
    python paper/figures/make_figures.py --root <project root>
Outputs: paper/figures/F*.pdf + F*.png (300 dpi), paper/figures/tiles/T*.png (1080x1350),
         paper/figures/captions.md
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, Rectangle

# ── palette: reference instance of the dataviz method (validated 3-slot all-pairs, light) ──
BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"
INK, INK2, MUTED = "#0b0b0b", "#52514e", "#898781"
GRID, AXIS, SURFACE, NEUTRAL = "#e1e0d9", "#c3c2b7", "#fcfcfb", "#f0efec"
BLUE_LIGHT = "#86b6ef"   # ordinal step 250 (lightest legal ordinal step on light)

plt.rcParams.update({
    "font.family": ["Segoe UI", "DejaVu Sans"],
    "font.size": 9, "axes.titlesize": 10, "axes.labelsize": 9,
    "axes.edgecolor": AXIS, "axes.labelcolor": INK2, "axes.linewidth": 0.8,
    "xtick.color": MUTED, "ytick.color": MUTED, "xtick.labelcolor": INK2, "ytick.labelcolor": INK2,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6,
    "axes.spines.top": False, "axes.spines.right": False,
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
    "legend.frameon": False, "pdf.fonttype": 42, "axes.axisbelow": True,
})
META = {"Creator": "make_figures.py", "Producer": None, "CreationDate": None}  # PDF metadata
PNG_META = {"Software": "make_figures.py"}

ROOT: Path
OUT: Path
CAPTIONS: list[str] = []
CAPTIONS_TH: list[str] = []      # Thai captions for main_th.tex (same numbers, same sources)


# ── helpers ──────────────────────────────────────────────────────────────────
REDACT: bytes | None = None


def sha16(rel: str) -> str:
    """SHA-256 prefix of the file AS PUBLISHED (folder name replaced by `project`, the bundle's only
    redaction), so a reader can check it against the public copy."""
    data = (ROOT / rel).read_bytes()
    if REDACT:
        data = data.replace(REDACT, b"project")
    return hashlib.sha256(data).hexdigest()[:16]


def src_line(rels: list[str]) -> str:
    return "; ".join(f"{r} (SHA-256 {sha16(r)}…)" for r in rels)


def load(rel: str) -> dict:
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))


def fmt(v: float, nd: int = 3) -> str:
    """Signed number with a true minus sign."""
    return f"{v:+.{nd}f}".replace("-", "−")


def ci95(mean: float, t: float) -> tuple[float, float]:
    """95% normal CI recovered from the reported mean and its t-statistic (SE = mean / t)."""
    se = abs(mean / t)
    return mean - 1.96 * se, mean + 1.96 * se


def save(fig, name: str, caption: str, sources: list[str], th: str | None = None) -> None:
    """Save a figure. `th` is the Thai caption for main_th.tex; it is built from the same computed
    variables as the English caption, so the two editions cannot drift apart numerically."""
    fig.savefig(OUT / f"{name}.pdf", metadata=META, bbox_inches="tight")
    fig.savefig(OUT / f"{name}.png", dpi=300, metadata=PNG_META, bbox_inches="tight")
    plt.close(fig)
    CAPTIONS.append(f"**{name}.** {caption}\n*Source:* {src_line(sources)}.\n")
    if th is not None:
        CAPTIONS_TH.append(f"**{name}.** {th}\n*Source:* {src_line(sources)}.\n")


def tile(draw, name: str, title: str, subtitle: str, sources: list[str]) -> None:
    """4:5 portfolio tile, 1080x1350 px. `draw(fig, rect)` paints the body into rect."""
    fig = plt.figure(figsize=(7.2, 9.0), dpi=150)
    fig.text(0.06, 0.955, title, fontsize=17, weight="bold", color=INK, va="top")
    fig.text(0.06, 0.905, subtitle, fontsize=10.5, color=INK2, va="top", wrap=True)
    draw(fig, [0.10, 0.17, 0.84, 0.66])
    foot = "Source: " + "; ".join(f"{Path(r).name} · SHA-256 {sha16(r)}…" for r in sources)
    fig.text(0.06, 0.035, foot, fontsize=6.6, color=MUTED, va="bottom", wrap=True)
    fig.savefig(OUT / "tiles" / f"{name}.png", dpi=150, metadata=PNG_META)
    plt.close(fig)


def bar_label(ax, x, y, text, above=True, color=INK2, size=8.5):
    ax.annotate(text, (x, y), xytext=(0, 4 if above else -4), textcoords="offset points",
                ha="center", va="bottom" if above else "top", color=color, fontsize=size)


# ── F1: G0 calibration, legacy vs harness-v2 ─────────────────────────────────
F1_SRC = ["report_calib_v1_full.json", "report_calib_v2_full.json"]


def f1_data():
    v1 = load(F1_SRC[0])["summary"]; v2 = load(F1_SRC[1])["summary"]
    return v1, v2


def f1_draw(axes, compact=False):
    v1, v2 = f1_data()
    ax1, ax2 = axes
    # (a) t decomposition: per-leg legacy → per-signal legacy → per-signal harness-v2
    vals = [v1["t_stat"], v1["per_signal"]["t_stat_pnl"], v2["per_signal"]["t_stat_pnl"]]
    labels = ["Legacy harness\nper-leg t", "Legacy harness\nper-signal t", "Harness-v2\nper-signal t"]
    colors = [ORANGE, ORANGE, BLUE]
    hatches = ["////", "", ""]
    xs = range(3)
    for x, v, c, h in zip(xs, vals, colors, hatches):
        ax1.bar(x, v, width=0.56, color=c, hatch=h, edgecolor=SURFACE if not h else "white",
                linewidth=0.0 if not h else 0.0)
        bar_label(ax1, x, v, fmt(v, 3 if not compact else 2))
    ax1.axhline(2.5, color=INK2, lw=0.9, ls=(0, (4, 3)))
    ax1.text(1.5, 2.56, "t = 2.5 gate used in the project", ha="center", va="bottom", color=INK2,
             fontsize=7.5, bbox=dict(fc=SURFACE, ec="none", pad=0.6))
    ax1.set_xticks(list(xs), labels, fontsize=7.5 if compact else 8)
    ax1.set_ylabel("t-statistic"); ax1.set_ylim(0, max(vals) * 1.22)
    ax1.set_title("(a) Significance, same data and code", loc="left", color=INK)
    ax1.grid(axis="x", visible=False)
    # (b) per-signal E[R] with 95% CI
    rows = [("Legacy\nharness", v1["per_signal"], ORANGE), ("Harness-v2", v2["per_signal"], BLUE)]
    for x, (lab, ps, c) in enumerate(rows):
        lo, hi = ci95(ps["expectancy_r"], ps["t_stat_r"])
        ax2.bar(x, ps["expectancy_r"], width=0.5, color=c)
        ax2.errorbar(x, ps["expectancy_r"], yerr=[[ps["expectancy_r"] - lo], [hi - ps["expectancy_r"]]],
                     color=INK, lw=1.0, capsize=4)
        ax2.annotate(f"{fmt(ps['expectancy_r'])}R\nn = {ps['signals']}", (x, hi), xytext=(0, 4),
                     textcoords="offset points", ha="center", va="bottom", color=INK2, fontsize=8)
    ax2.axhline(0, color=AXIS, lw=0.9)
    ax2.set_xticks([0, 1], [r[0] for r in rows], fontsize=8)
    ax2.set_ylabel("Expectancy per signal (R)")
    ax2.set_title("(b) Expectancy, 95% CI", loc="left", color=INK)
    ax2.set_xlim(-0.6, 1.6); ax2.grid(axis="x", visible=False)
    ax2.set_ylim(min(-0.25, ax2.get_ylim()[0]), ax2.get_ylim()[1] * 1.15)


def make_f1():
    fig, axes = plt.subplots(1, 2, figsize=(7.2, 3.1), gridspec_kw={"width_ratios": [1.5, 1], "wspace": 0.38})
    f1_draw(axes)
    v1, v2 = f1_data()
    save(fig, "F1_g0_calibration",
         "Harness calibration on identical data (165,437 M5 bars) and identical strategy code; the two "
         "runs differ only in the window-slicing rule. (a) The hatched bar is the legacy per-leg "
         f"statistic; moving to the per-signal unit and then to close-time slicing reduces t from "
         f"{fmt(v1['t_stat'])} to {fmt(v1['per_signal']['t_stat_pnl'])} to {fmt(v2['per_signal']['t_stat_pnl'])}. "
         "(b) Per-signal expectancy with a 95% interval recovered from the reported t (SE = mean/t). "
         "Limit-fill configuration, costs on.", F1_SRC,
         "การปรับเทียบระบบทดสอบบนข้อมูลชุดเดียวกัน (แท่ง M5 จำนวน 165,437 แท่ง) และโค้ดกลยุทธ์ชุดเดียวกัน "
         "การรันทั้งสองต่างกันเฉพาะกฎการตัดหน้าต่างข้อมูล (a) แท่งที่แรเงาเป็นเส้นทแยงคือสถิติต่อขาแบบเดิม "
         f"การเปลี่ยนเป็นหน่วยต่อสัญญาณ แล้วจึงเปลี่ยนเป็นการตัดหน้าต่างตามเวลาปิดของแท่ง ทำให้ค่า t ลดลงจาก "
         f"{fmt(v1['t_stat'])} เป็น {fmt(v1['per_signal']['t_stat_pnl'])} และเป็น {fmt(v2['per_signal']['t_stat_pnl'])} "
         "(b) ค่าคาดหมายต่อสัญญาณ พร้อมช่วงความเชื่อมั่น 95% ที่คำนวณกลับจากค่า t ที่รายงาน (SE = mean/t) "
         "การตั้งค่าแบบจับคู่คำสั่งด้วย limit order และคิดต้นทุนการซื้อขาย")
    return v1, v2


# ── F2: equity and drawdown curves ───────────────────────────────────────────
def daily(curve):
    last = {}
    for p in curve:
        last[p["timestamp"][:10]] = p["equity"]
    days = sorted(last)
    return [datetime.fromisoformat(d) for d in days], [last[d] for d in days]


def make_f2():
    fig, (a, b) = plt.subplots(2, 1, figsize=(7.0, 3.8), sharex=True,
                               gridspec_kw={"height_ratios": [2, 1], "hspace": 0.12})
    for rel, lab, c in [(F1_SRC[0], "Legacy harness", ORANGE), (F1_SRC[1], "Harness-v2", BLUE)]:
        d = load(rel)
        x, eq = daily(d["equity_curve"])
        e0 = d["summary"]["initial_balance"]
        ret = [(e / e0 - 1) * 100 for e in eq]
        peak, dd = -1e18, []
        for e in eq:
            peak = max(peak, e); dd.append((e / peak - 1) * 100)
        a.plot(x, ret, color=c, lw=2, label=lab)
        a.annotate(f"{lab}  {ret[-1]:+.1f}%", (x[-1], ret[-1]), xytext=(4, 0), textcoords="offset points",
                   va="center", color=INK2, fontsize=8)
        b.plot(x, dd, color=c, lw=1.4)
    a.set_ylabel("Cumulative return (%)"); b.set_ylabel("Drawdown (%)")
    a.axhline(0, color=AXIS, lw=0.8); b.axhline(0, color=AXIS, lw=0.8)
    a.legend(loc="upper left", fontsize=8)
    a.set_title("Same strategy, same data: the harness fix removes return, not drawdown", loc="left", color=INK)
    s1, s2 = load(F1_SRC[0])["summary"], load(F1_SRC[1])["summary"]
    save(fig, "F2_g0_equity",
         "Cumulative return (top) and drawdown from running peak (bottom) for the two calibration runs "
         "in Fig. F1, as percentages of the simulated starting balance, sampled at each day's last bar. "
         "Daily sampling understates the intrabar maximum drawdown reported by the backtester "
         f"(legacy {s1['max_drawdown_pct']:.2f}%, harness-v2 {s2['max_drawdown_pct']:.2f}%). Return "
         "falls under the corrected harness while maximum drawdown does not.", F1_SRC,
         "ผลตอบแทนสะสม (บน) และการลดลงจากจุดสูงสุดก่อนหน้า (ล่าง) ของการรันปรับเทียบทั้งสองในภาพ F1 "
         "คิดเป็นร้อยละของเงินทุนเริ่มต้นจำลอง โดยเก็บค่าที่แท่งสุดท้ายของแต่ละวัน "
         "การเก็บค่ารายวันทำให้ค่าที่แสดงต่ำกว่าการลดลงสูงสุดภายในแท่งที่ระบบทดสอบรายงาน "
         f"(ระบบเดิม {s1['max_drawdown_pct']:.2f}%, harness-v2 {s2['max_drawdown_pct']:.2f}%) "
         "ภายใต้ระบบที่แก้ไขแล้ว ผลตอบแทนลดลง แต่การลดลงสูงสุดไม่ได้ลดลงตาม")


# ── F3: forming-bar inclusion ────────────────────────────────────────────────
F3_SRC = ["paper/figures/data/test_no_lookahead_run_2026-11-01.txt", "tests/test_no_lookahead.py"]


def make_f3():
    txt = (ROOT / F3_SRC[0]).read_text(encoding="utf-8", errors="replace")
    import re
    m = re.search(r"FORMING H1 bar on (\d+)/(\d+) steps", txt)
    p = re.search(r"PASS\s+(\d+)/(\d+)", txt)
    legacy, n = int(m.group(1)), int(m.group(2))
    fixed_viol = int(p.group(2)) - int(p.group(1))
    theory = 55 / 60 * 100
    fig, ax = plt.subplots(figsize=(4.6, 2.4))
    vals = [legacy / n * 100, theory, fixed_viol / n * 100]
    labs = ["Legacy slicing\n(measured)", "Theory\n(55 of 60 min)", "Harness-v2\n(measured)"]
    cols = [ORANGE, NEUTRAL, BLUE]
    ax.barh(range(3), vals, color=cols, edgecolor=[SURFACE, AXIS, SURFACE], height=0.55)
    for i, v in enumerate(vals):
        ax.text(v + 1.5, i, f"{v:.1f}%", va="center", color=INK2, fontsize=8.5)
    ax.set_yticks(range(3), labs, fontsize=8); ax.invert_yaxis()
    ax.set_xlim(0, 105); ax.set_xlabel(f"Decision steps whose H1 window contains a still-forming bar (n = {n})")
    ax.grid(axis="y", visible=False)
    save(fig, "F3_forming_bar",
         f"Share of {n} random M5 decision steps at which the H1 window contained a bar that had not "
         f"yet closed. Legacy slicing: {legacy}/{n}; theoretical rate for M5 steps within an hour: 55/60; "
         f"harness-v2: {fixed_viol}/{n} (mandatory test, fixed seed, re-run 2026-11-01).", F3_SRC,
         th=f"สัดส่วนของจุดตัดสินใจบนแท่ง M5 ที่สุ่มมา {n} จุด ซึ่งหน้าต่างข้อมูล H1 ณ จุดนั้นมีแท่งราคาที่ยังไม่ปิด "
            f"วิธีตัดหน้าต่างแบบเดิม: {legacy}/{n}; อัตราตามทฤษฎีสำหรับจุดตัดสินใจ M5 ภายในหนึ่งชั่วโมง: 55/60; "
            f"harness-v2: {fixed_viol}/{n} (การทดสอบบังคับ ใช้ seed คงที่ รันซ้ำเมื่อ 2026-11-01)")


# ── F4: zone DEV trials ──────────────────────────────────────────────────────
F4_SRC = [f"report_zone_dev_t{i}.json" for i in (1, 2, 3, 4)] + ["specs/phase1_zone_spec_v1.md"]
F4_LABELS = ["T1 frozen v1.0", "T2 impulse 2.0×ATR", "T3 max age 24", "T4 both"]


def f4_draw(ax, compact=False):
    rows = []
    for i in range(4):
        ps = load(F4_SRC[i])["summary"]["per_signal"]
        lo, hi = ci95(ps["expectancy_r"], ps["t_stat_r"])
        rows.append((F4_LABELS[i], ps["expectancy_r"], lo, hi, ps["signals"], ps["t_stat_pnl"]))
    for y, (lab, m, lo, hi, n, t) in enumerate(rows):
        c = ORANGE if y == 0 else BLUE_LIGHT
        ax.plot([lo, hi], [y, y], color=INK2, lw=1.2, solid_capstyle="round")
        ax.plot([m], [y], "o", ms=8 if y == 0 else 7, color=c, mec=SURFACE, mew=2)
        ax.text(0.20, y, f"{fmt(m)}R    t {fmt(t, 2)}    n {n}", va="center", color=INK2,
                fontsize=8 if not compact else 9)
    ax.axvline(0, color=AXIS, lw=1)
    ax.axvline(0.15, color=INK2, lw=1, ls=(0, (4, 3)))
    ax.text(0.14, -0.72, "pre-registered gate\nE[R] ≥ +0.15R", ha="right", va="center", color=INK2,
            fontsize=7.5 if not compact else 8.5)
    ax.text(0.20, -0.72, "E[R]    t    n  (per signal)", ha="left", va="center", color=MUTED,
            fontsize=7.5 if not compact else 8.5)
    ax.set_yticks(range(4), [r[0] for r in rows], fontsize=8.5 if not compact else 9.5)
    ax.invert_yaxis(); ax.set_ylim(3.6, -1.2)
    ax.set_xlim(-0.32, 0.50 if not compact else 0.60)
    ax.set_xticks([-0.3, -0.2, -0.1, 0.0, 0.1, 0.2])
    ax.spines["bottom"].set_bounds(-0.32, 0.2)
    ax.set_xlabel("Expectancy per signal (R), 95% CI")
    ax.grid(axis="y", visible=False)
    return rows


def make_f4():
    fig, ax = plt.subplots(figsize=(6.6, 2.8))
    rows = f4_draw(ax)
    save(fig, "F4_zone_dev",
         "Pre-registered supply/demand-zone strategy on the DEV partition (2024). T1 is the frozen "
         "specification; T2–T4 vary the two zone-quality parameters (all four runs are rows 1–4 of the "
         "append-only trial ledger). Points are per-signal expectancy net of costs; bars are 95% "
         "intervals recovered from the reported t (SE = mean/t). The dashed line is the pre-registered "
         f"gate. T1 t = {fmt(rows[0][5])}. Every point estimate is negative and every interval lies "
         "below the gate.", F4_SRC[:4] + ["trials.csv", F4_SRC[4]],
         "กลยุทธ์โซนอุปสงค์--อุปทานแบบลงทะเบียนล่วงหน้าบนชุดข้อมูล DEV (2024) T1 คือข้อกำหนดที่ตรึงไว้ "
         "T2–T4 เปลี่ยนพารามิเตอร์คุณภาพของโซนสองตัว (การรันทั้งสี่ครั้งคือแถวที่ 1–4 "
         "ของสมุดบันทึกการทดลองแบบเพิ่มได้อย่างเดียว) จุดคือค่าคาดหมายต่อสัญญาณหลังหักต้นทุน แถบคือช่วง 95% "
         "ที่คำนวณกลับจากค่า t ที่รายงาน (SE = mean/t) เส้นประคือเกณฑ์ที่ลงทะเบียนไว้ล่วงหน้า "
         f"T1 t = {fmt(rows[0][5])} ค่าประมาณทุกจุดติดลบ และทุกช่วงอยู่ต่ำกว่าเกณฑ์")


# ── F5: zone funnel ──────────────────────────────────────────────────────────
def make_f5():
    f = load(F4_SRC[0])["summary"]["zone_funnel"]
    stages = [("H1 bars", f["h1_bars"]), ("Swings detected", f["swings_detected"]),
              ("Zones activated", f["zones_activated"]), ("First touches", f["touches"]),
              ("Signals traded", f["signals_opened"])]
    fig, ax = plt.subplots(figsize=(5.6, 2.4))
    ax.barh(range(len(stages)), [s[1] for s in stages], color=BLUE, height=0.6)
    for i, (_, v) in enumerate(stages):
        ax.text(v + 60, i, f"{v:,}", va="center", color=INK2, fontsize=8.5)
    ax.set_yticks(range(len(stages)), [s[0] for s in stages]); ax.invert_yaxis()
    ax.set_xlim(0, f["h1_bars"] * 1.12); ax.grid(axis="y", visible=False)
    ax.set_xlabel("Count (trial T1, DEV 2024)")
    save(fig, "F5_zone_funnel",
         f"Signal funnel for the frozen zone specification on DEV. Touches not traded: "
         f"{f['skipped_concurrent']} skipped by the one-position limit and {f['skipped_gap']} by the "
         "gap-through-stop rule.", [F4_SRC[0]],
         f"ลำดับการคัดกรองสัญญาณของข้อกำหนดโซนที่ตรึงไว้บนชุด DEV การแตะที่ไม่ได้เข้าซื้อขาย: "
         f"{f['skipped_concurrent']} ครั้งถูกข้ามเพราะกฎเปิดสถานะได้ครั้งละหนึ่งสถานะ และ {f['skipped_gap']} "
         "ครั้งถูกข้ามเพราะกฎราคาเปิดกระโดดข้ามจุดตัดขาดทุน")


# ── F6: idea register ────────────────────────────────────────────────────────
F6_SRC = ["paper/claims.csv"]
IDEAS = [  # (label, short name, tier B?)  — mirrors Table 3 of the outline; claim IDs in claims.csv
    ("NOT SUPPORTED", "SMC confluence\n(frozen config)", False),
    ("NOT SUPPORTED", "SMC ≥2-flag\nrefinement", False),
    ("FALSIFIED", "Supply/demand\nzone v1.0", False),
    ("NOT SUPPORTED", "Pending-limit\nexecution", False),
    ("NOT SUPPORTED", "H1 + limit\nentry", True),
    ("INCONCLUSIVE", "HMM regime\nfilter", False),
    ("INCONCLUSIVE", "LLM trade\nfilter", True),
    ("NOT SUPPORTED", "ML on M15\nprice features", True),
    ("NOT SUPPORTED", "Volatility risk\npremium¹", True),
    ("NOT SUPPORTED", "Crypto order-flow\nimbalance", True),
    ("FALSIFIED", "Cointegration\npairs (16)", False),
    ("FALSIFIED", "Crypto cross-\nsectional mom.", True),
    ("NOT SUPPORTED", "Portfolio TSMOM\n(41 assets)²", False),
    ("NOT TESTED", "Prediction-market\nbasket arbitrage", False),
    ("NOT TESTED", "Grading 3rd-party\nzone calls", False),
]
LABEL_STYLE = {"FALSIFIED": (ORANGE, "white"), "NOT SUPPORTED": (BLUE, "white"),
               "INCONCLUSIVE": (AQUA, INK), "NOT TESTED": (NEUTRAL, INK)}


def make_f6():
    fig, ax = plt.subplots(figsize=(7.0, 4.4))
    ax.set_xlim(0, 5); ax.set_ylim(0, 3.55); ax.axis("off")
    for k, (lab, name, tb) in enumerate(IDEAS):
        col, row = k % 5, 2 - k // 5
        fill, txt = LABEL_STYLE[lab]
        ax.add_patch(FancyBboxPatch((col + 0.05, row + 0.06), 0.9, 0.88,
                     boxstyle="round,pad=0,rounding_size=0.04", fc=fill, ec=AXIS if lab == "NOT TESTED" else fill,
                     lw=0.8))
        if tb:  # Tier B: dashed inner border + dagger (hatching is unreliable in PDF output)
            ax.add_patch(FancyBboxPatch((col + 0.11, row + 0.12), 0.78, 0.76,
                         boxstyle="round,pad=0,rounding_size=0.03", fc="none",
                         ec=txt, lw=0.9, ls=(0, (3, 2))))
        ax.text(col + 0.5, row + 0.80, f"{k + 1}" + (" †" if tb else ""), ha="center", va="top",
                color=txt, fontsize=7.5)
        ax.text(col + 0.5, row + 0.52, name, ha="center", va="center", color=txt, fontsize=7.6,
                weight="bold", linespacing=1.15)
        ax.text(col + 0.5, row + 0.15, lab, ha="center", va="bottom", color=txt, fontsize=6.6)
    # legend row
    x = 0.05
    for lab, (fill, _) in LABEL_STYLE.items():
        ax.add_patch(Rectangle((x, 3.18), 0.14, 0.14, fc=fill, ec=AXIS))
        ax.text(x + 0.19, 3.25, lab, va="center", fontsize=7.5, color=INK2); x += 1.1
    ax.add_patch(Rectangle((x, 3.18), 0.14, 0.14, fc=SURFACE, ec=INK2, lw=0.9, ls=(0, (3, 2))))
    ax.text(x + 0.19, 3.25, "† recorded, not re-run", va="center", fontsize=7.5, color=INK2)
    save(fig, "F6_idea_register",
         "The 15 documented ideas and their epistemic labels (Table 3). Tiles marked † (dashed border) rest on a dated "
         "contemporaneous record whose generating script was not re-run (Tier B). ¹Premium present but "
         "tail risk incompatible with a 10% drawdown cap. ²Phase-1 gate passed; no significant alpha "
         "beyond a buyable managed-futures ETF. 'Not supported' means no evidence of edge, not evidence "
         "of no edge. Per-idea sources are listed by claim ID in the claims table.", F6_SRC,
         "แนวคิดที่มีเอกสารยืนยัน 15 แนวคิดและป้ายระบุระดับหลักฐาน (ตาราง 3) ช่องที่มีเครื่องหมาย † (ขอบเส้นประ) "
         "อิงบันทึกที่ลงวันที่ไว้ในขณะนั้น ซึ่งไม่ได้รันสคริปต์ที่สร้างผลซ้ำ (ระดับ B) "
         "¹พบส่วนชดเชยความเสี่ยง แต่ความเสี่ยงส่วนหางไม่สอดคล้องกับเพดานการลดลงของมูลค่า 10% "
         "²ผ่านเกณฑ์ระยะที่ 1 แต่ไม่มีผลตอบแทนส่วนเกิน (alpha) ที่มีนัยสำคัญเหนือกองทุน ETF "
         "ประเภท managed futures ที่ซื้อได้ทั่วไป 'Not supported' หมายถึงไม่พบหลักฐานของความได้เปรียบ "
         "ไม่ใช่หลักฐานว่าไม่มีความได้เปรียบ แหล่งที่มาของแต่ละแนวคิดระบุตามรหัสข้อกล่าวอ้างในตารางข้อกล่าวอ้าง")


# ── F7: IS vs re-measurement with power band ─────────────────────────────────
F7_SRC = ["report_full.json"] + [f"report_w{i}.json" for i in range(1, 6)]


def per_signal_t(trades, scores):
    sig = defaultdict(list)
    for t in trades:
        if t["score"] >= 75:
            sig[(t["open_ts"], t["direction"], t["score"])].append(t["rr_realised"])
    r = [statistics.mean(v) for k, v in sig.items() if k[2] in scores]
    return statistics.mean(r) / statistics.stdev(r) * math.sqrt(len(r)), len(r)


def make_f7():
    two = (80, 90, 100)   # score -> timing-flag bijection used by backtest/_flag_analysis.py
    t_is, n_is = per_signal_t(load(F7_SRC[0])["trades"], two)
    # re-run windows: dedup exactly as _flag_analysis.py (no overlaps among w1..w5)
    rr = []
    for rel in F7_SRC[1:]:
        rr += load(rel)["trades"]
    t_rm, n_rm = per_signal_t(rr, two)
    s = t_is / math.sqrt(n_is)
    fig, ax = plt.subplots(figsize=(5.8, 3.2))
    ns = [n / 2 for n in range(2, 2 * 260)]
    mu = [s * math.sqrt(n) for n in ns]
    ax.fill_between(ns, [m - 1.96 for m in mu], [m + 1.96 for m in mu], color=BLUE, alpha=0.12, lw=0)
    ax.plot(ns, mu, color=BLUE, lw=2, label="Expected t if the IS effect were real (±1.96)")
    ax.axhline(0, color=AXIS, lw=0.9)
    ax.plot([n_is], [t_is], "o", ms=8, color=ORANGE, mec=SURFACE, mew=2)
    ax.annotate(f"In-sample, post-hoc subset\nt {t_is:+.3f}, n {n_is}", (n_is, t_is), xytext=(-8, 8),
                textcoords="offset points", ha="right", color=INK2, fontsize=8)
    ax.plot([n_rm], [t_rm], "o", ms=8, color=ORANGE, mec=SURFACE, mew=2)
    e_rm = s * math.sqrt(n_rm)
    ax.annotate(f"Re-measurement (inside IS period)\nt {t_rm:+.2f}, n {n_rm}; expected ≈ {e_rm:.1f}",
                (n_rm, t_rm), xytext=(10, -26), textcoords="offset points", color=INK2, fontsize=8)
    ax.set_xlabel("Number of independent signals (n)"); ax.set_ylabel("Per-signal t-statistic")
    ax.set_xlim(0, 260); ax.legend(loc="upper left", fontsize=7.5)
    save(fig, "F7_power_band",
         f"Why the re-measurement is weak evidence. Per-signal t for the ≥2-timing-flag SMC subset: "
         f"in-sample (subset chosen after seeing these data) t = {t_is:+.3f}, n = {n_is}; re-run windows "
         f"t = {t_rm:+.2f}, n = {n_rm}. The re-run windows lie inside the in-sample period and are not an "
         f"out-of-sample test. The band shows the t expected at each n if the in-sample per-signal Sharpe "
         f"({s:.3f}) were true; the re-measurement falls inside it. Both sides ran on the legacy harness.",
         F7_SRC,
         f"เหตุใดการวัดซ้ำจึงเป็นหลักฐานที่อ่อน ค่า t ต่อสัญญาณของกลุ่มย่อย SMC ที่มี timing flag ≥2: "
         f"ในตัวอย่าง (กลุ่มย่อยถูกเลือกหลังเห็นข้อมูลชุดนี้) t = {t_is:+.3f}, n = {n_is}; หน้าต่างที่รันซ้ำ "
         f"t = {t_rm:+.2f}, n = {n_rm} หน้าต่างที่รันซ้ำอยู่ภายในช่วงข้อมูลในตัวอย่าง จึงไม่ใช่การทดสอบนอกตัวอย่าง "
         f"แถบแสดงค่า t ที่คาดไว้ที่แต่ละ n หากอัตราส่วนชาร์ปต่อสัญญาณในตัวอย่าง ({s:.3f}) เป็นค่าจริง "
         f"ผลการวัดซ้ำอยู่ภายในแถบนั้น ทั้งสองฝั่งรันบนระบบทดสอบแบบเดิม")
    return t_is, n_is, t_rm, n_rm


# ── F8: architecture ─────────────────────────────────────────────────────────
F8_SRC = ["main.py", "core/event_bus.py", "agents/risk_manager.py", "execution/order_executor.py",
          "memory/trade_logger.py"]


def f8_draw(ax, compact=False):
    ax.set_xlim(0, 10); ax.set_ylim(0, 10); ax.axis("off")
    fs = 8.2 if not compact else 9.0

    def box(x, y, w, h, title, sub, fill=SURFACE, edge=INK2, tc=INK):
        ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0,rounding_size=0.15",
                                    fc=fill, ec=edge, lw=1.1))
        ax.text(x + w / 2, y + h * 0.64, title, ha="center", va="center", fontsize=fs, weight="bold", color=tc)
        if sub:
            ax.text(x + w / 2, y + h * 0.28, sub, ha="center", va="center", fontsize=fs - 1.6, color=INK2 if tc == INK else tc)

    lfs = fs - 2.6   # topic-label size

    def arrow(x0, y0, x1, y1, color=INK2, ls="-"):
        ax.annotate("", (x1, y1), (x0, y0), arrowprops=dict(arrowstyle="-|>", color=color, lw=1.1,
                                                              linestyle=ls, shrinkA=2, shrinkB=2))

    def tlabel(x, y, text, ha="left", va="center"):
        ax.text(x, y, text, fontsize=lfs, color=MUTED, ha=ha, va=va, family="monospace")

    ax.set_ylim(-0.9, 10.2)
    # left column: the deterministic decision chain, top→bottom (same row = horizontal arrows)
    X, W, H = 0.2, 3.9, 1.05
    RX, RW = 6.1, 3.7
    ys = [8.7, 6.95, 5.2, 3.45, 1.7]
    box(X, ys[0], W, H, "Broker terminal (MT5)", "blocking calls run in worker threads", NEUTRAL, AXIS)
    box(X, ys[1], W, H, "DataStream", "bars + account state")
    box(X, ys[2], W, H, "SignalBuilder", "deterministic entry rules")
    box(X, ys[3], W, H, "RiskManager", "VETO 1–9 + daily-DD breaker", fill=BLUE, edge=BLUE, tc="white")
    box(X, ys[4], W, H, "OrderExecutor", "fill confirmed in deal history")
    cx = X + W / 2
    arrow(cx, ys[0], cx, ys[1] + H); tlabel(cx + 0.12, (ys[0] + ys[1] + H) / 2, "polled via connector")
    arrow(cx, ys[1], cx, ys[2] + H); tlabel(cx + 0.12, (ys[1] + ys[2] + H) / 2, "OHLCV_UPDATED")
    arrow(cx, ys[2], cx, ys[3] + H); tlabel(cx + 0.12, (ys[2] + ys[3] + H) / 2, "SIGNAL_READY")
    arrow(cx, ys[3], cx, ys[4] + H); tlabel(cx + 0.12, (ys[3] + ys[4] + H) / 2, "TRADE_DECISION")
    mid = (X + W + RX) / 2
    # right column, row-aligned with what each component feeds
    box(RX, ys[2], RW, H, "Macro-bias advisory", "optional hourly H4/D1 LLM bias;\noff by default",
        fill=SURFACE, edge=AXIS)
    arrow(RX, ys[2] + H / 2, X + W, ys[2] + H / 2, color=MUTED, ls="--")
    tlabel(mid, ys[2] + H / 2 + 0.12, "MACRO_BIAS_UPDATED", ha="center", va="bottom")
    box(RX, ys[3], RW, H, "CalendarScraper", "high-impact news windows")
    arrow(RX, ys[3] + H / 2, X + W, ys[3] + H / 2)
    tlabel(mid, ys[3] + H / 2 + 0.12, "NEWS_BLOCK_*", ha="center", va="bottom")
    box(RX, ys[4], RW, H, "TradeLogger", "positions ↔ database\nreconciliation")
    arrow(X + W, ys[4] + H / 2, RX, ys[4] + H / 2)
    tlabel(mid, ys[4] + H / 2 + 0.12, "TRADE_EXECUTED", ha="center", va="bottom")
    sy = 0.0
    box(RX, sy, RW, H, "Shutdown listener", "HALT: positions keep broker\nstops; nothing is flattened",
        fill=ORANGE, edge=ORANGE, tc="white")
    arrow(RX + RW / 2, ys[4], RX + RW / 2, sy + H)
    tlabel(RX + RW / 2 + 0.12, (ys[4] + sy + H) / 2, "desync")
    arrow(X + W, ys[4] + 0.15, RX, sy + H * 0.5)
    tlabel(mid - 0.25, sy + H * 0.62, "unconfirmed fill", ha="center", va="top")
    ax.text(X, -0.75, "In-process asyncio event bus · 13 core tasks (main.py) · no LLM on the M5 decision path",
            fontsize=fs - 1.4, color=INK2)


def make_f8():
    fig, ax = plt.subplots(figsize=(7.0, 5.6))
    f8_draw(ax)
    save(fig, "F8_architecture",
         "Runtime architecture, drawn from the publish/subscribe calls in the code (event-bus topic names "
         "in monospace). Both halt paths (reconciliation desync in the trade logger; unconfirmed fill in "
         "the order executor) raise SYSTEM_SHUTDOWN, which stops trading without closing positions. The "
         "decision path from bars to orders is deterministic; the optional language-model "
         "component is an hourly higher-timeframe advisory that is disabled by default. Secondary "
         "components (break-even monitor, pending-order manager, notifier, status exporter, system "
         "monitor, emergency liquidator) are omitted for legibility.", F8_SRC,
         th="สถาปัตยกรรมของระบบขณะทำงาน วาดจากคำสั่ง publish/subscribe ที่มีอยู่จริงในโค้ด (ชื่อหัวข้อของ "
            "event bus แสดงด้วยตัวอักษรความกว้างคงที่) เส้นทางการหยุดระบบทั้งสองทาง ได้แก่ ข้อมูลไม่ตรงกันจากการ"
            "กระทบยอดใน trade logger และการจับคู่คำสั่ง (fill) ที่ยืนยันไม่ได้ใน order executor จะส่งสัญญาณ "
            "SYSTEM_SHUTDOWN ซึ่งหยุดการซื้อขายโดยไม่ปิดสถานะที่ถืออยู่ เส้นทางการตัดสินใจตั้งแต่ข้อมูลแท่งราคา"
            "จนถึงคำสั่งซื้อขายเป็นแบบกำหนดแน่นอน (deterministic) ส่วนประกอบโมเดลภาษาซึ่งเป็นทางเลือกทำหน้าที่"
            "เพียงให้คำแนะนำรายชั่วโมงบนกรอบเวลาที่ใหญ่กว่า และปิดไว้เป็นค่าเริ่มต้น ส่วนประกอบรอง (ตัวติดตาม"
            "จุดคุ้มทุน ตัวจัดการคำสั่งรอ ตัวแจ้งเตือน ตัวส่งออกสถานะ ตัวติดตามระบบ และตัวปิดสถานะฉุกเฉิน) "
            "ถูกละไว้เพื่อให้อ่านง่าย")


# ── F9: pre-registration timeline ────────────────────────────────────────────
F9_SRC = ["paper/figures/data/git_timeline_utc.txt", "trials.csv", "specs/phase1_zone_spec_v1.md",
          "paper/figures/data/spec_appendix_numstat_8634f34.txt"]


def f9_events():
    ev = []
    for line in (ROOT / F9_SRC[0]).read_text(encoding="utf-8").splitlines():
        h, t, s = line.split("|", 2)
        ev.append((datetime.fromisoformat(t.replace("Z", "+00:00")), h, s))
    import csv
    rows = list(csv.DictReader(open(ROOT / F9_SRC[1], encoding="utf-8")))
    trials = [(datetime.fromisoformat(r["timestamp_utc"].replace("Z", "+00:00")), r["trial_id"]) for r in rows]
    return {h: t for t, h, _ in ev}, trials


def f9_appendix_numstat() -> tuple[int, int]:
    """Lines added/deleted by the post-freeze spec commit, from `git show --numstat` output."""
    line = (ROOT / F9_SRC[3]).read_text(encoding="utf-8").split()
    return int(line[0]), int(line[1])


def f9_draw(ax, compact=False):
    c, trials = f9_events()
    t0 = c["f42335b"]

    def hrs(t):
        return (t - t0).total_seconds() / 3600

    added, deleted = f9_appendix_numstat()
    n_rows = len(trials)
    items = [  # (time, label, detail, kind)   kind: commit / spec / data / verdict
        (c["f42335b"], "Harness-v2 baseline committed", "G0 calibration passed", "commit"),
        (c["60f46fd"], "SPECIFICATION FROZEN", "prior, gate G2, data partitions", "spec"),
        (c["8634f34"], "Spec appendix A committed", f"provenance only: +{added} / −{deleted} lines", "spec"),
        (c["ac719c2"], "Engine + runner committed", "unit-tested on synthetic data", "commit"),
        (trials[0][0], "Ledger row 1 written", "first DEV run (frozen spec) completed", "data"),
        (trials[1][0], f"Ledger rows 2–{n_rows} written", "robustness runs, chosen after row 1", "data"),
        (c["fb08296"], "DEV report committed", "", "commit"),
        (c["cc2a676"], "VERDICT: falsified at DEV", "EVAL partitions left unspent", "verdict"),
    ]
    colors = {"commit": INK2, "spec": BLUE, "data": ORANGE, "verdict": ORANGE}
    fs = 8.0 if not compact else 9.2
    n = len(items)
    ax.set_xlim(0, 10); ax.set_ylim(n - 0.4, -0.7); ax.axis("off")
    LX = 3.0   # spine x
    ax.plot([LX, LX], [0, n - 1], color=AXIS, lw=1.6, zorder=0)
    ax.text(LX - 0.35, -0.55, "UTC", ha="right", va="center", fontsize=fs - 1, color=MUTED)
    prev = None
    for i, (t, lab, det, kind) in enumerate(items):
        big = kind in ("spec", "verdict")
        ax.plot([LX], [i], "o", ms=10 if big else 7, color=colors[kind], mec=SURFACE, mew=2, zorder=3)
        ax.text(LX - 0.35, i, f"{t:%Y-%m-%d %H:%M}", ha="right", va="center", fontsize=fs - 0.6, color=INK2,
                family="monospace")
        ax.text(LX + 0.4, i - (0.13 if det else 0), lab, ha="left", va="center", fontsize=fs,
                color=INK, weight="bold" if big else "normal")
        if det:
            ax.text(LX + 0.4, i + 0.2, det, ha="left", va="center", fontsize=fs - 1.2, color=INK2)
        if prev is not None:
            dh = (t - prev).total_seconds() / 3600
            gap_txt = f"+{dh:.1f} h" if dh >= 0.1 else f"+{dh * 60:.0f} min"
            ax.text(LX - 0.12, i - 0.5, gap_txt, ha="right", va="center", fontsize=fs - 2.0, color=MUTED)
        prev = t
    gap = hrs(trials[0][0]) - hrs(c["60f46fd"])
    return gap


def make_f9():
    fig, ax = plt.subplots(figsize=(6.0, 4.2))
    gap = f9_draw(ax)
    added, deleted = f9_appendix_numstat()
    save(fig, "F9_preregistration_timeline",
         f"Order of events in the zone study, from git commit timestamps and the trial ledger's "
         f"own timestamps (UTC; gaps between consecutive events on the left). The specification (prior, "
         f"gate G2, partitions) was committed {gap:.1f} h before the first ledger row was written. Ledger "
         "timestamps record when a run completed; start times were not recorded, so the first run may have "
         "begun shortly before the engine commit. The spec's own provenance appendix states that no "
         "statistic was computed on any partition before the freeze. The only later spec change was that "
         f"provenance appendix ({added} lines "
         f"added, {deleted} deleted). Rows 2–4 were chosen after row 1 under the spec's DEV-tuning "
         "allowance. Commit times are self-reported by the author's machine and are not externally "
         "notarised.", F9_SRC,
         th=f"ลำดับเหตุการณ์ในการศึกษากลยุทธ์โซน จากเวลาที่บันทึกใน git commit และเวลาที่บันทึกในสมุดบันทึก"
            f"การทดลองเอง (เวลา UTC; ด้านซ้ายแสดงช่วงห่างระหว่างเหตุการณ์ที่ต่อเนื่องกัน) ข้อกำหนดของกลยุทธ์ "
            f"(ความน่าจะเป็นล่วงหน้า เกณฑ์ G2 และการแบ่งชุดข้อมูล) ถูก commit ก่อนที่จะมีการบันทึกแถวแรกของ"
            f"สมุดบันทึก {gap:.1f} ชั่วโมง เวลาในสมุดบันทึกเป็นเวลาที่การรันเสร็จสิ้น โดยไม่มีการบันทึกเวลาเริ่ม "
            f"การรันครั้งแรกจึงอาจเริ่มขึ้นก่อน commit ของ engine เพียงเล็กน้อย ภาคผนวกที่มาของพารามิเตอร์"
            f"ในข้อกำหนดระบุว่าไม่มีการคำนวณค่าสถิติใด ๆ บนชุดข้อมูลส่วนใดเลยก่อนการตรึงข้อกำหนด การแก้ไข"
            f"ข้อกำหนดเพียงครั้งเดียวหลังจากนั้นคือภาคผนวกดังกล่าว (เพิ่ม {added} บรรทัด ลบ {deleted} บรรทัด) "
            f"แถวที่ 2--4 ถูกเลือกหลังแถวที่ 1 ภายใต้ข้อกำหนดที่อนุญาตให้ปรับแต่งบนชุด DEV ได้ เวลา commit "
            f"บันทึกโดยเครื่องของผู้เขียนเอง และไม่ได้รับการรับรองจากบุคคลภายนอก")


# ── tiles (4:5) ──────────────────────────────────────────────────────────────
def make_tiles(v1, v2):
    def t_arch(fig, r):
        ax = fig.add_axes(r); f8_draw(ax, compact=True)
    tile(t_arch, "T1_architecture", "Architecture",
         "Deterministic async pipeline with a hard-coded risk gate.\nSafety stops halt the system without dumping positions.",
         F8_SRC[:2])

    def t_g0(fig, r):
        a = fig.add_axes([r[0], r[1] + r[3] * 0.50, r[2], r[3] * 0.44])
        b = fig.add_axes([r[0] + r[2] * 0.18, r[1] - 0.02, r[2] * 0.64, r[3] * 0.36])
        f1_draw((a, b), compact=True)
    tile(t_g0, "T2_g0_calibration", "Fixing the test, not the strategy",
         f"Same data, same code. Removing a lookahead bug and counting\nsignals instead of legs: "
         f"t {fmt(v1['t_stat'], 2)} → {fmt(v2['per_signal']['t_stat_pnl'], 2)}.",
         F1_SRC)

    def t_tl(fig, r):
        ax = fig.add_axes([0.02, r[1] + 0.02, 0.96, r[3]]); f9_draw(ax, compact=True)
    c, trials = f9_events()
    gap_h = (trials[0][0] - c["60f46fd"]).total_seconds() / 3600
    tile(t_tl, "T3_preregistration", "Rules first, data second",
         "The prior, the pass/fail gate and the data partitions were\n"
         f"committed {gap_h:.0f} hours before the first backtest result was recorded.",
         F9_SRC[:2])

    def t_zone(fig, r):
        ax = fig.add_axes([0.25, 0.20, 0.70, 0.60]); f4_draw(ax, compact=True)
    tile(t_zone, "T4_zone_dev", "Pre-registered test: failed",
         "Every variant lost money per signal, far from the bar set\nin advance. The evaluation data was left unspent.",
         F4_SRC[:4])


def main():
    global ROOT, OUT
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--folder-name", default=None,
                    help="original project folder name (private build only). Source hashes in captions "
                         "and tiles are then computed on the published (redacted) bytes, so they match "
                         "the public files. Omit in the public repository, whose files are already redacted.")
    a = ap.parse_args()
    ROOT = Path(a.root).resolve()
    global REDACT
    REDACT = a.folder_name.encode() if a.folder_name else None
    OUT = ROOT / "paper" / "figures"
    (OUT / "tiles").mkdir(parents=True, exist_ok=True)
    v1, v2 = make_f1()
    make_f2(); make_f3(); make_f4(); make_f5(); make_f6()
    f7 = make_f7()
    make_f8(); make_f9()
    make_tiles(v1, v2)
    (OUT / "captions.md").write_text(
        "# Figure captions (generated by make_figures.py — do not edit by hand)\n\n" + "\n".join(CAPTIONS),
        encoding="utf-8")
    (OUT / "captions_th.md").write_text(
        "# Thai figure captions (generated by make_figures.py — do not edit by hand)\n\n" + "\n".join(CAPTIONS_TH),
        encoding="utf-8")
    print("F7 check:", f7)


if __name__ == "__main__":
    main()
