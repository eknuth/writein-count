"""Guardrail: reconcile our detected write-ins against official county totals.

The pipeline has no inherent notion of ground truth -- it counts whatever reached
`done` and reports confidently, so a coverage hole or a systematic locate failure
can pass silently (and once did: two opposite errors that averaged into a
plausible total). This compares our count of detected filled write-in ovals, per
contest, against the official county aggregate and WARNS on large divergence.

Officials count a write-in when the oval is filled, so the comparison metric is
the in-scope `done` rows the vision model judged oval-filled. Warnings are loud
but non-fatal: the operator decides, but the divergence can no longer hide.
"""
from __future__ import annotations

import json
import os

from . import db

OFFICIAL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "official_results.json")
WARN_THRESHOLD = 0.15  # |ours - official| / official above this -> warn


def detected_filled(conn) -> dict:
    """In-scope filled write-in ovals per party (matches the official metric)."""
    rows = conn.execute(
        "SELECT i.party, COUNT(*) n FROM images i JOIN results r ON r.path=i.path "
        "WHERE i.party IN ('Democratic','Republican') AND i.status='done' "
        "AND r.vision_oval='filled' GROUP BY i.party").fetchall()
    return {r["party"]: r["n"] for r in rows}


def check(conn, threshold: float = WARN_THRESHOLD) -> bool:
    """Print a reconciliation table; return True if every contest is within
    threshold of the official aggregate, False (with warnings) otherwise."""
    if not os.path.exists(OFFICIAL):
        print(f"[reconcile] no official baseline at {OFFICIAL}; skipping")
        return True
    ref = json.load(open(OFFICIAL))
    contests = ref.get("contests", {})
    ours = detected_filled(conn)

    print("=" * 60)
    print("RECONCILE vs official write-in totals")
    print(f"  source: {ref.get('source','?')} ({ref.get('status','?')}, as of {ref.get('as_of','?')})")
    print(f"  metric: in-scope filled write-in ovals; warn if |delta| > {threshold:.0%}")
    print("-" * 60)
    ok = True
    for party, info in contests.items():
        official = info.get("writein_total")
        got = ours.get(party, 0)
        if not official:
            continue
        delta = (got - official) / official
        flag = "" if abs(delta) <= threshold else "  <-- WARN"
        if flag:
            ok = False
        print(f"  {party:<12} ours={got:>6}  official={official:>6}  "
              f"delta={delta:+6.1%}{flag}")
    print("-" * 60)
    if ok:
        print("  OK: all contests within threshold.")
    else:
        print("  WARNING: a contest diverges from the official total. Check for")
        print("  un-ingested boxes (run stage0_manifest) or a locate/mark regression")
        print("  before trusting these counts.")
    print("=" * 60)
    return ok


def main(argv=None):
    check(db.connect())


if __name__ == "__main__":
    main()
