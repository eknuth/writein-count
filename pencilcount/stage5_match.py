"""Stage 5: decide whether a transcribed write-in means "Pencil".

Fuzzy match (handwriting + OCR both introduce noise) with a human review queue
for the ambiguous middle band. Per the user's decision, *any* "Pencil" written
counts regardless of oval state, so the oval is recorded but not required.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

from . import db
from .config import CONFIG

# The write-in term being counted and its match thresholds (see config.py).
TARGET = CONFIG.target.term
STRICT = CONFIG.target.strict          # >= -> target hit
REVIEW_LOW = CONFIG.target.review_low  # [REVIEW_LOW, STRICT) -> review
LOW_CONF = CONFIG.target.low_conf      # vision confidence below this -> review

# Canonical candidates to fold near-identical reads onto, so OCR/handwriting
# noise doesn't fragment one person into dozens of spellings. Each entry maps a
# display name to fold-on aliases: the full name plus the surname (the surname
# catches partial/garbled reads like "c drazen" or "christine dra"). Configured
# per election in config.py / writein.toml [[candidates]].
KNOWN_CANDIDATES = [{"name": c.name, "aliases": list(c.aliases)} for c in CONFIG.candidates]
CANON_STRICT = CONFIG.target.canon_strict  # whole-string fuzzy threshold to fold onto a known name
SURNAME_FUZZ = CONFIG.target.surname_fuzz  # per-token fuzzy threshold against a surname alias
MIN_ALIAS = CONFIG.target.min_alias        # only fuzzy/substring-match aliases at least this long

# Canonical display names a read can be folded onto (a recognized candidate).
KNOWN_NAMES = CONFIG.known_names

# Protest/none votes ("not kotek", "no kotek", "anyone but tina") must NOT fold
# onto the named candidate — they're the opposite of a vote for them.
_NEGATIONS = {"not", "no", "never", "anti", "non", "none", "nope", "anyone", "but"}

_norm_re = re.compile(r"[^a-z ]+")
_ws_re = re.compile(r"\s+")


def normalize(text: str) -> str:
    return _ws_re.sub(" ", _norm_re.sub(" ", (text or "").lower())).strip()


def _alias_score(norm: str, alias: str, surname: bool) -> float:
    """How strongly a normalized read matches one alias of a known candidate.

    For a `surname` alias (single word of a multi-word name) we also allow
    substring and per-token fuzzy hits, so "c drazen" or "christine dra" fold in.
    For a full-name alias we use whole-string similarity only — that keeps a
    standalone name like "pencil" strict and avoids pulling in look-alikes.
    """
    s = SequenceMatcher(None, norm, alias).ratio()
    if surname and len(alias) >= MIN_ALIAS:
        if alias in norm:
            s = max(s, 0.95)
        for tok in norm.split():
            if len(tok) >= MIN_ALIAS:
                s = max(s, SequenceMatcher(None, tok, alias).ratio())
    return s


def canonicalize(text: str) -> str:
    """Normalized candidate name, folded onto a known candidate when close.

    Returns "" for a blank write-in. Unknown names pass through normalized so
    every distinct write-in value is still recorded and auditable.
    """
    norm = normalize(text)
    if not norm:
        return ""
    # Don't fold protest votes onto the candidate they name against.
    if _NEGATIONS & set(norm.split()):
        return norm
    best, best_score = norm, 0.0
    for cand in KNOWN_CANDIDATES:
        multiword = len(cand["name"].split()) > 1
        for alias in cand["aliases"]:
            is_surname = multiword and len(alias.split()) == 1
            s = _alias_score(norm, alias, is_surname)
            # Surname hits fold at the looser SURNAME_FUZZ; full names need
            # CANON_STRICT. A single-word candidate (e.g. "pencil") stays strict.
            thresh = SURNAME_FUZZ if is_surname else CANON_STRICT
            if s >= thresh and s > best_score:
                best, best_score = cand["name"], s
    return best


def _lev(a: str, b: str) -> int:
    """Levenshtein edit distance."""
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def pencil_score(text: str) -> float:
    """Best similarity of any token (and the whole string) to 'pencil'.

    A single edit away from 'pencil' (pensil, percil, fencil, rencil, penci) is
    treated as a hit: that's the dominant OCR/handwriting error and the variants
    seen in the data. Two or more edits (pencing, "pen c") stay below and route
    to review rather than being auto-counted.
    """
    norm = normalize(text)
    if not norm:
        return 0.0
    if TARGET in norm:                      # substring match (handles "the pencil")
        return 1.0
    cands = norm.split() + [norm]
    if any(len(c) >= 4 and _lev(c, TARGET) <= 1 for c in cands):
        return 0.99
    return max(SequenceMatcher(None, c, TARGET).ratio() for c in cands)


def classify_match(text: str, vision_conf: float) -> tuple[str, float]:
    score = pencil_score(text)
    if score >= STRICT and vision_conf >= LOW_CONF:
        return TARGET, score
    if score >= REVIEW_LOW or (score >= STRICT and vision_conf < LOW_CONF):
        return "review", score
    return f"not_{TARGET}", score


def quarantine_mislocated(conn, min_count=8, min_share=0.5):
    """Flag reads from mislocated regions so they can't pollute the tally.

    A genuine write-in line yields diverse handwriting. A region that landed on
    printed text (a contest header, a different race's printed candidate) yields
    the SAME transcription on every ballot of that layout. So: within any layout
    whose region never validated, if one identical non-Pencil transcription
    dominates, treat that whole layout's non-Pencil reads as mislocated and
    quarantine them (excluded from the count, routed to human review). Pencil
    reads are never quarantined, protecting the deliverable.
    """
    from collections import Counter, defaultdict
    conn.execute("UPDATE results SET quarantined=0")           # reconcile on rescore
    rows = conn.execute(
        "SELECT i.layout, i.path, r.vision_text FROM images i "
        "JOIN results r ON r.path=i.path JOIN regions reg ON reg.layout=i.layout "
        "WHERE reg.valid=0 AND r.candidate IS NOT NULL AND r.candidate<>'' "
        "AND r.match != ?", (TARGET,)).fetchall()
    by_layout = defaultdict(list)
    for r in rows:
        by_layout[r["layout"]].append(r)
    flagged, layouts = 0, []
    for layout, items in by_layout.items():
        modal_text, modal_n = Counter(normalize(it["vision_text"]) for it in items).most_common(1)[0]
        if modal_n >= min_count and modal_n >= min_share * len(items):
            layouts.append((layout, modal_text, modal_n, len(items)))
            for it in items:
                conn.execute("UPDATE results SET quarantined=1 WHERE path=?", (it["path"],))
                db.add_review(conn, it["path"], "mislocated_region", it["vision_text"], 0.0, None)
                flagged += 1
    conn.commit()
    print(f"quarantined {flagged} reads across {len(layouts)} mislocated layout(s):")
    for l, t, n, tot in layouts:
        print(f"  {l}: \"{t}\" x{n}/{tot}")
    return flagged


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--box", default=None)
    ap.add_argument("--rescore", action="store_true",
                    help="recompute candidate + match for every already-read row "
                         "(any status) from stored vision_text; does not change status")
    args = ap.parse_args(argv)

    conn = db.connect()
    if args.rescore:
        q = ("SELECT i.path, i.box, i.seq, r.vision_text, r.vision_conf, r.vision_oval, "
             "r.crop_path FROM images i JOIN results r ON r.path=i.path "
             "WHERE r.vision_text IS NOT NULL")
    else:
        q = ("SELECT i.path, i.box, i.seq, r.vision_text, r.vision_conf, r.vision_oval, "
             "r.crop_path FROM images i JOIN results r ON r.path=i.path WHERE i.status=?")
    params = [] if args.rescore else [db.READ]
    if args.box:
        q += " AND i.box = ?"
        params.append(args.box)
    rows = conn.execute(q, params).fetchall()
    print(f"matching {len(rows)} read write-ins" + (" (rescore)" if args.rescore else ""))
    counts = {"pencil": 0, "review": 0, "not_pencil": 0}
    for r in rows:
        text = r["vision_text"] or ""
        conf = r["vision_conf"] or 0.0
        match, score = classify_match(text, conf)
        candidate = canonicalize(text)
        # The target deliverable is defined by the strict matcher; keep the
        # candidate field in lockstep so the per-candidate tally can't diverge
        # from the headline count.
        if match == TARGET:
            candidate = TARGET
        counts[match] += 1
        db.upsert_result(conn, r["path"], candidate=candidate, match=match, match_score=score)
        # Review if Pencil is ambiguous OR the write-in is a non-blank name we
        # couldn't fold onto a known candidate (so a human can name it).
        unknown = bool(candidate) and candidate not in KNOWN_NAMES
        if match == "review" or unknown:
            reason = ("low_conf" if conf < LOW_CONF
                      else "unknown_candidate" if unknown
                      else "fuzzy_band")
            db.add_review(conn, r["path"], reason, text, score, r["crop_path"])
        else:
            db.remove_review(conn, r["path"])  # reconcile on rescore
        if not args.rescore:
            db.set_status(conn, r["path"], db.DONE)
    conn.commit()
    print("match results:", counts)
    quarantine_mislocated(conn)


if __name__ == "__main__":
    main()
