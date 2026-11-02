#!/usr/bin/env bash
# build.sh — regenerate every derived artifact of the report, in dependency order.
#   1. figures + captions.md / captions_th.md   (paper/figures/make_figures.py; reads evidence files only)
#   2. captions.tex / captions_th.tex           (paper/tools/captions_to_tex.py)
#   3. evidence bundle                          (paper/tools/build_public_bundle.py)
#   4. main.pdf and main_th.pdf                 (tectonic)
#
# Two modes (BUNDLE_MODE):
#   verify (default)  for the public repository. Its evidence files are already the published
#                     (redacted) copies, so the bundle is not rebuilt: every file is checked
#                     byte-for-byte against the committed hash lists, and withheld files are
#                     reported as withheld. The appendix hash tables are committed artifacts.
#   build             for the original project only. Stages the bundle (folder-name redaction),
#                     regenerates the appendix hash tables, and compares the new hash lists with
#                     the committed references without overwriting them.
#
# Usage (from the repository root):
#   FIG_PY=<python with matplotlib> APP_PY=<python> TECTONIC=<tectonic exe> [BUNDLE_MODE=build] \
#   [SCAN_FILE=<sensitive-literal list>] bash paper/build.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
: "${FIG_PY:?set FIG_PY}" "${APP_PY:?set APP_PY}" "${TECTONIC:?set TECTONIC}"
MODE="${BUNDLE_MODE:-verify}"
# Reproducible PDFs: tectonic stamps SOURCE_DATE_EPOCH instead of the build time, so a rebuild in
# a fresh clone is byte-identical to the committed PDFs. Fixed at 2026-11-01T00:00:00Z.
export SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-1793491200}"

if [ "$MODE" = build ]; then
  SCAN_ARGS=()
  [ -n "${SCAN_FILE:-}" ] && SCAN_ARGS=(--scan-file "$SCAN_FILE")
  "$FIG_PY" -I paper/figures/make_figures.py --root . --folder-name "$(basename "$ROOT")"
  "$APP_PY" -I paper/tools/captions_to_tex.py .
  "$APP_PY" -I paper/tools/build_public_bundle.py --root . --folder-name "$(basename "$ROOT")" \
      "${SCAN_ARGS[@]}"
  # Compare the rebuilt bundle with the committed reference hash lists (never overwrite them:
  # a reference that is refreshed automatically can no longer detect a change).
  for pair in "SHA256SUMS.txt:public_bundle.SHA256SUMS.txt" "EXCLUDED.SHA256SUMS.txt:public_bundle.EXCLUDED.SHA256SUMS.txt"; do
    built="paper/public_bundle/${pair%%:*}"; ref="paper/${pair##*:}"
    if cmp -s "$built" "$ref"; then echo "bundle check: $(basename "$built") MATCHES committed reference";
    else echo "bundle check: $(basename "$built") DIFFERS from committed reference $ref — evidence changed; review before updating the reference"; fi
  done
elif [ "$MODE" = verify ]; then
  "$FIG_PY" -I paper/figures/make_figures.py --root .
  "$APP_PY" -I paper/tools/captions_to_tex.py .
  "$APP_PY" -I paper/tools/build_public_bundle.py --root . --verify
else
  echo "unknown BUNDLE_MODE: $MODE" >&2; exit 2
fi

cd paper
for doc in main main_th; do
  "$TECTONIC" --keep-logs "$doc.tex" >/dev/null
  echo "$doc.pdf: overfull boxes $(grep -c Overfull "$doc.log" || true), undefined refs/cites $(grep -ci 'Reference.*undefined\|Citation.*undefined' "$doc.log" || true)"
done
