"""Stage 2: locate the Governor write-in region on a ballot front.

The Governor contest sits in a different place on every ballot style, so we find
it dynamically:

  1. OCR the page; find the "Governor" header token (reliable, high confidence).
  2. Within Governor's column, detect the write-in *underline* by horizontal
     dark-pixel projection. The printed underline is a long continuous run that
     candidate text and the oval outline never produce, so it is a robust anchor
     even when OCR misses the faint "Write-In" microlabel (it often does).
  3. From the line anchor, derive the write-in crop region, the oval sub-box,
     and the handwriting band as *relative* (fraction-of-page) boxes.

Because layout is fixed per style, the relative boxes are cached per style in the
`regions` table and reused for every later page of that style -- no per-page OCR.
"""
from __future__ import annotations

import numpy as np

from . import db
from .common import load_gray, ocr_tsv, ocr_text
from .config import CONFIG

DARK = 110              # grayscale threshold for "ink" (used by stage 3 too)
LINE_DARK = 140         # looser threshold for the faint printed underline
LINE_GAP = 8            # px: bridge anti-aliasing breaks in the underline
MIN_LINE_RUN = 110      # px: min (bridged) run to count as the underline
MAX_LINE_FRAC = 0.85    # runs longer than this fraction of column width are borders
# A contest-box bottom separator is a long run flush to the box's LEFT edge. The
# left-edge test (start <= MIN_INDENT) is what separates it from the indented
# write-in underline, so the length threshold can sit below MAX_LINE_FRAC.
BORDER_FRAC = 0.80
MIN_INDENT = 70         # px: the write-in underline starts right of the oval
COL_W_FRAC = 0.22       # column width as fraction of page width
SEARCH_FRAC = 0.34      # how far below Governor to search for its write-in line


def find_governor(rows: list[dict]):
    token = CONFIG.contest.header_token
    cands = [r for r in rows if r["text"].lower().startswith(token) and r["conf"] > 40]
    if not cands:
        return None
    # Highest-confidence, topmost.
    cands.sort(key=lambda r: (-r["conf"], r["top"]))
    return cands[0]


def _longest_dark_run(mask_row, max_gap: int = LINE_GAP) -> tuple[int, int]:
    """Return (start, length) of the longest run of True, bridging gaps of up to
    `max_gap` False pixels. Anti-aliasing breaks a faint printed underline into
    fragments; bridging recovers it while word-spacing (wider gaps) still splits
    candidate text into short pieces."""
    best_len = best_start = 0
    start = last_true = None
    for i, v in enumerate(mask_row):
        if v:
            if start is None:
                start = i
            last_true = i
        elif start is not None and i - last_true > max_gap:
            if last_true - start + 1 > best_len:
                best_len, best_start = last_true - start + 1, start
            start = last_true = None
    if start is not None and last_true - start + 1 > best_len:
        best_len, best_start = last_true - start + 1, start
    return best_start, best_len


def _next_contest_top(rows, x0, x1, gov_top):
    """Top of the next contest's 'Vote for' line below Governor, bounding the
    governor contest. Returns None if not found."""
    tops = []
    for r in rows:
        # Match the contest's "Vote (for One)" header. Exact token only --
        # a prefix match catches candidate names like "Forest".
        if r["text"].lower() == CONFIG.contest.next_contest_token and x0 <= r["left"] <= x1 \
                and r["top"] > gov_top + 60 and r["conf"] > 30:
            tops.append(r["top"])
    return min(tops) if tops else None


def _find_label_top(img, x0, x1, y0, y1):
    """Top (original-y) of the 'Write-In' microlabel in a band, via upscaled OCR.
    Returns the topmost match (the governor contest's own write-in)."""
    crop = img.crop((x0, y0, x1, y1))
    w, h = crop.size
    big = crop.resize((w * 2, h * 2))
    best = None
    for t in ocr_tsv(big, psm=6):
        txt = t["text"].lower().replace("-", "").replace(".", "")
        if t["conf"] > 20 and ("write" in txt or txt in ("writein", "writ", "rite")):
            orig_top = y0 + t["top"] // 2
            if best is None or orig_top < best:
                best = orig_top
    return best


def _indented_runs(a, x0, x1, y0, y1):
    """List of (y, start_offset, length) for bridged runs longer than MIN_LINE_RUN."""
    band = a[y0:y1, x0:x1] < LINE_DARK
    out = []
    for i in range(band.shape[0]):
        start, length = _longest_dark_run(band[i])
        if length > MIN_LINE_RUN:
            out.append((y0 + i, start, length))
    return out


def find_writein_line(img, gov_left: int, gov_top: int, rows=None):
    """Return (line_y, line_x_start, line_x_end, col_x0, col_x1) or None.

    Primary anchor is the 'Write-In' microlabel printed just under the underline
    (found by targeted upscaled OCR) -- the underline is the indented run right
    above it. Falls back to a border-bounded projection when no label is read.
    """
    W, H = img.size
    a = np.asarray(img, dtype=np.uint8)
    x0 = max(0, gov_left - 45)
    x1 = min(W, x0 + int(COL_W_FRAC * W))
    colw = x1 - x0
    y0 = gov_top + 45
    nxt = _next_contest_top(rows, x0, x1, gov_top) if rows is not None else None
    y_end = (nxt - 12) if nxt is not None else gov_top + int(SEARCH_FRAC * H)
    y1 = min(H, y_end)
    if y1 <= y0:
        return None

    def indented(y, start, length):
        return start > MIN_INDENT and MIN_LINE_RUN < length <= MAX_LINE_FRAC * colw

    # Primary: anchor on the "Write-In" label, take the underline just above it.
    label_top = _find_label_top(img, x0, x1, y0, y1)
    if label_top is not None:
        near = [r for r in _indented_runs(a, x0, x1, max(y0, label_top - 48), label_top + 2)
                if indented(*r)]
        if near:
            y, start, length = max(near, key=lambda r: r[2])  # longest = the line
            return (y, x0 + start, x0 + start + length, x0, x1)
        return (label_top - 16, x0 + 100, x1 - 10, x0, x1)  # estimate if no run read

    # Fallback (no label read): the write-in underline is the longest *indented*
    # run sitting above the governor box's bottom separator. The separator is a
    # long run flush to the box's left edge (NOT indented); bounding at it excludes
    # the next contest's header text, which lives below the separator and is what a
    # long candidate list (e.g. the 14-name Republican governor race) used to make
    # the old "last indented run" heuristic latch onto. Choosing the longest
    # indented run -- not the lowest -- matches the label path and avoids stray
    # runs just under the line (separator anti-aliasing, label text).
    runs = _indented_runs(a, x0, x1, y0, y1)
    seps = [y for y, s, l in runs
            if l >= BORDER_FRAC * colw and s <= MIN_INDENT and y > gov_top + 250]
    cutoff = min(seps) if seps else y1
    cand = [(y, s, l) for y, s, l in runs if y < cutoff and indented(y, s, l)]
    if not cand:
        return None
    y, start, length = max(cand, key=lambda r: r[2])
    return (y, x0 + start, x0 + start + length, x0, x1)


def _validate(img, region_px) -> bool:
    """Confirm the region is really a write-in line: the 'Write-In' microlabel
    sits just under the printed line, near the bottom of the crop. The label is
    tiny, so upscale before OCR and accept partial reads."""
    x0, y0, x1, y1 = region_px
    strip = img.crop((x0, y1 - 22, x1, y1 + 16))
    w, h = strip.size
    strip = strip.resize((w * 3, h * 3))
    txt = ocr_text(strip, psm=6).lower().replace(" ", "").replace("-", "")
    return any(k in txt for k in ("writein", "write", "writ", "rite"))


def locate(img, rows: list[dict] | None = None) -> dict | None:
    """Return relative region/oval boxes for the Governor write-in, or None."""
    if rows is None:
        rows = ocr_tsv(img, psm=3)
    gov = find_governor(rows)
    if gov is None:
        return None
    line = find_writein_line(img, gov["left"], gov["top"], rows)
    if line is None:
        return None
    ly, lxs, lxe, cx0, cx1 = line
    W, H = img.size

    # Vision crop: oval + handwriting + line + label (a bit of the last candidate
    # row above is harmless; the model is told to read only the write-in line).
    region_px = (cx0, ly - 60, cx1, ly + 26)
    # Oval sits just left of the line start, vertically centered on the line.
    oval_px = (lxs - 54, ly - 30, lxs - 2, ly + 6)

    def rel(box):
        return (box[0] / W, box[1] / H, box[2] / W, box[3] / H)

    return {"region_rel": rel(region_px), "oval_rel": rel(oval_px),
            "valid": _validate(img, region_px)}


def main(argv=None):
    """Standalone pass: locate (and cache by style) for classified fronts."""
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--box", default=None)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args(argv)

    conn = db.connect()
    q = "SELECT path, layout FROM images WHERE status = ?"
    params = [db.CLASSIFIED]
    if args.box:
        q += " AND box = ?"
        params.append(args.box)
    if args.limit:
        q += f" LIMIT {int(args.limit)}"
    rows = conn.execute(q, params).fetchall()
    print(f"locating for {len(rows)} fronts")
    for i, r in enumerate(rows, 1):
        path, layout = r["path"], r["layout"]
        try:
            cached = db.get_region(conn, layout)
            if cached is None:
                img = load_gray(path)
                loc = locate(img)
                if loc is None:
                    db.set_status(conn, path, db.SKIP_NO_GOVERNOR, err="locate: no governor/line")
                    continue
                db.save_region(conn, layout, loc["region_rel"], loc["oval_rel"],
                               path, valid=int(loc["valid"]))
            db.set_status(conn, path, db.LOCATED)
        except Exception as e:  # noqa: BLE001
            db.set_status(conn, path, db.ERROR, err=f"locate: {e}")
        if i % 50 == 0:
            conn.commit()
            print(f"  {i}/{len(rows)}")
    conn.commit()
    print("status:", db.counts_by_status(conn))


if __name__ == "__main__":
    main()
