# วัดก่อนเชื่อ — Measuring Before Believing

**การศึกษาเชิงหักล้างสมมติฐานแบบลงทะเบียนล่วงหน้าของกลยุทธ์โครงสร้างราคาสำหรับนักลงทุนรายย่อยบน XAUUSD**
*A Pre-Registered Falsification Study of Retail Price-Structure Strategies on XAUUSD*

SuperKICKY01 — นักวิจัยอิสระระดับนักเรียน ประเทศไทย (independent student researcher, Thailand)
รายงานทางเทคนิคอิสระ ยังไม่ผ่านการประเมินโดยผู้ทรงคุณวุฒิ (not peer-reviewed)

- รายงานฉบับภาษาไทย: [`paper/main_th.pdf`](paper/main_th.pdf)
- English report: [`paper/main.pdf`](paper/main.pdf)

[ภาษาไทย](#ภาษาไทย) · [English](#english)

---

## ภาษาไทย

### คลังเก็บนี้คืออะไร

คลังเก็บนี้เป็นรายงานทางเทคนิคพร้อมหลักฐานทั้งหมดที่ใช้สร้างรายงาน คำถามวิจัยคือ
กฎการเข้าซื้อขายที่อิงโครงสร้างราคาซึ่งสอนกันแพร่หลายในหมู่นักลงทุนรายย่อย ได้แก่ ออร์เดอร์บล็อก (order block)
และโซนอุปสงค์–อุปทาน (supply/demand zone) แสดงความได้เปรียบ (edge) ต่อสัญญาณที่วัดได้บนข้อมูล XAUUSD
ราย 5 นาทีหรือไม่ เมื่อการทดสอบย้อนหลังใช้เฉพาะข้อมูลที่มีอยู่จริง ณ เวลาตัดสินใจ
และการอนุมานทางสถิติใช้หน่วยที่เป็นอิสระต่อกัน

ไม่มีการใช้เงินจริงในการซื้อขายในการศึกษาใด ๆ ที่รายงานไว้ คลังเก็บนี้ไม่ใช่คำแนะนำการลงทุน

### ผลการศึกษาหลัก

- **ข้อบกพร่องของระบบทดสอบย้อนหลังเดิม** หน้าต่างข้อมูลกรอบเวลาใหญ่มีแท่งราคาที่ยังไม่ปิดอยู่ใน 91.5%
  ของจุดตัดสินใจ (915 จาก 1,000 จุดที่สุ่ม) และนัยสำคัญถูกคำนวณต่อขาของสถานะแทนที่จะคำนวณต่อสัญญาณ
  ระบบที่แก้ไขแล้วผ่านการทดสอบบังคับ 1,000/1,000 จุด
- **กลยุทธ์ออร์เดอร์บล็อก** เมื่อแก้ไขข้อบกพร่องทั้งสองบนข้อมูลและโค้ดชุดเดียวกัน ค่าสถิติ t ต่อสัญญาณลดลงจาก
  +2.45 เหลือ +0.92 (n = 77) ค่าคาดหมายต่อสัญญาณลดลง 62% และช่วงความเชื่อมั่น 95% ครอบคลุมศูนย์
  ผลนี้หมายถึงไม่พบหลักฐานของความได้เปรียบ ไม่ใช่หลักฐานว่าไม่มีความได้เปรียบ
- **กลยุทธ์โซนอุปสงค์–อุปทาน (ลงทะเบียนล่วงหน้า)** ข้อกำหนดถูก commit 21.4 ชั่วโมงก่อนผลการทดสอบย้อนหลังครั้งแรก
  บนชุดข้อมูล DEV (2024) ได้ค่าคาดหมาย −0.143R และ t = −2.79 (n = 406)
  เทียบกับเกณฑ์ที่ลงทะเบียนไว้ว่าต้องได้อย่างน้อย +0.15R และ t ≥ 2.5 รูปแบบที่ปรับเปลี่ยนอีกสามแบบก็ติดลบเช่นกัน
  การศึกษาหยุดที่ขั้นการพัฒนา และชุดข้อมูลสำหรับการประเมินผล (EVAL, FRESH) ไม่ได้ถูกใช้
- **ทะเบียนแนวคิด** แนวคิดที่มีเอกสารยืนยัน 15 แนวคิด พร้อมป้ายระบุระดับหลักฐาน
  และการแก้ไขข้อผิดพลาดสามข้อในเอกสารก่อนหน้าของโครงการ

ข้อจำกัดสำคัญ: ศึกษาเพียงสินทรัพย์เดียว แหล่งข้อมูลเดียว และช่วงเวลาเดียว การจับคู่คำสั่งจำลองจากแท่งราคา
ยังไม่ได้รันตัวควบคุมเชิงศูนย์บนระบบที่แก้ไขแล้ว และป้ายเวลาของ commit บันทึกโดยเครื่องของผู้เขียนเอง
รายละเอียดอยู่ในส่วนที่ 7 ของรายงาน

### หลักฐานอยู่ที่ใด

- `paper/claims.csv` — ตัวเลขทุกตัวในรายงาน พร้อมไฟล์ต้นทางและตำแหน่งในไฟล์
- ไฟล์ผลการทดสอบ (`report_*.json`, `wfv_*.json`), สมุดบันทึกการทดลอง (`trials.csv`), ข้อกำหนดที่ลงทะเบียนไว้
  (`specs/`), รายงานแต่ละขั้น (`reports/`) และโค้ดที่สร้างผล (`backtest/`, `quant/`, `core/`, `tools/`, `tests/`)
  อยู่ที่ราก (root) ของคลังเก็บ โดยมีโครงสร้างเดียวกับโครงการเดิม
- `SHA256SUMS.txt` — ค่าแฮช SHA-256 ของทุกไฟล์หลักฐานที่เผยแพร่ ไฟล์หกไฟล์ถูกปกปิดข้อมูล
  โดยการเปลี่ยนแปลงเพียงอย่างเดียวคือการแทนที่ชื่อโฟลเดอร์เดิมของโครงการด้วยคำว่า `project`
- `EXCLUDED.SHA256SUMS.txt` — ค่าแฮชของไฟล์สี่ไฟล์ที่ไม่ได้เผยแพร่ ได้แก่ ข้อมูลราคา
  (ยังไม่ได้ตรวจสอบเงื่อนไขการเผยแพร่ซ้ำ ให้ดาวน์โหลดใหม่ด้วย `data/_fetch_dukascopy_2024.py` แล้วเทียบค่าแฮช)
  ไฟล์สองไฟล์ที่มีรหัสระบุบัญชีทดลอง และไฟล์สถานะโครงการซึ่งระบุชื่อโบรกเกอร์และยอดเงินในบัญชี

### การตรวจสอบ

```bash
python paper/tools/build_public_bundle.py --root . --verify   # ตรวจทุกไฟล์หลักฐานกับค่าแฮช
python paper/tools/audit_paper.py --root .                    # ตรวจตัวเลขทั้งสองฉบับกับไฟล์ต้นทาง
```

สคริปต์ตรวจสอบคำนวณตัวเลขใหม่จากไฟล์ต้นทาง และตรวจว่าตัวเลขในฉบับภาษาไทยตรงกับไฟล์ต้นทางและฉบับภาษาอังกฤษ
การตรวจสอบที่ต้องใช้ไฟล์ที่ไม่ได้เผยแพร่จะถูกรายงานว่า **SKIPPED (ข้าม)** และไม่นับว่าผ่าน ในคลังเก็บนี้
ผลที่คาดไว้จึงเป็น **PASSED WITH SKIPS — not a full pass** (exit 3) ไม่ใช่การผ่านทั้งหมด
`paper/audit_report.txt` คือผลการตรวจบนเครื่องของผู้เขียนซึ่งมีไฟล์ครบทุกไฟล์
GitHub Actions รันการตรวจเดียวกันบนไฟล์ในคลังเก็บนี้ทุกครั้งที่มีการ push

**การทดสอบการรั่วของข้อมูลอนาคต (lookahead)** ซึ่งเป็นหลักฐานของข้อค้นพบหลัก มีสองรุ่น
`tests/test_no_lookahead_synthetic.py` รันได้ในคลังเก็บนี้และรันจริงใน CI โดยใช้ข้อมูลสังเคราะห์
โค้ดตัดหน้าต่างข้อมูลที่เผยแพร่ และไฟล์ตั้งค่าขั้นต่ำ (`tests/public_config_stub.py`)
ตรวจเงื่อนไขเดียวกันที่ลงทะเบียนไว้ล่วงหน้า (แท่งกรอบเวลาใหญ่ทุกแท่งในหน้าต่างต้องปิดก่อนหรือ ณ เวลาตัดสินใจ)
บนจุดเล่นซ้ำที่สุ่ม 1,000 จุด ผ่านเมื่อใช้กฎที่แก้ไขแล้ว และต้องไม่ผ่านเมื่อใช้กฎเดิม (การตรวจแบบกลายพันธุ์
หรือ mutation check ซึ่ง CI รันด้วย) ส่วน `tests/test_no_lookahead.py` ใช้ข้อมูลราคาจริงซึ่งไม่ได้เผยแพร่
จึงถูกรายงานว่าข้าม (SKIPPED) การทดสอบด้วยข้อมูลสังเคราะห์ครอบคลุมตรรกะของการตัดหน้าต่าง
ส่วนการทดสอบด้วยข้อมูลจริงครอบคลุมข้อมูลจริงด้วย

### การเปิดเผยการใช้ AI

ผู้เขียนเป็นนักเรียนระดับมัธยมศึกษา ระบบการซื้อขาย การทดสอบย้อนหลัง รายงาน และฉบับแปลภาษาไทย
ถูกพัฒนาด้วยผู้ช่วยเขียนโค้ด AI (Claude ของ Anthropic) ภายใต้การออกแบบและการตรวจทานของผู้เขียน
หากข้อความในฉบับภาษาไทยและฉบับภาษาอังกฤษไม่ตรงกัน ให้ถือฉบับภาษาอังกฤษเป็นหลัก

### สัญญาอนุญาต (license)

- **โค้ด** (ไฟล์ `*.py` ทั้งหมด รวมถึง `paper/tools/`, `paper/figures/make_figures.py` และ `paper/build.sh`)
  ใช้สัญญาอนุญาต MIT ดู [`LICENSE`](LICENSE)
- **เนื้อหาใน `paper/`** (ข้อความรายงานทั้งสองฉบับ ไฟล์ PDF ภาพ และ tiles) ใช้สัญญาอนุญาต
  Creative Commons Attribution 4.0 International (CC BY 4.0) ดู [`LICENSE-CC-BY-4.0.txt`](LICENSE-CC-BY-4.0.txt)
- **ฟอนต์ Sarabun** ใน `paper/fonts/sarabun/` ใช้สัญญาอนุญาตของผู้สร้างฟอนต์ คือ SIL Open Font License 1.1
  (`paper/fonts/sarabun/OFL.txt`) ไม่อยู่ภายใต้สัญญาอนุญาตสองข้างต้น
- ไฟล์อื่นนอก `paper/` (ไฟล์ผล บันทึก และสมุดบันทึกการทดลอง) เผยแพร่ร่วมกับโค้ดภายใต้สัญญาอนุญาต MIT

---

## English

### What this is

A technical report and all of the evidence used to produce it. The research question: do two
families of retail price-structure entry rules — order blocks and supply/demand zones — show a
measurable per-signal edge on XAUUSD 5-minute data when the backtest is causal and inference uses
independent units?

No real capital was traded in any of the studies reported here. Nothing here is investment advice.

### Key results

- **Defects in the original backtest harness.** The higher-timeframe window contained a
  still-forming bar at 91.5% of decision points (915 of 1,000 sampled), and significance was
  computed per position leg instead of per signal. The corrected harness passes its mandatory
  test on 1,000/1,000 points.
- **Order-block strategy.** With both defects corrected on identical data and code, the
  per-signal t-statistic fell from +2.45 to +0.92 (n = 77), per-signal expectancy fell by 62%,
  and the 95% interval includes zero. This is no evidence of an edge, not evidence of no edge.
- **Supply/demand-zone strategy (pre-registered).** The specification was committed 21.4 hours
  before the first backtest result. On the DEV partition (2024): expectancy −0.143R, t = −2.79
  (n = 406), against a pre-registered gate of at least +0.15R and t ≥ 2.5. Three variants were
  also negative. The study stopped at the development stage; the evaluation partitions (EVAL,
  FRESH) were not used.
- **Idea register.** 15 documented ideas with explicit evidence labels, and three corrections to
  earlier project documents.

Main limitations: one instrument, one data source, one period; bar-based fill simulation; no null
control yet on the corrected harness; commit timestamps recorded by the author's own machine.
See Section 7 of the report.

### Where the evidence is

- `paper/claims.csv` — every number in the report, with its source file and locator.
- Result files (`report_*.json`, `wfv_*.json`), the trial ledger (`trials.csv`), the
  pre-registered specification (`specs/`), stage reports (`reports/`) and the code that produced
  the results (`backtest/`, `quant/`, `core/`, `tools/`, `tests/`) are at the repository root,
  in the original project layout.
- `SHA256SUMS.txt` — SHA-256 of every published evidence file. Six files were redacted; the only
  change was replacing the original project folder name with the token `project`.
- `EXCLUDED.SHA256SUMS.txt` — hashes of the four unpublished files: the price data (redistribution
  terms not verified; regenerate it with `data/_fetch_dukascopy_2024.py` and compare the hash),
  two files that contain a demonstration-account identifier, and the project status file, which
  names the broker and the account balance.

### Verifying

```bash
python paper/tools/build_public_bundle.py --root . --verify   # byte-check every evidence file
python paper/tools/audit_paper.py --root .                    # recompute the numbers of both editions
```

The audit recomputes every number from its source file and checks that the Thai edition matches
both the sources and the English edition. Any check that needs an unpublished file is reported as
**SKIPPED**, never as passed. In this repository the expected result is therefore **PASSED WITH
SKIPS — not a full pass** (exit 3). `paper/audit_report.txt` is the run on the author's machine,
where every file is present. GitHub Actions runs the same checks on this repository on every push.

**The lookahead test**, the evidence for the headline finding, exists in two versions.
`tests/test_no_lookahead_synthetic.py` runs here, and in CI as a real run: it uses synthetic data,
the published window-slicing code and a minimal configuration stub (`tests/public_config_stub.py`)
to check the same pre-registered invariant (every higher-timeframe bar in a window closes at or
before the decision time) on 1,000 random replay steps. It passes with the fixed rule and must fail
with the old rule (a mutation check that CI also runs). `tests/test_no_lookahead.py` runs on the
real price data, which is withheld, so it is reported as SKIPPED. The synthetic test covers the
slicing logic; the real-data test also covers the data.

### AI-assistance disclosure

The author is a high-school student. The trading system, the backtests, the report and its Thai
translation were developed with an AI coding assistant (Anthropic's Claude), under the author's
design and review. Where the Thai and English editions differ, the English edition governs.

### License

- **Code** (all `*.py` files, including `paper/tools/`, `paper/figures/make_figures.py`, and
  `paper/build.sh`): MIT — see [`LICENSE`](LICENSE).
- **Content of `paper/`** (the text of both editions of the report, the PDFs, figures and tiles):
  Creative Commons Attribution 4.0 International (CC BY 4.0) — see
  [`LICENSE-CC-BY-4.0.txt`](LICENSE-CC-BY-4.0.txt).
- **The Sarabun font** in `paper/fonts/sarabun/` keeps its own license, the SIL Open Font License
  1.1 (`paper/fonts/sarabun/OFL.txt`); neither license above applies to it.
- Other files outside `paper/` (result files, logs, the trial ledger) are distributed with the code
  under the MIT license.
