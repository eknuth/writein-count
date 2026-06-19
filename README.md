# writein-count

A configurable pipeline that tallies handwritten **write-in votes** from scanned
paper ballots, using a local vision model for transcription and a fully auditable
trail from every count back to its source image.

It was built to answer one question about Multnomah County's 2026 primary: how
many people wrote in "Pencil" for Governor? (719, as it turned out, more than any
named write-in candidate on the Democratic side.) The pipeline is parameterized,
so you can point it at a different contest, write-in term, candidate list, or
scanner by supplying a config file instead of editing code. The Pencil run ships
as the worked example.

> The Python package is named `pencilcount` after that first run. The repository
> and CLI are general; only the package name carries the history.

## How it works

Each scanned image runs through a six-stage funnel. The state of every image is a
column in SQLite, so the whole pipeline is resumable and idempotent: re-run it any
time and finished work is never redone.

```
stage 0  manifest   enumerate images into the DB as `pending`
stage 1  classify   OCR the header; keep ballot fronts, read party + style
stage 2  locate     find the contest write-in region ONCE per layout, cache it
stage 3  mark       ink-score the write-in; gate blank vs marked
stage 4  read       vision-transcribe marked write-ins (local Ollama)
stage 5  match      fuzzy-classify target / review / not-target
```

The funnel is the point. Of 406,769 images in the example run, 60% carry no
Governor contest and another 38% have a blank write-in line. Fewer than 1% (3,708)
ever reach the vision model, which is the only expensive stage.

### Why these choices

- **Cache the layout, not the page.** The write-in box sits in a different spot on
  every ballot style. Stage 2 locates it once per style and caches relative boxes
  in the `regions` table, so later pages of that style skip OCR entirely.
- **Right tool per stage.** Classification and ink-scoring are CPU-bound and run on
  a multiprocessing pool. The vision reads are network-bound calls to a local
  model and run on a thread pool. SQLite stays in WAL mode with a single-writer
  invariant (the parent process owns all writes), so the parallelism never
  corrupts state.
- **Keep the model local.** Stage 4 calls a local Ollama vision model at
  temperature 0 and asks for structured JSON. No per-call cost on hundreds of GB
  of scans, deterministic output, and every raw read is stored for audit.
- **Trust nothing without a guardrail.** Stage 5 uses fuzzy matching with
  confidence gates to absorb OCR garble ("pensil", "percil"), routes the ambiguous
  middle band to a human review queue, and quarantines regions that landed on the
  wrong text. `reconcile.py` cross-checks the total against the official county
  aggregate and warns on large divergence.

## Install

Requires Python 3.11+ (for stdlib `tomllib`).

```bash
pip install -r requirements.txt
```

Two external runtime dependencies are installed separately:

- **Tesseract** (OCR for stages 1-2): `brew install tesseract` /
  `apt-get install tesseract-ocr`
- **Ollama** with a vision model (stage 4): install from https://ollama.com, then
  `ollama pull gemma4:12b`

## Configure

Copy the example config and edit it for your election:

```bash
cp examples/pencil-multnomah-2026/config.toml writein.toml
```

The pipeline resolves its config from `$WRITEIN_CONFIG`, then `./writein.toml`,
then the bundled example, then built-in defaults. Any key you omit falls back to
its default, so a partial config is valid. The knobs you will most likely change:

- `images_root` — root of your scanned image tree
- `[contest]` — the contest name and header tokens to find
- `[[parties]]` — the parties printed on the ballot header
- `[target]` — the write-in term you are counting and its match thresholds
- `[[candidates]]` — known names to fold near-identical reads onto
- `[vision]` — the Ollama model and endpoint
- `[filename]` — your scanner's filename pattern and box-directory prefix

## Run

Point `images_root` at your scans, then run the whole funnel:

```bash
python -m pencilcount.run all
```

Or run a single phase (each pulls rows by status, so they compose):

```bash
python -m pencilcount.run classify
python -m pencilcount.run read --read-workers 6
python -m pencilcount.stage5_match --rescore   # recompute matches from stored reads
python -m pencilcount.reconcile                 # cross-check vs official totals
```

## Telemetry (optional)

The pipeline can emit OpenTelemetry metrics and traces so you can watch
performance live. It is off by default and a no-op unless you opt in.

```bash
pip install -e ".[telemetry]"          # optional deps
# point at your collector (e.g. a local SigNoz)
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
WRITEIN_TELEMETRY=1 python -m pencilcount.run all
```

Or configure it under `[telemetry]` in your config (endpoint, headers, console
output, OTLP on/off). What it captures:

- **`writein.vision.read.latency`** — per-call vision transcription latency (ms histogram)
- **`writein.stage.duration`** + **`writein.images.processed`** — wall-clock and throughput per phase
- **`writein.images.by_status`** / **`writein.match.by_result`** — the funnel and tally as live gauges
- **`writein.locate.cache`** — layout-region cache hit vs miss (the locate-once win)
- **`writein.vision.inflight`** — concurrent in-flight vision reads

Without the optional deps installed, or with `enabled = false`, none of this runs
and the pipeline behaves exactly as before.

## A note on data

This repository is **code only**. No ballot images, no database, no per-ballot
transcripts, and no rendered reports are included. Write-in votes are public
record, but the per-ballot crops and transcriptions stay local by design and are
excluded by `.gitignore`. Run the pipeline against your own scans to reproduce a
count.
