"""Stage 3: detect whether the Governor write-in has any mark.

Two passes:
  score()  -- cheap CPU ink measurement per ballot (oval fill + handwriting band).
  decide() -- per-layout baseline + margin: a marked ballot is an OUTLIER above
              its layout's blank baseline.

Why a per-layout baseline: the write-in region always contains preprinted ink
(the last candidate's name, the line, the 'Write-In' label) whose amount depends
on the layout (a two-line candidate name inks far more than a one-line one). A
single global threshold therefore flags every long-name layout as "marked". The
printed baseline is constant within a layout, so a handwritten name or filled
oval shows up as a clear positive outlier above it.

Per the user's rule, *any* "Pencil" written counts, so has_mark fires on either
signal (recall-favoring); the vision read + fuzzy match remove false positives.
"""
from __future__ import annotations

import numpy as np

from . import db
from .common import load_gray
from .config import CONFIG
from .stage2_locate import DARK

# Mark-decision thresholds (see config.py [mark]). Margins are above the
# per-layout blank baseline (dark-ratio units); calibrated on AB-0028: filled
# write-in ovals score >=0.25 vs <=0.16 empty; handwriting >=0.15 vs <=0.08 blank.
OVAL_MARGIN = CONFIG.mark.oval_margin
LINE_MARGIN = CONFIG.mark.line_margin
# Fallback absolute thresholds when a layout has too few samples to baseline.
OVAL_ABS = CONFIG.mark.oval_abs
LINE_ABS = CONFIG.mark.line_abs
MIN_SAMPLES = CONFIG.mark.min_samples
BASELINE_PCTL = CONFIG.mark.baseline_pctl  # robust "blank" level even if many ballots are written-in
# Handwriting band height above the printed underline. Kept tight so the printed
# candidate name above the line does not leak in (it sits higher up).
HAND_BAND_TOP = 32
HAND_BAND_BOT = 4


def _abs(box_rel, w, h):
    return (int(box_rel[0] * w), int(box_rel[1] * h),
            int(box_rel[2] * w), int(box_rel[3] * h))


def _dark_ratio(arr, box):
    x0, y0, x1, y1 = box
    sub = arr[max(0, y0):y1, max(0, x0):x1]
    if sub.size == 0:
        return 0.0
    return float((sub < DARK).mean())


def score(img, region_rel, oval_rel) -> dict:
    """Raw ink signals for one ballot's write-in region (no decision yet)."""
    w, h = img.size
    region = _abs(region_rel, w, h)
    oval = _abs(oval_rel, w, h)
    a = np.asarray(img, dtype=np.uint8)
    oval_fill = _dark_ratio(a, oval)
    line_y = oval[3] - 6                  # underline ~6px below oval bottom
    # Tight band just above the line (where handwriting sits) -> excludes the
    # printed candidate name, which sits higher in the region.
    hand = (oval[2] + 2, line_y - HAND_BAND_TOP, region[2], line_y - HAND_BAND_BOT)
    line_ink = _dark_ratio(a, hand)
    return {"oval_fill_score": oval_fill, "line_ink_score": line_ink, "region": region}


def decide_one(oval_fill, line_ink, base_oval, base_line, n_samples) -> bool:
    if n_samples >= MIN_SAMPLES:
        return (oval_fill - base_oval) > OVAL_MARGIN or (line_ink - base_line) > LINE_MARGIN
    return oval_fill > OVAL_ABS or line_ink > LINE_ABS


def crop_path_for(box: str, seq: str) -> str:
    return str(db.CROP_DIR / f"{box}_{seq}.png")


def decide(conn, box=None) -> int:
    """Per-layout baseline pass: turn SCORED rows into MARKED / SKIP_BLANK."""
    layouts = [r["layout"] for r in conn.execute(
        "SELECT DISTINCT i.layout FROM images i WHERE i.status = ?"
        + (" AND i.box = ?" if box else ""),
        [db.SCORED] + ([box] if box else [])).fetchall()]
    total_marked = 0
    for layout in layouts:
        # Baseline from the whole layout population scored so far (robust pctl).
        pop = conn.execute(
            "SELECT r.oval_fill_score o, r.line_ink_score l FROM images i "
            "JOIN results r ON r.path=i.path WHERE i.layout=? AND r.oval_fill_score IS NOT NULL",
            (layout,)).fetchall()
        ov = [p["o"] for p in pop]
        ln = [p["l"] for p in pop]
        n = len(ov)
        base_o = float(np.percentile(ov, BASELINE_PCTL)) if ov else 0.0
        base_l = float(np.percentile(ln, BASELINE_PCTL)) if ln else 0.0
        reg = db.get_region(conn, layout)
        region_valid = bool(reg and reg["valid"])
        rows = conn.execute(
            "SELECT i.path, i.box, i.seq, r.oval_fill_score o, r.line_ink_score l "
            "FROM images i JOIN results r ON r.path=i.path WHERE i.layout=? AND i.status=?"
            + (" AND i.box=?" if box else ""),
            [layout, db.SCORED] + ([box] if box else [])).fetchall()
        for r in rows:
            # Apply the ink decision even on an unvalidated region. Force-marking
            # every front of an invalid layout floods the vision queue and the
            # tally with phantom marks: a mislocated region reads the adjacent
            # printed text (the next contest header, the last candidate name), not
            # handwriting, so it recovers no real write-in -- it only manufactures
            # false ones. The stored ink scores still gate genuine marks; we just
            # flag the marked subset for review so a mislocated layout surfaces for
            # relocation without polluting the count.
            marked = decide_one(r["o"], r["l"], base_o, base_l, n)
            if not region_valid and marked:
                db.add_review(conn, r["path"], "unvalidated_region", "", 0.0,
                              crop_path_for(r["box"], r["seq"]))
            if marked:
                total_marked += 1
                crop_path = crop_path_for(r["box"], r["seq"])
                load_gray(r["path"]).crop(_abs_region(conn, layout, r["path"])).save(crop_path)
                db.upsert_result(conn, r["path"], has_mark=1, crop_path=crop_path)
                db.set_status(conn, r["path"], db.MARKED)
            else:
                db.upsert_result(conn, r["path"], has_mark=0)
                db.set_status(conn, r["path"], db.SKIP_BLANK)
        conn.commit()
    return total_marked


def _abs_region(conn, layout, path):
    reg = db.get_region(conn, layout)
    w, h = load_gray(path).size
    return _abs((reg["x0"], reg["y0"], reg["x1"], reg["y1"]), w, h)


def main(argv=None):
    """Standalone: score located fronts then decide marks (use run.py for pool)."""
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--box", default=None)
    args = ap.parse_args(argv)
    conn = db.connect()
    rows = conn.execute(
        "SELECT i.path, i.layout FROM images i WHERE i.status=?"
        + (" AND i.box=?" if args.box else ""),
        [db.LOCATED] + ([args.box] if args.box else [])).fetchall()
    print(f"scoring {len(rows)} located fronts")
    for r in rows:
        reg = db.get_region(conn, r["layout"])
        if reg is None:
            db.set_status(conn, r["path"], db.ERROR, err="mark: no region")
            continue
        img = load_gray(r["path"])
        s = score(img, (reg["x0"], reg["y0"], reg["x1"], reg["y1"]),
                  (reg["ox0"], reg["oy0"], reg["ox1"], reg["oy1"]))
        db.upsert_result(conn, r["path"], oval_fill_score=s["oval_fill_score"],
                         line_ink_score=s["line_ink_score"])
        db.set_status(conn, r["path"], db.SCORED)
    conn.commit()
    marked = decide(conn, args.box)
    print(f"done. marked={marked}")
    print("status:", db.counts_by_status(conn))


if __name__ == "__main__":
    main()
