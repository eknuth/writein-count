"""SQLite-backed state for the Pencil governor write-in tally.

One row per image. The `status` column is a state machine that lets every stage
be idempotent and the whole pipeline resumable while 400 GB of scans load:

    pending -> classified -> located -> marked -> read -> done
    (terminal early-exits: skip_no_governor, skip_blank, error)

Everything a count depends on is persisted here so any number is auditable back
to a specific ballot image (box + sequence parsed from the filename).
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from .config import CONFIG

# Project root = parent of this package dir. Holds the code, DB, and crops.
ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "pencilcount" / "tally.db"
CROP_DIR = ROOT / "pencilcount" / "crops"

# Ballot image tree (the scanned JPGs in box dirs). Kept separate from ROOT
# because the ~400 GB of scans live on an external volume, not in the repo.
# Set via config (images_root) or the PENCIL_IMAGES_ROOT / WRITEIN_IMAGES_ROOT env.
IMAGES_ROOT = CONFIG.images_root

# Status values
PENDING = "pending"
CLASSIFIED = "classified"
LOCATED = "located"
SCORED = "scored"          # ink-scored; awaiting per-layout mark decision
MARKED = "marked"
READ = "read"
DONE = "done"
SKIP_NO_GOVERNOR = "skip_no_governor"  # target card, back side, or no governor race
SKIP_BLANK = "skip_blank"              # governor write-in present but empty
ERROR = "error"

SCHEMA = """
CREATE TABLE IF NOT EXISTS images (
    path        TEXT PRIMARY KEY,   -- absolute path to the jpg
    box         TEXT NOT NULL,      -- e.g. AB-0001  (Clear Ballot box / traceability key)
    seq         TEXT NOT NULL,      -- e.g. 10011    (sequence within box)
    status      TEXT NOT NULL,
    page_kind   TEXT,               -- target_card | ballot_front | ballot_back | unknown
    party       TEXT,               -- Democratic | Republican | other | NULL
    style       TEXT,               -- ballot style code e.g. 4303-1-WS (for reporting)
    layout      TEXT,               -- region cache key: "<party>|<numeric prefix>"
    width       INTEGER,
    height      INTEGER,
    updated_at  REAL,
    err         TEXT
);
CREATE INDEX IF NOT EXISTS idx_images_status ON images(status);
CREATE INDEX IF NOT EXISTS idx_images_layout ON images(layout);

CREATE TABLE IF NOT EXISTS regions (
    layout           TEXT PRIMARY KEY,   -- "<party>|<numeric prefix>"
    -- relative bbox (fractions of page w/h) of the governor write-in line region:
    x0 REAL, y0 REAL, x1 REAL, y1 REAL,
    -- relative bbox of the oval sub-region within the page (for fill scoring):
    ox0 REAL, oy0 REAL, ox1 REAL, oy1 REAL,
    calibrated_from  TEXT,    -- image path the region was derived from
    method           TEXT,    -- ocr_auto | manual
    valid            INTEGER, -- 1 if region validated (Write-In label present), else 0
    updated_at       REAL
);

CREATE TABLE IF NOT EXISTS results (
    path            TEXT PRIMARY KEY,
    oval_fill_score REAL,
    line_ink_score  REAL,
    has_mark        INTEGER,   -- 0/1
    vision_text     TEXT,      -- raw handwriting transcription
    vision_oval     TEXT,      -- model's read of oval state: filled|empty|unsure
    vision_conf     REAL,      -- 0..1
    vision_model    TEXT,
    candidate       TEXT,      -- normalized write-in name (any candidate), "" if blank
    match           TEXT,      -- pencil | not_pencil | review
    match_score     REAL,      -- fuzzy similarity to "pencil"
    crop_path       TEXT,
    updated_at      REAL,
    FOREIGN KEY(path) REFERENCES images(path)
);

CREATE TABLE IF NOT EXISTS review_queue (
    path         TEXT PRIMARY KEY,
    reason       TEXT,
    vision_text  TEXT,
    match_score  REAL,
    crop_path    TEXT,
    updated_at   REAL
);
"""


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=60.0)
    conn.row_factory = sqlite3.Row
    # WAL lets the multiprocessing workers and a watcher coexist.
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=60000;")
    # Apply lightweight column migrations on an existing DB (no-op on a fresh one).
    if conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='results'").fetchone():
        migrate(conn)
        conn.commit()
    return conn


def init(db_path: Path = DB_PATH) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        migrate(conn)
        conn.commit()
    finally:
        conn.close()
    CROP_DIR.mkdir(parents=True, exist_ok=True)


def migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a DB was first created (idempotent)."""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(results)")}
    if "candidate" not in have:
        conn.execute("ALTER TABLE results ADD COLUMN candidate TEXT")
    if "quarantined" not in have:
        # 1 => read excluded from the tally (region judged mislocated). 0/NULL => counted.
        conn.execute("ALTER TABLE results ADD COLUMN quarantined INTEGER DEFAULT 0")


def now() -> float:
    return time.time()


def set_status(conn: sqlite3.Connection, path: str, status: str, **fields) -> None:
    """Update an image row's status plus any extra columns."""
    cols = ["status = ?", "updated_at = ?"]
    vals = [status, now()]
    for k, v in fields.items():
        cols.append(f"{k} = ?")
        vals.append(v)
    vals.append(path)
    conn.execute(f"UPDATE images SET {', '.join(cols)} WHERE path = ?", vals)


def upsert_result(conn: sqlite3.Connection, path: str, **fields) -> None:
    fields["path"] = path
    fields["updated_at"] = now()
    keys = list(fields.keys())
    placeholders = ", ".join("?" for _ in keys)
    updates = ", ".join(f"{k}=excluded.{k}" for k in keys if k != "path")
    conn.execute(
        f"INSERT INTO results ({', '.join(keys)}) VALUES ({placeholders}) "
        f"ON CONFLICT(path) DO UPDATE SET {updates}",
        [fields[k] for k in keys],
    )


def get_region(conn: sqlite3.Connection, layout: str):
    if not layout:
        return None
    return conn.execute("SELECT * FROM regions WHERE layout = ?", (layout,)).fetchone()


def save_region(conn: sqlite3.Connection, layout: str, rel, oval_rel,
                calibrated_from: str, valid: int = 1, method: str = "ocr_auto") -> None:
    x0, y0, x1, y1 = rel
    ox0, oy0, ox1, oy1 = oval_rel
    conn.execute(
        "INSERT INTO regions (layout,x0,y0,x1,y1,ox0,oy0,ox1,oy1,calibrated_from,method,valid,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(layout) DO UPDATE SET "
        "x0=excluded.x0,y0=excluded.y0,x1=excluded.x1,y1=excluded.y1,"
        "ox0=excluded.ox0,oy0=excluded.oy0,ox1=excluded.ox1,oy1=excluded.oy1,"
        "calibrated_from=excluded.calibrated_from,method=excluded.method,"
        "valid=excluded.valid,updated_at=excluded.updated_at",
        (layout, x0, y0, x1, y1, ox0, oy0, ox1, oy1, calibrated_from, method, valid, now()),
    )


def add_review(conn: sqlite3.Connection, path: str, reason: str, vision_text: str,
               match_score: float, crop_path: str) -> None:
    conn.execute(
        "INSERT INTO review_queue (path,reason,vision_text,match_score,crop_path,updated_at) "
        "VALUES (?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET "
        "reason=excluded.reason,vision_text=excluded.vision_text,"
        "match_score=excluded.match_score,crop_path=excluded.crop_path,updated_at=excluded.updated_at",
        (path, reason, vision_text, match_score, crop_path, now()),
    )


def remove_review(conn: sqlite3.Connection, path: str) -> None:
    conn.execute("DELETE FROM review_queue WHERE path = ?", (path,))


def counts_by_status(conn: sqlite3.Connection) -> dict:
    rows = conn.execute("SELECT status, COUNT(*) c FROM images GROUP BY status").fetchall()
    return {r["status"]: r["c"] for r in rows}


if __name__ == "__main__":
    init()
    conn = connect()
    print("Initialized", DB_PATH)
    print("status counts:", counts_by_status(conn))
