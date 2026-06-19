"""Build report_assets/manifest.json from the live DB.

Scoped to the partisan Governor contest (Democratic + Republican fronts that
reached `done`). Out-of-scope "other"-party ballots (nonpartisan/judicial races,
e.g. the Erin Lagesen judicial write-ins) are excluded from the tally so the
report reflects only the Governor write-in count. Feeds build_report_v2.py.
"""
from __future__ import annotations

import json
import os

from . import db
from .config import CONFIG

HERE = os.path.dirname(os.path.abspath(__file__))
ASSET = os.path.join(HERE, "report_assets")
OUT = os.path.join(ASSET, "manifest.json")
OFFICIAL = os.path.join(HERE, "official_results.json")
SCOPE = ("Multnomah County, Democratic & Republican primary ballots "
         "(partisan Governor contest)")
GALLERY_PER = 9  # sample crops per candidate section
TARGET = CONFIG.target.term
BOX_PREFIX = CONFIG.filename.box_prefix


def _scalar(conn, sql):
    return conn.execute(sql).fetchone()[0]


def main():
    conn = db.connect()
    status = db.counts_by_status(conn)

    # In-scope rows: Dem/Rep fronts that completed, with a result.
    base = ("FROM images i JOIN results r ON r.path=i.path "
            "WHERE i.party IN ('Democratic','Republican') AND i.status='done'")
    marked_read = _scalar(conn, f"SELECT COUNT(*) {base}")
    pencil_total = conn.execute(f"SELECT COUNT(*) {base} AND r.match=?", (TARGET,)).fetchone()[0]
    pencil_dem = conn.execute(
        "SELECT COUNT(*) FROM images i JOIN results r ON r.path=i.path "
        "WHERE i.party='Democratic' AND i.status='done' AND r.match=?", (TARGET,)).fetchone()[0]
    named = _scalar(conn, f"SELECT COUNT(*) {base} AND r.candidate IS NOT NULL AND r.candidate<>''")
    ambiguous = _scalar(conn, f"SELECT COUNT(*) {base} AND r.match='review'")
    blank = _scalar(conn, f"SELECT COUNT(*) {base} AND (r.candidate IS NULL OR r.candidate='')")
    out_of_scope = _scalar(conn, "SELECT COUNT(*) FROM images i JOIN results r ON r.path=i.path "
                                 "WHERE i.party='other' AND i.status='done'")

    # Candidate tally (in scope, unquarantined, named).
    rows = conn.execute(
        f"SELECT r.candidate, i.party, COUNT(*) n {base} "
        "AND r.candidate IS NOT NULL AND r.candidate<>'' "
        "AND COALESCE(r.quarantined,0)=0 GROUP BY r.candidate, i.party").fetchall()
    cand: dict[str, dict] = {}
    for r in rows:
        c = cand.setdefault(r["candidate"], {"name": r["candidate"], "total": 0, "by_party": {}})
        c["total"] += r["n"]
        c["by_party"][r["party"]] = c["by_party"].get(r["party"], 0) + r["n"]
    tally = sorted(cand.values(), key=lambda c: (-c["total"], c["name"]))

    summary = {
        "scope": SCOPE,
        "images_total": sum(status.values()),
        "marked_read": marked_read,
        "out_of_scope_other": out_of_scope,
        "quarantined_partisan": _scalar(conn, "SELECT COUNT(*) FROM results WHERE COALESCE(quarantined,0)=1"),
        "skip_no_governor": status.get("skip_no_governor", 0),
        "skip_blank": status.get("skip_blank", 0),
        "pencil_total": pencil_total,
        "pencil_democratic": pencil_dem,
        "other_names": named - pencil_total,
        "ambiguous": ambiguous,
        "distinct_candidates": len(tally),
        "blank_writeins": blank,
        "review_total": _scalar(conn, "SELECT COUNT(*) FROM review_queue"),
    }

    # Validation block: coverage completeness + cross-check vs official totals.
    # This is the audit trail surfaced in the report so the count is not taken on
    # faith. detected_filled per party matches how officials count a write-in (a
    # filled oval), so it is the right quantity to compare.
    boxes_ingested = _scalar(conn, "SELECT COUNT(DISTINCT box) FROM images")
    try:
        boxes_on_disk = sum(1 for e in os.scandir(db.IMAGES_ROOT)
                            if e.is_dir() and e.name.startswith(BOX_PREFIX))
    except OSError:
        boxes_on_disk = None
    filled = {r["party"]: r["n"] for r in conn.execute(
        "SELECT i.party, COUNT(*) n FROM images i JOIN results r ON r.path=i.path "
        "WHERE i.party IN ('Democratic','Republican') AND i.status='done' "
        "AND r.vision_oval='filled' GROUP BY i.party").fetchall()}
    official = json.load(open(OFFICIAL)) if os.path.exists(OFFICIAL) else {"contests": {}}
    contests = []
    for party, info in official.get("contests", {}).items():
        off = info.get("writein_total")
        got = filled.get(party, 0)
        contests.append({"party": party, "ours": got, "official": off,
                         "delta": (got - off) / off if off else None})
    validation = {
        "boxes_ingested": boxes_ingested,
        "boxes_on_disk": boxes_on_disk,
        "source": official.get("source"),
        "as_of": official.get("as_of"),
        "official_status": official.get("status"),
        "contests": contests,
    }
    # Top folded candidates (for the method note), kept in sync with the live data.
    top_folded = ", ".join(f"{c['name'].title()} {c['total']}"
                           for c in tally[:4] if c["name"] != TARGET)

    # Gallery: target first, then the next top named candidates. Absolute crop
    # paths so build_report_v2 resolves them regardless of cwd.
    gallery: dict[str, list] = {}
    names = [TARGET] + [c["name"] for c in tally if c["name"] != TARGET][:11]
    for name in names:
        items = conn.execute(
            f"SELECT i.box, i.seq, i.party, r.vision_text, r.crop_path {base} "
            "AND r.candidate=? AND r.crop_path IS NOT NULL "
            "ORDER BY r.vision_conf DESC LIMIT ?", (name, GALLERY_PER)).fetchall()
        picks = [{"box": it["box"], "seq": it["seq"], "party": it["party"],
                  "text": it["vision_text"], "crop": it["crop_path"]}
                 for it in items if it["crop_path"] and os.path.exists(it["crop_path"])]
        if picks:
            gallery[name] = picks

    json.dump({"summary": summary, "validation": validation, "top_folded": top_folded,
               "candidate_tally": tally, "gallery": gallery},
              open(OUT, "w"), indent=2)
    print("wrote", OUT)
    print("summary:", json.dumps(summary, indent=2))
    print("top tally:", [(c["name"], c["total"]) for c in tally[:6]])


if __name__ == "__main__":
    main()
