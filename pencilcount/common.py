"""Shared helpers: filename parsing, image loading, OCR, cropping."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image

from .config import CONFIG

# Scanner filename convention -> (box, seq). Default AB-0001+10011.jpg yields
# box=AB-0001, seq=10011; override via config [filename] pattern.
NAME_RE = re.compile(CONFIG.filename.pattern, re.IGNORECASE)


def parse_name(path: str | Path):
    """Return (box, seq) from a ballot image path, or (None, None)."""
    m = NAME_RE.search(Path(path).name)
    if not m:
        return None, None
    return m.group("box"), m.group("seq")


def load_gray(path: str | Path) -> Image.Image:
    """Open as 8-bit grayscale."""
    return Image.open(path).convert("L")


def crop_rel(img: Image.Image, rel) -> Image.Image:
    """Crop using relative (fraction-of-size) bbox (x0,y0,x1,y1)."""
    w, h = img.size
    x0, y0, x1, y1 = rel
    return img.crop((int(x0 * w), int(y0 * h), int(x1 * w), int(y1 * h)))


def dark_ratio(img: Image.Image, threshold: int = 128) -> float:
    """Fraction of pixels darker than `threshold` (ink density)."""
    a = np.asarray(img, dtype=np.uint8)
    if a.size == 0:
        return 0.0
    return float((a < threshold).mean())


# ---- Tesseract CLI wrappers (no python bindings available) ----

def ocr_text(img: Image.Image, psm: int = 6, extra: list[str] | None = None) -> str:
    """Run tesseract on a PIL image, return plain text."""
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".png", delete=True) as tf:
        img.save(tf.name)
        cmd = ["tesseract", tf.name, "stdout", "--psm", str(psm)]
        if extra:
            cmd += extra
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    return out.stdout


def ocr_tsv(img: Image.Image, psm: int = 3) -> list[dict]:
    """Run tesseract in TSV mode; return word boxes as dicts with
    text, left, top, width, height, conf (pixel coords in `img`)."""
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".png", delete=True) as tf:
        img.save(tf.name)
        out = subprocess.run(
            ["tesseract", tf.name, "stdout", "--psm", str(psm), "tsv"],
            capture_output=True, text=True, timeout=180,
        ).stdout
    rows = []
    lines = out.splitlines()
    if not lines:
        return rows
    header = lines[0].split("\t")
    idx = {h: i for i, h in enumerate(header)}
    for ln in lines[1:]:
        parts = ln.split("\t")
        if len(parts) != len(header):
            continue
        text = parts[idx["text"]].strip()
        if not text:
            continue
        try:
            rows.append({
                "text": text,
                "left": int(parts[idx["left"]]),
                "top": int(parts[idx["top"]]),
                "width": int(parts[idx["width"]]),
                "height": int(parts[idx["height"]]),
                "conf": float(parts[idx["conf"]]),
            })
        except (ValueError, KeyError):
            continue
    return rows
