"""Central configuration for the write-in tally pipeline.

Everything that ties the pipeline to a specific election lives here: the contest
to find, the parties on the ballot, the write-in term being counted, the known
candidates to fold near-identical reads onto, the vision model, and the scanner's
filename convention. Point the pipeline at a different race by supplying a TOML
config instead of editing the stages.

Resolution order for the config file:
  1. $WRITEIN_CONFIG (explicit path)
  2. ./writein.toml in the current working directory
  3. the bundled example (examples/pencil-multnomah-2026/config.toml)
  4. built-in defaults below (identical to the bundled example)

Any key omitted from the TOML falls back to the default, so a partial config is
valid and a missing config reproduces the original Pencil-count behavior exactly.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
EXAMPLE_CONFIG = PKG_DIR.parent / "examples" / "pencil-multnomah-2026" / "config.toml"

DEFAULT_PROMPT = (
    "This image is a cropped 'write-in' line from the {contest} contest on a paper "
    "ballot. A voter may have hand-written a candidate name on the line and/or "
    "filled in the oval. Transcribe ONLY the hand-written text on the write-in "
    "line. Ignore the small printed words 'Write-In' and ignore any printed "
    "candidate name that may appear above the line. If nothing is hand-written, "
    "use an empty string. Also judge whether the oval/bubble is filled.\n"
    "Respond with ONLY a JSON object: "
    '{"text": "<handwriting verbatim or empty>", '
    '"oval": "filled|empty|unsure", "confidence": <0.0-1.0>}'
)


@dataclass(frozen=True)
class ContestCfg:
    # Display name of the contest (used in the vision prompt and reports).
    name: str = "Governor"
    # Lowercased prefix of the contest header OCR token (stage 2 anchor).
    header_token: str = "govern"
    # Lowercased phrases that mark a ballot *front* (stage 1 funnel gate).
    front_markers: tuple[str, ...] = ("nominating ballot", "official primary")
    # Lowercased exact token that begins the next contest below (stage 2 bound).
    next_contest_token: str = "vote"


@dataclass(frozen=True)
class PartyCfg:
    label: str          # canonical label stored in the DB
    match: str          # lowercased substring searched for in the header OCR


@dataclass(frozen=True)
class CandidateCfg:
    name: str
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class TargetCfg:
    # The write-in term being counted (the headline deliverable).
    term: str = "pencil"
    strict: float = 0.84       # >= -> count as the target
    review_low: float = 0.60   # [review_low, strict) -> human review
    low_conf: float = 0.45     # vision confidence below this -> review
    canon_strict: float = 0.84  # whole-string fuzzy threshold to fold onto a known name
    surname_fuzz: float = 0.80  # per-token fuzzy threshold against a surname alias
    min_alias: int = 5          # only fuzzy/substring-match aliases at least this long


@dataclass(frozen=True)
class VisionCfg:
    url: str = "http://localhost:11434/api/generate"
    model: str = "gemma4:12b"
    temperature: float = 0.0
    read_workers: int = 4
    prompt: str = DEFAULT_PROMPT


@dataclass(frozen=True)
class ClassifyCfg:
    # OCR bands as page fractions (x0, y0, x1, y1).
    header_band: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 0.13)
    style_band: tuple[float, float, float, float] = (0.62, 0.0, 1.0, 0.06)
    # Ballot-style code, e.g. 4303-1-WS, and its numeric prefix (suffix dropped).
    style_regex: str = r"\b(\d{4}-\d{1,2}(?:-[A-Z]{1,3})?)\b"
    numeric_regex: str = r"(\d{4}-\d{1,2})"
    # Target/separator cards are shorter than ballot cards (pixels).
    target_card_max_h: int = 2450


@dataclass(frozen=True)
class LocateCfg:
    # Grayscale ink thresholds (0-255): general ink and the fainter printed line.
    dark: int = 110
    line_dark: int = 140
    line_gap: int = 8          # px: bridge anti-aliasing breaks in the underline
    min_line_run: int = 110    # px: min (bridged) run to count as the underline
    max_line_frac: float = 0.85  # runs longer than this fraction of column width are borders
    border_frac: float = 0.80    # a box bottom-separator run, flush to the left edge
    min_indent: int = 70         # px: the write-in underline starts right of the oval
    col_w_frac: float = 0.22     # column width as a fraction of page width
    search_frac: float = 0.34    # how far below the contest header to search for its line
    # Vision-crop offsets (px) from the detected line y / column x.
    region_top_off: int = 60
    region_bot_off: int = 26
    oval_left_off: int = 54
    oval_top_off: int = 30
    oval_right_off: int = 2
    oval_bot_off: int = 6


@dataclass(frozen=True)
class MarkCfg:
    # Margins above the per-layout blank baseline (dark-ratio units).
    oval_margin: float = 0.12
    line_margin: float = 0.07
    # Fallback absolute thresholds when a layout has too few samples to baseline.
    oval_abs: float = 0.18
    line_abs: float = 0.11
    min_samples: int = 8
    baseline_pctl: int = 30
    # Handwriting band height above the printed underline (px); kept tight so the
    # printed candidate name above the line does not leak in.
    hand_band_top: int = 32
    hand_band_bot: int = 4


@dataclass(frozen=True)
class TelemetryCfg:
    # Off by default; the pipeline runs identically with telemetry disabled or
    # opentelemetry not installed. Flip on via config or WRITEIN_TELEMETRY=1.
    enabled: bool = False
    service_name: str = "writein-count"
    otlp: bool = True          # export over OTLP/HTTP (SigNoz, collectors, etc.)
    console: bool = False      # also print metrics/spans to stdout
    endpoint: str = ""         # base OTLP URL; falls back to OTEL_EXPORTER_OTLP_ENDPOINT
    headers: str = ""          # "k=v,k2=v2"; falls back to OTEL_EXPORTER_OTLP_HEADERS
    export_interval_ms: int = 5000


@dataclass(frozen=True)
class FilenameCfg:
    # Regex with named groups `box` and `seq` matched against the image filename.
    pattern: str = r"(?P<box>AB-\d+)\+(?P<seq>\d+)\.jpe?g$"
    # Box-directory prefix scanned under images_root (stage 0).
    box_prefix: str = "AB-"


def _default_parties() -> list[PartyCfg]:
    return [PartyCfg("Democratic", "democratic"), PartyCfg("Republican", "republican")]


def _default_candidates() -> list[CandidateCfg]:
    return [
        CandidateCfg("pencil", ("pencil",)),
        CandidateCfg("christine drazan", ("christine drazan", "drazan")),
        CandidateCfg("chris dudley", ("chris dudley", "dudley")),
        CandidateCfg("ed diehl", ("ed diehl", "diehl")),
        CandidateCfg("tina kotek", ("tina kotek", "kotek")),
        CandidateCfg("nick kristof", ("nick kristof", "nicholas kristof", "kristof")),
        CandidateCfg("erin lagesen", ("erin lagesen", "erin c lagesen", "lagesen")),
        CandidateCfg("dan rayfield", ("dan rayfield", "rayfield")),
        CandidateCfg("betsy johnson", ("betsy johnson",)),
    ]


@dataclass(frozen=True)
class Config:
    images_root: Path = Path("/Volumes/storage/pencil/BallotImages20260519Primary")
    contest: ContestCfg = field(default_factory=ContestCfg)
    parties: list[PartyCfg] = field(default_factory=_default_parties)
    candidates: list[CandidateCfg] = field(default_factory=_default_candidates)
    target: TargetCfg = field(default_factory=TargetCfg)
    vision: VisionCfg = field(default_factory=VisionCfg)
    classify: ClassifyCfg = field(default_factory=ClassifyCfg)
    locate: LocateCfg = field(default_factory=LocateCfg)
    mark: MarkCfg = field(default_factory=MarkCfg)
    telemetry: TelemetryCfg = field(default_factory=TelemetryCfg)
    filename: FilenameCfg = field(default_factory=FilenameCfg)

    @property
    def known_names(self) -> set[str]:
        return {c.name for c in self.candidates}

    def vision_prompt(self) -> str:
        """The read prompt with the contest name substituted in."""
        return self.vision.prompt.replace("{contest}", self.contest.name)


def _config_path() -> Path | None:
    env = os.environ.get("WRITEIN_CONFIG")
    if env:
        return Path(env)
    local = Path.cwd() / "writein.toml"
    if local.is_file():
        return local
    if EXAMPLE_CONFIG.is_file():
        return EXAMPLE_CONFIG
    return None


def load_config(path: Path | None = None) -> Config:
    """Build a Config from TOML, falling back to built-in defaults per key."""
    cfg = Config()
    path = path or _config_path()
    data: dict = {}
    if path is not None and Path(path).is_file():
        with open(path, "rb") as f:
            data = tomllib.load(f)

    # images_root: TOML, then the legacy env override, then default.
    images_root = data.get("images_root", str(cfg.images_root))
    images_root = os.environ.get("PENCIL_IMAGES_ROOT") \
        or os.environ.get("WRITEIN_IMAGES_ROOT") or images_root

    def sub(section: str, base):
        d = data.get(section)
        return replace(base, **{k: v for k, v in d.items()
                                if k in base.__dataclass_fields__}) if isinstance(d, dict) else base

    contest = sub("contest", cfg.contest)
    if isinstance(contest.front_markers, list):
        contest = replace(contest, front_markers=tuple(contest.front_markers))

    parties = [PartyCfg(p["label"], p["match"]) for p in data["parties"]] \
        if isinstance(data.get("parties"), list) else cfg.parties
    candidates = [CandidateCfg(c["name"], tuple(c["aliases"])) for c in data["candidates"]] \
        if isinstance(data.get("candidates"), list) else cfg.candidates

    vision = sub("vision", cfg.vision)
    # read_workers env override keeps the original PENCIL_READ_WORKERS knob.
    rw = os.environ.get("PENCIL_READ_WORKERS") or os.environ.get("WRITEIN_READ_WORKERS")
    if rw:
        vision = replace(vision, read_workers=int(rw))

    # OCR bands arrive from TOML as lists; the stages expect 4-tuples.
    classify = sub("classify", cfg.classify)
    classify = replace(classify, header_band=tuple(classify.header_band),
                       style_band=tuple(classify.style_band))

    telemetry = sub("telemetry", cfg.telemetry)
    if os.environ.get("WRITEIN_TELEMETRY", "").lower() in ("1", "true", "yes"):
        telemetry = replace(telemetry, enabled=True)

    return Config(
        images_root=Path(images_root),
        contest=contest,
        parties=parties,
        candidates=candidates,
        target=sub("target", cfg.target),
        vision=vision,
        classify=classify,
        locate=sub("locate", cfg.locate),
        mark=sub("mark", cfg.mark),
        telemetry=telemetry,
        filename=sub("filename", cfg.filename),
    )


# Module-level singleton: load once, import everywhere.
CONFIG = load_config()
