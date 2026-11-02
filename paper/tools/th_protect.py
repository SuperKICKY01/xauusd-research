r"""
th_protect.py — wrap every word listed in paper/tools/th_protected_words.txt in \tw{...} inside
the body of paper/main_th.tex (idempotent: already-wrapped occurrences are left alone).

Why: XeTeX breaks Thai lines at word boundaries found by ICU's dictionary segmentation, which
splits some loanwords and compounds. \tw{} makes the word unbreakable while keeping a break
opportunity on each side. Generated Thai captions get the same treatment in captions_to_tex.py.

Usage: python paper/tools/th_protect.py <project root>
"""
import re
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
tex = root / "paper" / "main_th.tex"
lst = root / "paper" / "tools" / "th_protected_words.txt"
words = [w.strip() for w in lst.read_text(encoding="utf-8").splitlines()
         if w.strip() and not w.lstrip().startswith("#")]
s = tex.read_text(encoding="utf-8")
marker = "\\begin{document}"
head, body = s.split(marker, 1)
total = 0
# Longest first, so a compound (e.g. ข้อค้นพบ) is wrapped before its part (ค้นพบ). The pattern
# skips text already inside a \tw{...} group.
for w in sorted(words, key=len, reverse=True):
    pat = re.compile(r"(?<!\\tw\{)(?<![฀-๿}]\\tw\{)" + re.escape(w))
    out, pos, k = [], 0, 0
    for m in pat.finditer(body):
        # skip if this occurrence lies inside an existing \tw{...}
        open_ = body.rfind("\\tw{", 0, m.start())
        if open_ != -1 and body.find("}", open_) >= m.end():
            continue
        out.append(body[pos:m.start()]); out.append("\\tw{" + w + "}"); pos = m.end(); k += 1
    out.append(body[pos:])
    body = "".join(out)
    total += k
tex.write_text(head + marker + body, encoding="utf-8")
print(f"wrapped {total} new occurrence(s) of {len(words)} protected words")
