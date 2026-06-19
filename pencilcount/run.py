"""Orchestrator: runs the funnel in parallel, resumable, idempotent.

Phases (each pulls rows by status, so safe to re-run as scans keep loading):

  classify  CPU pool  -> read page kind / party / style on every pending page
  locate    serial    -> compute the write-in region ONCE per style, cache it
  mark      CPU pool  -> ink-score the write-in; gate blank vs marked
  read      bounded   -> vision-transcribe marked write-ins (local Ollama)
  match     inline    -> fuzzy-classify pencil / review / not_pencil

Workers are pure (no DB handle); the parent owns all writes, so SQLite never sees
concurrent writers. Default `all` runs every phase in order.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import multiprocessing as mp
import os

from . import db
from .common import load_gray
from .config import CONFIG
from . import stage1_classify, stage2_locate, stage3_mark, stage4_read, stage5_match

N_CPU = max(1, (os.cpu_count() or 4) - 2)
# Concurrent vision reads. Network-bound (HTTP to local Ollama), so threads, not
# processes. One 12B model is the real bottleneck, but a few in-flight requests
# overlap encode/queue/decode. Tune via env or --read-workers; Ollama must allow
# concurrency (OLLAMA_NUM_PARALLEL >= this).
READ_WORKERS = CONFIG.vision.read_workers


# ---- worker functions (module-level so they pickle) ----

def _w_classify(path):
    try:
        res = stage1_classify.classify(path)
        return ("ok", path, res)
    except Exception as e:  # noqa: BLE001
        return ("err", path, f"classify: {e}")


def _w_mark(task):
    path, region_rel, oval_rel = task
    try:
        img = load_gray(path)
        s = stage3_mark.score(img, region_rel, oval_rel)
        return ("ok", path, s["oval_fill_score"], s["line_ink_score"])
    except Exception as e:  # noqa: BLE001
        return ("err", path, str(e))


def _select(conn, status, box, limit):
    q = "SELECT path, box, seq, layout FROM images WHERE status = ?"
    params = [status]
    if box:
        q += " AND box = ?"
        params.append(box)
    if limit:
        q += f" LIMIT {int(limit)}"
    return conn.execute(q, params).fetchall()


def phase_classify(conn, box, limit, pool):
    rows = _select(conn, db.PENDING, box, limit)
    if not rows:
        return
    print(f"[classify] {len(rows)} pages on {N_CPU} workers")
    n = 0
    for kind, path, payload in pool.imap_unordered(_w_classify, [r["path"] for r in rows], chunksize=8):
        if kind == "err":
            db.set_status(conn, path, db.ERROR, err=payload)
        else:
            status = payload.pop("status")
            db.set_status(conn, path, status, **payload)
        n += 1
        if n % 500 == 0:
            conn.commit()
            print(f"  classified {n}/{len(rows)}")
    conn.commit()


LOCATE_TRIES = 6  # samples to try per layout until one validates


def phase_locate(conn, box, limit):
    """Compute each unseen layout's write-in region once, then promote in bulk.

    Tries several sample ballots per layout and keeps the first that validates
    (the 'Write-In' label is readable under the line). A single bad sample can
    mislocate, so retrying makes the cached region robust. Layouts that never
    validate keep a best-effort region but are flagged (valid=0) for review.
    """
    rows = _select(conn, db.CLASSIFIED, box, limit)
    if not rows:
        return
    samples = {}
    for r in rows:
        if r["layout"] is not None:
            samples.setdefault(r["layout"], []).append(r["path"])
    print(f"[locate] {len(samples)} distinct layouts among {len(rows)} fronts")
    invalid = []
    for layout, paths in samples.items():
        if db.get_region(conn, layout) is not None:
            continue
        best = None
        for sp in paths[:LOCATE_TRIES]:
            try:
                loc = stage2_locate.locate(load_gray(sp))
            except Exception:  # noqa: BLE001
                loc = None
            if loc is None:
                continue
            best = (loc, sp)
            if loc["valid"]:
                break
        if best is None:
            print(f"  layout {layout!r}: no governor/line -> fronts skipped")
            continue
        loc, sp = best
        db.save_region(conn, layout, loc["region_rel"], loc["oval_rel"],
                       sp, valid=int(loc["valid"]))
        if not loc["valid"]:
            invalid.append(layout)
    conn.commit()
    if invalid:
        print(f"  [WARN] {len(invalid)} layouts never validated (flagged for review): {invalid}")
    for r in rows:
        if r["layout"] is not None and db.get_region(conn, r["layout"]) is not None:
            db.set_status(conn, r["path"], db.LOCATED)
        else:
            db.set_status(conn, r["path"], db.SKIP_NO_GOVERNOR, err="no region for layout")
    conn.commit()


def phase_mark(conn, box, limit, pool):
    rows = _select(conn, db.LOCATED, box, limit)
    if not rows:
        return
    tasks = []
    for r in rows:
        reg = db.get_region(conn, r["layout"])
        if reg is None:
            db.set_status(conn, r["path"], db.ERROR, err="mark: no region")
            continue
        region_rel = (reg["x0"], reg["y0"], reg["x1"], reg["y1"])
        oval_rel = (reg["ox0"], reg["oy0"], reg["ox1"], reg["oy1"])
        tasks.append((r["path"], region_rel, oval_rel))
    print(f"[mark] scoring {len(tasks)} fronts on {N_CPU} workers")
    n = 0
    for out in pool.imap_unordered(_w_mark, tasks, chunksize=8):
        if out[0] == "err":
            db.set_status(conn, out[1], db.ERROR, err=f"mark: {out[2]}")
        else:
            _, path, oval, ink = out
            db.upsert_result(conn, path, oval_fill_score=oval, line_ink_score=ink)
            db.set_status(conn, path, db.SCORED)
        n += 1
        if n % 500 == 0:
            conn.commit()
            print(f"  scored {n}/{len(tasks)}")
    conn.commit()
    marked = stage3_mark.decide(conn, box)
    print(f"  done: {marked} write-in marks found (per-layout baseline)")


def _w_read(task):
    path, crop_path = task
    try:
        return ("ok", path, stage4_read.read_crop(crop_path))
    except Exception as e:  # noqa: BLE001
        return ("err", path, str(e))


def phase_read(conn, box, limit, workers=READ_WORKERS):
    rows = conn.execute(
        "SELECT i.path, r.crop_path FROM images i JOIN results r ON r.path=i.path "
        "WHERE i.status=?" + (" AND i.box=?" if box else "")
        + (f" LIMIT {int(limit)}" if limit else ""),
        [db.MARKED] + ([box] if box else []),
    ).fetchall()
    if not rows:
        return
    print(f"[read] {len(rows)} marked write-ins via {stage4_read.MODEL} on {workers} workers")
    tasks = [(r["path"], r["crop_path"]) for r in rows]
    n = 0
    # Workers only do the network read; the main thread owns every DB write so
    # SQLite never sees concurrent writers (same invariant as the CPU phases).
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for kind, path, payload in ex.map(_w_read, tasks):
            if kind == "err":
                db.set_status(conn, path, db.ERROR, err=f"read: {payload}")
            else:
                db.upsert_result(conn, path, vision_text=payload["text"],
                                 vision_oval=payload["oval"], vision_conf=payload["conf"],
                                 vision_model=stage4_read.MODEL)
                db.set_status(conn, path, db.READ)
            n += 1
            if n % 20 == 0:
                conn.commit()
                print(f"  read {n}/{len(rows)}")
    conn.commit()


def phase_match(conn, box, limit):
    stage5_match.main(["--box", box] if box else [])


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=["classify", "locate", "mark", "read", "match", "all"],
                    default="all", nargs="?")
    ap.add_argument("--box", default=None)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--read-workers", type=int, default=READ_WORKERS,
                    help="concurrent vision-read requests to Ollama (default %(default)s)")
    args = ap.parse_args(argv)

    db.init()
    conn = db.connect()

    if args.phase in ("classify", "mark", "all"):
        with mp.Pool(N_CPU) as pool:
            if args.phase in ("classify", "all"):
                phase_classify(conn, args.box, args.limit, pool)
            if args.phase in ("all",):
                phase_locate(conn, args.box, args.limit)
            if args.phase in ("mark", "all"):
                phase_mark(conn, args.box, args.limit, pool)
    if args.phase == "locate":
        phase_locate(conn, args.box, args.limit)
    if args.phase in ("read", "all"):
        phase_read(conn, args.box, args.limit, workers=args.read_workers)
    if args.phase in ("match", "all"):
        phase_match(conn, args.box, args.limit)

    print("status:", db.counts_by_status(conn))
    # Guardrail: a full run with no box filter is a complete tally, so reconcile
    # it against the official county aggregate and warn on large divergence.
    if args.phase in ("match", "all") and not args.box:
        from . import reconcile
        reconcile.check(conn)


if __name__ == "__main__":
    main()
