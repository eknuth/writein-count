"""Stage 1: classify each page and read party + ballot style.

Cheap funnel gate. Only ballot *fronts* carry the Governor race, so target
cards and ballot backs are sent straight to `skip_no_governor`. We OCR only the
top header band (fast) to read:
  - page kind  (front vs back vs target card)
  - party      (Democratic / Republican / other)
  - style code (e.g. 4303-1-WS) used to cache the write-in region per style

Pure worker: `classify(path)` does no DB I/O so it can run in a process pool.
"""
from __future__ import annotations

import re

from . import db
from .common import load_gray, ocr_text
from .config import CONFIG

# Header band = top fraction of the page (where the title/party/style live).
HEADER_BAND = (0.0, 0.0, 1.0, 0.13)
# Style code is printed in the top-right corner of ballot cards.
STYLE_BAND = (0.62, 0.0, 1.0, 0.06)

# Ballot style code, e.g. 4303-1-WS, 4604-2-YS. Also matches bottom form codes.
STYLE_RE = re.compile(r"\b(\d{4}-\d{1,2}(?:-[A-Z]{1,3})?)\b")
# Numeric prefix (precinct/style), suffix dropped: 5006-1-ZS -> 5006-1.
NUMERIC_RE = re.compile(r"(\d{4}-\d{1,2})")


def layout_key(party: str | None, style: str | None) -> str | None:
    """Region-cache key. The printed -WS/-YS/-ZS suffix encodes the party layout
    but OCR drops it often, so we key on (party, numeric prefix): both reliable,
    and together they pin the physical layout (Dem and Rep lists differ)."""
    if not style:
        return None
    m = NUMERIC_RE.search(style)
    if not m:
        return None
    return f"{party or 'other'}|{m.group(1)}"

# Target cards are notably shorter than ballot cards.
TARGET_CARD_MAX_H = 2450


def _find_style(*texts) -> str | None:
    """Return the most complete style match across the texts.

    The code prints as e.g. "4303-1-WS"; OCR sometimes drops the letter suffix.
    Prefer matches that carry a suffix and, among those, the longest, so the
    same physical layout maps to one stable cache key.
    """
    found = []
    for t in texts:
        found += STYLE_RE.findall(t or "")
    if not found:
        return None
    with_suffix = [s for s in found if any(c.isalpha() for c in s)]
    pool = with_suffix or found
    return max(pool, key=len)


def classify(path: str) -> dict:
    """Return {page_kind, party, style, width, height, status}. No DB I/O."""
    img = load_gray(path)
    w, h = img.size
    res = {"width": w, "height": h, "page_kind": "unknown",
           "party": None, "style": None, "status": db.SKIP_NO_GOVERNOR}

    header = ocr_text(img.crop(_px(img, HEADER_BAND)), psm=6)
    low = header.lower()

    # Target card separator (Clear Ballot box marker).
    if "target card" in low or (h <= TARGET_CARD_MAX_H and "clear ballot" in low):
        res["page_kind"] = "target_card"
        return res

    style = _find_style(header, ocr_text(img.crop(_px(img, STYLE_BAND)), psm=7))
    res["style"] = style

    is_front = any(marker in low for marker in CONFIG.contest.front_markers)
    if not is_front:
        # No ballot-front header -> a back side or non-ballot page. No contest.
        res["page_kind"] = "ballot_back" if h > TARGET_CARD_MAX_H else "unknown"
        return res

    res["page_kind"] = "ballot_front"
    res["party"] = "other"
    for party in CONFIG.parties:
        if party.match in low:
            res["party"] = party.label
            break
    res["layout"] = layout_key(res["party"], style)
    res["status"] = db.CLASSIFIED
    return res


def _px(img, rel):
    w, h = img.size
    x0, y0, x1, y1 = rel
    return (int(x0 * w), int(y0 * h), int(x1 * w), int(y1 * h))


def main(argv=None):
    """Standalone sequential pass over pending rows (use run.py for parallel)."""
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--box", default=None, help="only classify a given box, e.g. AB-0028")
    args = ap.parse_args(argv)

    conn = db.connect()
    q = "SELECT path FROM images WHERE status = ?"
    params = [db.PENDING]
    if args.box:
        q += " AND box = ?"
        params.append(args.box)
    if args.limit:
        q += f" LIMIT {int(args.limit)}"
    rows = conn.execute(q, params).fetchall()
    print(f"classifying {len(rows)} pages")
    for i, r in enumerate(rows, 1):
        path = r["path"]
        try:
            res = classify(path)
            db.set_status(conn, path, res.pop("status"), **res)
        except Exception as e:  # noqa: BLE001
            db.set_status(conn, path, db.ERROR, err=f"classify: {e}")
        if i % 50 == 0:
            conn.commit()
            print(f"  {i}/{len(rows)}")
    conn.commit()
    print("status:", db.counts_by_status(conn))


if __name__ == "__main__":
    main()
