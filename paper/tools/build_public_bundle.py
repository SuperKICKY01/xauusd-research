"""
build_public_bundle.py — stage the public evidence bundle and its hash table.

Redaction rule (the ONLY transformation applied): every occurrence of the original
project folder name is replaced by the token `project`. The folder name itself is read from
the --folder-name argument so it never appears in this file. All other bytes are copied
unchanged. Files listed in EXCLUDE are not published (hash-only).

Outputs
  paper/public_bundle/<relative path>         redacted (or verbatim) copies
  paper/public_bundle/SHA256SUMS.txt          hashes of the public copies
  paper/appendix_hashes.tex                   LaTeX longtable rows for the appendix
  stdout                                      redaction counts + identifier scan

Usage: python paper/tools/build_public_bundle.py --root <project root> --folder-name <name>
           [--scan-file <file with one sensitive literal per line>]
       python paper/tools/build_public_bundle.py --root <public repo root> --verify
"""
from __future__ import annotations

import argparse
import hashlib
import re
import shutil
from pathlib import Path

EXCLUDE = {
    "data/xauusd_m5_full.csv",   # licensing unverified: hash + fetch script only
    "logs/chaos.log",            # contains a demo account number and broker server name: hash only
    ".claude/skills/quant-trading/lessons/2026-06-05-llm-htf-demotion.md",  # contains the demo account number
    "PROJECT_STATUS.md",         # names the broker and the account balance: hash only
}
# Self-contained by construction: every evidence path must resolve inside --root.
REF_SUMS = "paper/public_bundle.SHA256SUMS.txt"
REF_EXCLUDED = "paper/public_bundle.EXCLUDED.SHA256SUMS.txt"


def read_sums(path: Path) -> dict[str, str]:
    out = {}
    for ln in path.read_text(encoding="utf-8").splitlines():
        if ln.strip():
            h, _, p = ln.partition("  ")
            out[p.strip()] = h.strip()
    return out


def verify(root: Path) -> None:
    """Public-repository mode. The files in a published repository are already the redacted
    copies, so the redaction labels cannot be regenerated from them; instead every evidence file
    is checked byte-for-byte against the committed hash lists. Nothing is written.
    Withheld files that are absent are reported as WITHHELD (hash-only), never as verified."""
    rels = [l.strip() for l in (root / "paper" / "_evidence_files.txt").read_text(encoding="utf-8").splitlines()
            if l.strip()]
    pub, exc = read_sums(root / REF_SUMS), read_sums(root / REF_EXCLUDED)
    ok = withheld = 0
    bad = []
    if set(rels) != set(pub) | set(exc):
        bad.append(f"evidence list and hash lists disagree: {sorted(set(rels) ^ (set(pub) | set(exc)))}")
    for rel in rels:
        f = root / rel
        if rel in EXCLUDE:
            if not f.exists():
                withheld += 1
                continue
            if hashlib.sha256(f.read_bytes()).hexdigest() == exc.get(rel):
                ok += 1
            else:
                bad.append(f"withheld file present but hash differs: {rel}")
            continue
        if not f.exists():
            bad.append(f"MISSING: {rel}")
            continue
        h = hashlib.sha256(f.read_bytes()).hexdigest()
        if h == pub.get(rel):
            ok += 1
        else:
            bad.append(f"HASH DIFFERS: {rel}")
    print(f"bundle verify: {ok} files byte-identical to the committed hash lists; "
          f"{withheld} withheld (hash-only, absent); {len(bad)} problems")
    for b in bad:
        print("  " + b)
    raise SystemExit(1 if bad else 0)


def tex_escape(s: str) -> str:
    return (s.replace("\\", "/").replace("_", r"\_").replace("&", r"\&").replace("%", r"\%")
             .replace("#", r"\#"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--folder-name", default=None)
    ap.add_argument("--verify", action="store_true",
                    help="public-repository mode: check every evidence file against the committed hash lists")
    ap.add_argument("--scan-file", default=None)
    ap.add_argument("--out", default=None,
                    help="bundle directory (default paper/public_bundle). With --out, the appendix table is "
                         "written into that directory instead of paper/, so a verification rebuild never "
                         "touches the committed outputs.")
    a = ap.parse_args()
    root = Path(a.root).resolve()
    if a.verify:
        verify(root)
    if not a.folder_name:
        raise SystemExit("--folder-name is required to build the bundle (or use --verify)")
    out =Path(a.out).resolve() if a.out else root / "paper" / "public_bundle"
    tex_path = (out / "appendix_hashes.tex") if a.out else (root / "paper" / "appendix_hashes.tex")
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    old = a.folder_name.encode()
    new = b"project"
    sensitive = []
    if a.scan_file:
        sensitive = [l.strip().encode() for l in Path(a.scan_file).read_text(encoding="utf-8").splitlines()
                     if len(l.strip()) >= 4]

    rels = [l.strip() for l in (root / "paper" / "_evidence_files.txt").read_text(encoding="utf-8").splitlines()
            if l.strip()]

    rows, sums, excluded_sums, flagged = [], [], [], []
    for rel in rels:
        src = (root / rel).resolve()
        if root not in src.parents:
            raise SystemExit(f"evidence path escapes the repository: {rel}")
        pub_rel = rel
        data = src.read_bytes()
        n_red = data.count(old)
        if pub_rel in EXCLUDE:
            h = hashlib.sha256(data).hexdigest()
            rows.append((pub_rel, h, "not published (hash of original)"))
            excluded_sums.append(f"{h}  {pub_rel}")
            continue
        red = data.replace(old, new)
        assert old not in red
        for s in sensitive:
            if s in red:
                flagged.append(pub_rel)
        dst = out / pub_rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(red)
        h = hashlib.sha256(red).hexdigest()
        sums.append(f"{h}  {pub_rel}")
        plural = "" if n_red == 1 else "s"
        rows.append((pub_rel, h, f"redacted ({n_red} replacement{plural})" if n_red else "verbatim"))

    (out / "SHA256SUMS.txt").write_text("\n".join(sums) + "\n", encoding="utf-8")
    # Full hashes of files that are NOT published (content withheld; hash only), so a reader who
    # obtains such a file independently (e.g. re-fetches the price CSV) can verify it.
    (out / "EXCLUDED.SHA256SUMS.txt").write_text("\n".join(excluded_sums) + "\n", encoding="utf-8")

    def brk(p: str) -> str:
        return tex_escape(p).replace("/", r"/\allowbreak{}").replace("-", r"-\allowbreak{}")

    def note_th(note: str) -> str:
        # Thai status labels for main_th.tex; same rows, same hashes, same replacement counts.
        if note == "verbatim":
            return "ตรงตามต้นฉบับ"
        if note.startswith("not published"):
            return "ไม่เผยแพร่ (ค่าแฮชของต้นฉบับ)"
        n = re.search(r"\((\d+) replacement", note).group(1)
        return f"ปกปิดข้อมูล (แทนที่ {n} จุด)"

    for path, header, label in ((tex_path, r"File & SHA-256 (prefix) & Status \\", lambda s: s),
                                (tex_path.with_name("appendix_hashes_th.tex"),
                                 r"ไฟล์ & SHA-256 (ส่วนต้น) & สถานะ \\", note_th)):
        tex = [r"{\scriptsize",
               r"\begin{longtable}{@{}>{\raggedright\arraybackslash}p{8.6cm}p{2.7cm}>{\raggedright\arraybackslash}p{3.4cm}@{}}",
               r"\toprule", header, r"\midrule", r"\endhead"]
        tex += [rf"\texttt{{{brk(p)}}} & \texttt{{{h[:16]}}} & {label(note)} \\" for p, h, note in rows]
        tex += [r"\bottomrule", r"\end{longtable}", r"}"]
        path.write_text("\n".join(tex) + "\n", encoding="utf-8")

    n_red_files = sum(1 for _, _, n in rows if n.startswith("redacted"))
    print(f"files: {len(rows)}  published: {len(sums)}  redacted: {n_red_files}  excluded: {len(EXCLUDE)}")
    for p, _, n in rows:
        if n != "verbatim":
            print(f"  {n:40s} {p}")
    print("sensitive-literal hits in public copies:", sorted(set(flagged)) or "none")


if __name__ == "__main__":
    main()
