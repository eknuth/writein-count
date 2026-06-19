"""Stage 4: read the handwritten write-in with a local vision model.

Runs only on the marked subset from stage 3, against the local Ollama server
(model and endpoint from config; default `gemma4:12b`, vision-capable). The model
transcribes the handwriting and reports the oval state; we store the raw text
verbatim so every decision stays auditable.
"""
from __future__ import annotations

import base64
import json
import urllib.request

from . import db
from .config import CONFIG

OLLAMA_URL = CONFIG.vision.url
MODEL = CONFIG.vision.model
PROMPT = CONFIG.vision_prompt()


def read_crop(crop_path: str, timeout: float = 180.0) -> dict:
    """Call the local vision model on a crop; return {text, oval, conf}."""
    with open(crop_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    payload = {
        "model": MODEL,
        "prompt": PROMPT,
        "images": [b64],
        "stream": False,
        "format": "json",
        "options": {"temperature": CONFIG.vision.temperature},
    }
    req = urllib.request.Request(
        OLLAMA_URL, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    raw = body.get("response", "").strip()
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        # Model didn't return clean JSON; keep raw text for review.
        return {"text": raw, "oval": "unsure", "conf": 0.0}
    return {
        "text": str(obj.get("text", "")).strip(),
        "oval": str(obj.get("oval", "unsure")).strip().lower(),
        "conf": float(obj.get("confidence", 0.0) or 0.0),
    }


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--box", default=None)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args(argv)

    conn = db.connect()
    q = ("SELECT i.path, r.crop_path FROM images i JOIN results r ON r.path = i.path "
         "WHERE i.status = ?")
    params = [db.MARKED]
    if args.box:
        q += " AND i.box = ?"
        params.append(args.box)
    if args.limit:
        q += f" LIMIT {int(args.limit)}"
    rows = conn.execute(q, params).fetchall()
    print(f"vision-reading {len(rows)} marked write-ins with {MODEL}")
    for i, r in enumerate(rows, 1):
        path, crop_path = r["path"], r["crop_path"]
        try:
            out = read_crop(crop_path)
            db.upsert_result(conn, path, vision_text=out["text"], vision_oval=out["oval"],
                             vision_conf=out["conf"], vision_model=MODEL)
            db.set_status(conn, path, db.READ)
        except Exception as e:  # noqa: BLE001
            db.set_status(conn, path, db.ERROR, err=f"read: {e}")
        conn.commit()
        if i % 10 == 0:
            print(f"  {i}/{len(rows)}")
    print("status:", db.counts_by_status(conn))


if __name__ == "__main__":
    main()
