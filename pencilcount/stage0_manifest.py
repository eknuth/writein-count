"""Stage 0: enumerate ballot images into the DB as `pending`.

Idempotent and watch-safe: re-run any time (including on an interval) while the
~400 GB of scans are still loading. New files are added; existing rows are left
untouched so completed work is never redone.
"""
from __future__ import annotations

import argparse
import sys

from . import db
from .common import parse_name
from .config import CONFIG

# Only count files that have finished copying: skip partials/temp files.
PARTIAL_SUFFIXES = (".part", ".tmp", ".crdownload", ".download")
BOX_PREFIX = CONFIG.filename.box_prefix


def scan(conn, root, verbose=True) -> tuple[int, int]:
    # Per-directory os.scandir, NOT a recursive glob: while the ~400 GB is
    # actively writing, a full-tree glob is I/O-starved and crawls; scanning each
    # AB-* box dir directly stays fast (whole tree in seconds).
    import os
    added = skipped = 0
    cur = conn.cursor()
    batch = []
    boxes = sorted(d.name for d in os.scandir(root)
                   if d.is_dir() and d.name.startswith(BOX_PREFIX))
    for b in boxes:
        try:
            entries = os.scandir(os.path.join(root, b))
        except OSError:
            continue
        for e in entries:
            name = e.name
            if name.startswith(".") or name.lower().endswith(PARTIAL_SUFFIXES):
                continue
            if not name.lower().endswith((".jpg", ".jpeg")):
                continue
            box, seq = parse_name(name)
            if box is None:
                skipped += 1
                continue
            batch.append((os.path.abspath(e.path), box, seq, db.PENDING, db.now()))
            if len(batch) >= 5000:
                added += _flush(cur, batch)
                batch.clear()
    if batch:
        added += _flush(cur, batch)
    conn.commit()
    if verbose:
        print(f"manifest: +{added} new, {skipped} unparseable")
    return added, skipped


def _flush(cur, batch) -> int:
    before = cur.connection.total_changes
    cur.executemany(
        "INSERT INTO images (path, box, seq, status, updated_at) VALUES (?,?,?,?,?) "
        "ON CONFLICT(path) DO NOTHING",
        batch,
    )
    return cur.connection.total_changes - before


def coverage_report(conn, root) -> None:
    """Warn loudly if box dirs on disk were never ingested.

    This is the silent-truncation guardrail: a scan that ran while the volume was
    still copying (or pointed at the wrong root) loads only some boxes, and nothing
    downstream can tell a partial count from a complete one. Comparing disk boxes
    to ingested boxes surfaces it before the counts are trusted.
    """
    import os
    try:
        disk = {e.name for e in os.scandir(root) if e.is_dir() and e.name.startswith(BOX_PREFIX)}
    except OSError as exc:
        print(f"[WARN] cannot read images root {root!r}: {exc}")
        return
    dbx = {r[0] for r in conn.execute("SELECT DISTINCT box FROM images").fetchall()}
    missing = sorted(disk - dbx)
    print(f"coverage: {len(dbx)} boxes ingested / {len(disk)} on disk")
    if missing:
        sample = ", ".join(missing[:5]) + (" ..." if len(missing) > 5 else "")
        print(f"[WARN] {len(missing)} box(es) on disk NOT ingested -> counts are INCOMPLETE.")
        print(f"       missing: {sample}")
        print(f"       re-run the manifest, then the pipeline, to include them.")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", type=int, default=0,
                    help="if >0, rescan every N seconds (for incremental loading)")
    args = ap.parse_args(argv)

    db.init()
    conn = db.connect()
    if args.watch <= 0:
        scan(conn, db.IMAGES_ROOT)
        coverage_report(conn, db.IMAGES_ROOT)
        print("status:", db.counts_by_status(conn))
        return

    import time
    print(f"watch mode: rescanning every {args.watch}s (ctrl-c to stop)")
    try:
        while True:
            scan(conn, db.IMAGES_ROOT)
            sys.stdout.flush()
            time.sleep(args.watch)
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
