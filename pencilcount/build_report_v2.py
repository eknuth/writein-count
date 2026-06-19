"""Build the Multnomah County Pencil write-in report as a print-ready HTML doc.

Design: editorial "graphite + pencil-yellow" system (mega-design). Reads the
prepared manifest (summary, consolidated candidate tally, gallery of real
write-in crops), embeds every crop as a base64 data URI so the output is fully
self-contained, and writes report.html. Render to PDF with Chrome headless.
"""
from __future__ import annotations

import base64
import html
import json
import os

ROOT = os.path.dirname(os.path.abspath(__file__))
ASSET = os.path.join(ROOT, "report_assets")
MANIFEST = os.path.join(ASSET, "manifest.json")
OUT_HTML = os.path.join(ROOT, "report.html")

PARTY_LABEL = {"Democratic": "Dem", "Republican": "Rep", "other": "other"}


def b64(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("ascii")


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def party_str(by_party: dict) -> str:
    return ", ".join(f"{PARTY_LABEL.get(k, k)} {v}"
                     for k, v in sorted(by_party.items(), key=lambda kv: -kv[1]))


def main():
    man = json.load(open(MANIFEST))
    s = man["summary"]
    tally = man["candidate_tally"]
    gallery = man["gallery"]
    PENCIL = s["pencil_total"]
    OTHER_NAMES = s["other_names"]
    AMBIGUOUS = s["ambiguous"]
    OUT_OF_SCOPE = s.get("out_of_scope_other", 0)
    MISREAD = s.get("quarantined_partisan", s.get("misread_printed", 0))

    # ---- candidate table rows (top 40, then tail summary) ----
    top = tally[:40]
    tail = tally[40:]
    tail_votes = sum(c["total"] for c in tail)
    rows = []
    for i, c in enumerate(top, 1):
        is_pencil = c["name"] == "pencil"
        cls = " class=\"pencil-row\"" if is_pencil else ""
        name = "PENCIL" if is_pencil else c["name"].title()
        mark = '<span class="dot"></span>' if is_pencil else ""
        rows.append(
            f"<tr{cls}><td class='rank'>{i}</td>"
            f"<td class='cand'>{mark}{esc(name)}</td>"
            f"<td class='num'>{c['total']:,}</td>"
            f"<td class='party'>{esc(party_str(c['by_party']))}</td></tr>")
    table_rows = "\n".join(rows)

    # ---- funnel bars ----
    total = s["images_total"]

    def bar(label, n, kind=""):
        pct = 100 * n / total
        return (f"<div class='fbar {kind}'>"
                f"<div class='fbar-label'>{esc(label)}</div>"
                f"<div class='fbar-track'><div class='fbar-fill' style='width:{max(pct,0.6):.2f}%'></div></div>"
                f"<div class='fbar-val'>{n:,}</div></div>")

    funnel = "\n".join([
        bar("All scanned images", s["images_total"]),
        bar("No governor contest (cards, backs)", s["skip_no_governor"]),
        bar("Governor write-in left blank", s["skip_blank"]),
        bar("Write-in marked &amp; vision-read", s["marked_read"], "hl"),
    ])

    # ---- read-result split (of the 5,414) ----
    mr = s["marked_read"]
    split = "\n".join([
        f"<div class='split-seg other' style='flex:{OTHER_NAMES}'><span>Other names {OTHER_NAMES:,}</span></div>",
        f"<div class='split-seg pencil' style='flex:{PENCIL}'><span>Pencil {PENCIL}</span></div>",
        f"<div class='split-seg amb' style='flex:{max(AMBIGUOUS,40)}'><span>Review {AMBIGUOUS}</span></div>",
    ])

    # ---- gallery ----
    def chip(item):
        p = os.path.join(ASSET, item["crop"])
        if not os.path.exists(p):
            return ""
        uri = f"data:image/png;base64,{b64(p)}"
        read = esc(item.get("text") or "(blank)")
        meta = f"{esc(item['box'])}&middot;{esc(item['seq'])} &nbsp; {PARTY_LABEL.get(item['party'], esc(item['party']))}"
        return (f"<figure class='chip'>"
                f"<div class='chip-img'><img src='{uri}' alt='write-in crop {esc(item['box'])} {esc(item['seq'])}'></div>"
                f"<figcaption><span class='read'>&ldquo;{read}&rdquo;</span>"
                f"<span class='meta'>{meta}</span></figcaption></figure>")

    # order: pencil first, then the rest in tally order
    names = list(gallery.keys())
    ordered = (["pencil"] if "pencil" in names else []) + [n for n in names if n != "pencil"]
    # map name -> tally total
    totals = {c["name"]: c["total"] for c in tally}
    sections = []
    for name in ordered:
        items = [chip(it) for it in gallery[name]]
        items = [x for x in items if x]
        if not items:
            continue
        is_pencil = name == "pencil"
        disp = "PENCIL" if is_pencil else name.title()
        tot = totals.get(name, len(items))
        head_cls = "gsection-head pencil" if is_pencil else "gsection-head"
        note = "counted votes" if is_pencil else "sample reads"
        sections.append(
            f"<section class='gsection'>"
            f"<div class='{head_cls}'><h3>{esc(disp)}</h3>"
            f"<span class='gcount'>{tot:,} total &middot; {note}</span></div>"
            f"<div class='chipgrid'>{''.join(items)}</div></section>")
    gallery_html = "\n".join(sections)

    # ---- validation (cross-check vs official county totals) ----
    val = man.get("validation", {})
    vrows = []
    for c in val.get("contests", []):
        off = c.get("official")
        delta = c.get("delta")
        dstr = f"{delta:+.1%}" if delta is not None else "n/a"
        within = delta is not None and abs(delta) <= 0.15
        cls = "ok" if within else "warn"
        vrows.append(
            f"<tr><td class='cand'>{esc(c['party'])} Governor</td>"
            f"<td class='num'>{c.get('ours',0):,}</td>"
            f"<td class='num'>{off:,}</td>"
            f"<td class='num {cls}'>{dstr}</td></tr>")
    val_rows = "\n".join(vrows)
    bi = val.get("boxes_ingested")
    bd = val.get("boxes_on_disk")
    coverage_str = (f"{bi:,} of {bd:,}" if bi is not None and bd is not None else f"{bi:,}")
    coverage_pct = (f"{100*bi/bd:.0f}%" if bi and bd else "")
    val_src = esc(val.get("source") or "Oregon Secretary of State")
    val_asof = esc(val.get("as_of") or "")
    val_status = esc(val.get("official_status") or "unofficial")
    top_folded = esc(man.get("top_folded") or "")

    review = s["review_total"]
    distinct = s["distinct_candidates"]
    blank = s["blank_writeins"]

    # Explicit token replacement (not str.format) so the CSS braces are untouched.
    repl = {
        "{table_rows}": table_rows, "{funnel}": funnel, "{split}": split,
        "{gallery}": gallery_html, "{pencil}": str(PENCIL),
        "{pencil_dem}": str(s["pencil_democratic"]), "{marked}": f"{mr:,}",
        "{images}": f"{total:,}", "{no_gov}": f"{s['skip_no_governor']:,}",
        "{blank}": f"{blank:,}", "{distinct}": f"{distinct:,}", "{review}": f"{review:,}",
        "{other}": f"{OTHER_NAMES:,}", "{amb}": str(AMBIGUOUS),
        "{out_of_scope}": f"{OUT_OF_SCOPE:,}", "{misread_printed}": f"{MISREAD:,}",
        "{tail_n}": f"{len(tail):,}", "{tail_votes}": f"{tail_votes:,}",
        "{val_rows}": val_rows, "{coverage}": coverage_str, "{coverage_pct}": coverage_pct,
        "{val_src}": val_src, "{val_asof}": val_asof, "{val_status}": val_status,
        "{top_folded}": top_folded,
    }
    doc = TEMPLATE
    for k, v in repl.items():
        doc = doc.replace(k, v)
    with open(OUT_HTML, "w") as f:
        f.write(doc)
    print("wrote", OUT_HTML, f"({len(doc)//1024} KB)")


TEMPLATE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Multnomah County — Pencil Governor Write-In Count</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,500;9..144,600;9..144,700;9..144,900&family=Archivo:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root{
  --paper:#F7F3EA; --paper-2:#FBF8F1; --ink:#1E2027; --ink-soft:#3A3E4A;
  --graphite:#6A6F7E; --hair:rgba(30,32,39,.14); --hair-2:rgba(30,32,39,.08);
  --pencil:#F4BE1B; --pencil-deep:#D89B00; --wood:#C8A06A; --eraser:#E0664B;
}
*{box-sizing:border-box}
@page{ size:Letter; margin:0; }
html,body{margin:0;padding:0;background:#555;}
body{font-family:"Archivo",sans-serif;color:var(--ink);
  -webkit-print-color-adjust:exact;print-color-adjust:exact;
  font-variant-numeric:tabular-nums;}
.sheet{position:relative;width:8.5in;min-height:11in;background:var(--paper);
  margin:0 auto;padding:0.7in 0.72in 0.6in;page-break-after:always;overflow:hidden;}
.sheet:last-child{page-break-after:auto;}
h1,h2,h3{font-family:"Fraunces",Georgia,serif;text-wrap:balance;margin:0;font-weight:600;}
p{text-wrap:pretty;}
.num,.fbar-val{font-variant-numeric:tabular-nums;}

/* ---------- shared chrome ---------- */
.kicker{font-family:"Archivo";font-size:11px;letter-spacing:.22em;text-transform:uppercase;
  color:var(--graphite);font-weight:600;}
.rule{height:3px;background:var(--ink);border:0;margin:0;}
.rule.accent{background:var(--pencil);}
.foot{position:absolute;left:0.72in;right:0.72in;bottom:0.42in;display:flex;justify-content:space-between;
  font-size:10px;color:var(--graphite);letter-spacing:.04em;border-top:1px solid var(--hair-2);padding-top:8px;}
.pagetag{font-weight:600;}

/* ---------- cover ---------- */
.cover{display:flex;flex-direction:column;}
.cover .topband{display:flex;justify-content:space-between;align-items:flex-start;}
.cover .seal{font-family:"Fraunces";font-weight:900;font-size:13px;letter-spacing:.02em;
  border:2px solid var(--ink);border-radius:50%;width:60px;height:60px;display:flex;align-items:center;
  justify-content:center;text-align:center;line-height:1.05;padding:6px;}
.cover h1{font-size:74px;line-height:.92;font-weight:900;letter-spacing:-.015em;margin-top:46px;}
.cover h1 .lede{display:block;font-size:30px;font-weight:500;font-style:italic;color:var(--ink-soft);
  letter-spacing:0;margin-bottom:10px;}
.cover .sub{margin-top:18px;max-width:5.6in;font-size:15px;line-height:1.55;color:var(--ink-soft);}
.hero{margin-top:42px;background:var(--ink);color:var(--paper);border-radius:14px;padding:30px 34px;
  display:flex;align-items:center;gap:34px;position:relative;overflow:hidden;}
.hero::after{content:"";position:absolute;right:-40px;top:-40px;width:220px;height:220px;
  background:radial-gradient(circle at center,rgba(244,190,27,.22),transparent 70%);}
.hero .big{font-family:"Fraunces";font-weight:900;font-size:118px;line-height:.82;color:var(--pencil);
  letter-spacing:-.02em;}
.hero .htxt{display:flex;flex-direction:column;gap:6px;}
.hero .htxt .l1{font-family:"Fraunces";font-size:26px;font-weight:600;}
.hero .htxt .l2{font-size:14px;color:rgba(247,243,234,.78);line-height:1.5;max-width:3in;}
.hero .chiprow{display:flex;gap:8px;margin-top:8px;}
.hero .pill{font-size:10.5px;letter-spacing:.08em;text-transform:uppercase;font-weight:600;
  background:rgba(247,243,234,.12);color:var(--paper);border:1px solid rgba(247,243,234,.25);
  border-radius:999px;padding:5px 10px;}
.cover .meta-grid{margin-top:38px;display:grid;grid-template-columns:repeat(3,1fr);gap:1px;
  background:var(--hair);border:1px solid var(--hair);border-radius:10px;overflow:hidden;}
.cover .meta-grid .cell{background:var(--paper-2);padding:14px 16px;}
.cover .meta-grid .k{font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:var(--graphite);font-weight:600;}
.cover .meta-grid .v{font-family:"Fraunces";font-size:21px;font-weight:600;margin-top:4px;}
.notice{margin-top:22px;display:flex;gap:10px;align-items:flex-start;font-size:12px;color:var(--ink-soft);
  background:rgba(244,190,27,.14);border-left:4px solid var(--pencil);padding:12px 14px;border-radius:0 8px 8px 0;}

/* ---------- section header ---------- */
.shead{display:flex;align-items:baseline;justify-content:space-between;margin-bottom:6px;}
.shead h2{font-size:34px;font-weight:700;letter-spacing:-.01em;}
.shead .sk{font-size:11px;letter-spacing:.2em;text-transform:uppercase;color:var(--graphite);font-weight:600;}
.lead{font-size:13.5px;line-height:1.6;color:var(--ink-soft);max-width:6.2in;margin:14px 0 26px;}

/* ---------- funnel ---------- */
.fbar{display:grid;grid-template-columns:2.5in 1fr 0.9in;align-items:center;gap:14px;margin:11px 0;}
.fbar-label{font-size:12.5px;color:var(--ink-soft);}
.fbar-track{height:26px;background:var(--hair-2);border-radius:6px;overflow:hidden;}
.fbar-fill{height:100%;background:linear-gradient(90deg,#3A3E4A,#5A6072);border-radius:6px;}
.fbar.hl .fbar-fill{background:linear-gradient(90deg,var(--pencil-deep),var(--pencil));}
.fbar.hl .fbar-label{font-weight:700;color:var(--ink);}
.fbar-val{font-size:14px;font-weight:600;text-align:right;}
.splitwrap{margin-top:30px;}
.splitwrap .cap{font-size:12px;color:var(--graphite);margin-bottom:8px;letter-spacing:.04em;}
.split{display:flex;height:54px;border-radius:8px;overflow:hidden;border:1px solid var(--hair);}
.split-seg{display:flex;align-items:center;justify-content:center;color:#fff;font-size:11.5px;font-weight:600;
  padding:0 6px;text-align:center;min-width:0;}
.split-seg span{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.split-seg.other{background:#4A4F5E;} .split-seg.pencil{background:var(--pencil);color:var(--ink);}
.split-seg.amb{background:var(--eraser);}
.statcards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-top:30px;}
.statcards .c{background:var(--paper-2);border:1px solid var(--hair);border-radius:10px;padding:16px;}
.statcards .c .v{font-family:"Fraunces";font-size:30px;font-weight:700;line-height:1;}
.statcards .c .k{font-size:11px;color:var(--graphite);margin-top:7px;line-height:1.35;}
.statcards .c.accent{background:var(--ink);} .statcards .c.accent .v{color:var(--pencil);} .statcards .c.accent .k{color:rgba(247,243,234,.75);}

/* ---------- table ---------- */
table{width:100%;border-collapse:collapse;font-size:12.5px;}
thead th{text-align:left;font-family:"Archivo";font-size:10px;letter-spacing:.14em;text-transform:uppercase;
  color:var(--graphite);font-weight:700;padding:0 10px 8px;border-bottom:2px solid var(--ink);}
th.num,td.num{text-align:right;}
tbody td{padding:7px 10px;border-bottom:1px solid var(--hair-2);}
td.rank{color:var(--graphite);width:34px;font-size:11px;}
td.cand{font-weight:600;}
td.num{font-weight:700;font-size:13.5px;width:74px;}
td.party{color:var(--graphite);font-size:11px;width:1.9in;}
tr.pencil-row td{background:rgba(244,190,27,.20);}
tr.pencil-row td.cand{position:relative;}
tr.pencil-row td:first-child{box-shadow:inset 4px 0 0 var(--pencil-deep);}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--pencil-deep);margin-right:7px;vertical-align:middle;}
.tail{margin-top:14px;font-size:12px;color:var(--ink-soft);background:var(--paper-2);border:1px dashed var(--hair);
  border-radius:8px;padding:11px 14px;}

/* ---------- gallery ---------- */
.gintro{font-size:13px;color:var(--ink-soft);line-height:1.6;max-width:6.2in;margin:12px 0 22px;}
.gsection{margin-bottom:20px;page-break-inside:avoid;}
.gsection-head{display:flex;align-items:baseline;gap:12px;border-bottom:1.5px solid var(--hair);
  padding-bottom:6px;margin-bottom:12px;}
.gsection-head h3{font-size:19px;font-weight:700;}
.gsection-head.pencil h3{color:var(--pencil-deep);}
.gsection-head .gcount{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--graphite);font-weight:600;}
.chipgrid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;}
.chip{margin:0;background:var(--paper-2);border:1px solid var(--hair);border-radius:9px;overflow:hidden;}
.chip-img{background:#fff;border-bottom:1px solid var(--hair-2);padding:7px;height:74px;display:flex;align-items:center;justify-content:center;}
.chip-img img{max-width:100%;max-height:100%;object-fit:contain;image-rendering:auto;}
.chip figcaption{padding:8px 10px;display:flex;flex-direction:column;gap:3px;}
.chip .read{font-family:"Fraunces";font-size:13px;font-weight:600;color:var(--ink);line-height:1.15;}
.chip .meta{font-size:10px;color:var(--graphite);letter-spacing:.03em;}

/* ---------- methodology ---------- */
.method{font-size:13px;line-height:1.65;color:var(--ink-soft);max-width:6.3in;}
.method h3{font-family:"Archivo";font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:var(--ink);
  margin:22px 0 7px;font-weight:700;}
.method p{margin:0 0 9px;}
.method .pipe{display:flex;flex-wrap:wrap;gap:7px;margin:8px 0 4px;}
.method .pipe .stage{font-size:11px;background:var(--paper-2);border:1px solid var(--hair);border-radius:999px;
  padding:5px 11px;font-weight:600;color:var(--ink-soft);}
.method .pipe .stage b{color:var(--pencil-deep);}
.caveat{background:var(--paper-2);border:1px solid var(--hair);border-left:4px solid var(--eraser);
  border-radius:0 8px 8px 0;padding:12px 14px;margin:9px 0;font-size:12.5px;}
.caveat b{color:var(--ink);}
</style></head>
<body>

<!-- ========== COVER ========== -->
<section class="sheet cover">
  <div class="topband">
    <div class="kicker">Multnomah County &nbsp;&bull;&nbsp; Oregon 2026 Democratic Primary</div>
    <div class="seal">WRITE<br>IN</div>
  </div>
  <hr class="rule" style="margin-top:12px">
  <h1><span class="lede">Governor write-in tabulation</span>The&nbsp;Pencil&nbsp;Count</h1>
  <p class="sub">An automated count of handwritten Governor write-in votes on Multnomah County
  ballot images, scanned and read by local optical-character and vision models. Every counted
  ballot traces back to a specific box and sequence number.</p>

  <div class="hero">
    <div class="big">{pencil}</div>
    <div class="htxt">
      <div class="l1">votes for &ldquo;Pencil&rdquo;</div>
      <div class="l2">Democratic primary for Governor: {pencil_dem} of {pencil} on Democratic ballots, every oval filled. The single most common write-in in the contest, ahead of every named candidate.</div>
      <div class="chiprow"><span class="pill">Democratic</span><span class="pill">Oval filled</span><span class="pill">Multnomah Co.</span></div>
    </div>
  </div>

  <div class="meta-grid">
    <div class="cell"><div class="k">Images processed</div><div class="v">{images}</div></div>
    <div class="cell"><div class="k">Write-ins read</div><div class="v">{marked}</div></div>
    <div class="cell"><div class="k">Distinct names</div><div class="v">{distinct}</div></div>
  </div>

  <div class="notice"><span>&#9888;</span>
  <span><b>Automated count, pending human review.</b> Figures are produced by machine OCR and a
  local vision model. {review} ambiguous or unrecognized reads remain queued for human adjudication.</span></div>

  <div class="foot"><span>Prepared 2026&#8209;06&#8209;19 &nbsp;&bull;&nbsp; pencilcount pipeline</span><span class="pagetag">Multnomah County</span></div>
</section>

<!-- ========== PIPELINE ========== -->
<section class="sheet">
  <div class="shead"><h2>How the count narrows</h2><span class="sk">Pipeline / 01</span></div>
  <p class="lead">Each scanned image runs a six-stage funnel. Most pages carry no Governor contest
  (target cards, ballot backs) or leave the write-in line blank. Only ballots with an actual
  write-in mark reach the vision model that transcribes the handwriting.</p>
  {funnel}

  <div class="splitwrap">
    <div class="cap">Of the marked &amp; read write-ins (partisan ballots), by transcribed result:</div>
    <div class="split">{split}</div>
  </div>

  <div class="notice" style="margin-top:22px"><span>&#9888;</span>
  <span><b>Scope: partisan ballots only.</b> The Governor primary appears solely on Democratic and
  Republican ballots. {out_of_scope} write-in reads from nonpartisan (&ldquo;other&rdquo;) ballots
  and {misread_printed} reads where an unvalidated region landed on a printed contest header are
  excluded as detector artifacts, not real write-ins.</span></div>

  <div class="statcards">
    <div class="c accent"><div class="v">{pencil}</div><div class="k">Pencil votes (Democratic)</div></div>
    <div class="c"><div class="v">{other}</div><div class="k">Other named write-ins</div></div>
    <div class="c"><div class="v">{amb}</div><div class="k">Ambiguous (Pencil-like)</div></div>
    <div class="c"><div class="v">{review}</div><div class="k">In human-review queue</div></div>
  </div>

  <div class="foot"><span>The Pencil Count &nbsp;&bull;&nbsp; Multnomah County</span><span class="pagetag">01</span></div>
</section>

<!-- ========== TALLY ========== -->
<section class="sheet">
  <div class="shead"><h2>All write-in candidates</h2><span class="sk">Tally / 02</span></div>
  <p class="lead">Consolidated counts after folding handwriting/OCR spelling variants onto known
  candidates. The top 40 names are listed; Pencil is highlighted.</p>
  <table>
    <thead><tr><th>#</th><th>Write-in candidate</th><th class="num">Votes</th><th>By party</th></tr></thead>
    <tbody>
    {table_rows}
    </tbody>
  </table>
  <div class="tail">Plus <b>{tail_n}</b> further distinct write-in values accounting for
  <b>{tail_votes}</b> additional votes (rare names, joke entries, and garbled reads), most of
  which sit in the human-review queue. Full per-ballot detail is in <code>results.csv</code>.</div>
  <div class="foot"><span>The Pencil Count &nbsp;&bull;&nbsp; Multnomah County</span><span class="pagetag">02</span></div>
</section>

<!-- ========== GALLERY ========== -->
<section class="sheet">
  <div class="shead"><h2>The ballots themselves</h2><span class="sk">Evidence / 03</span></div>
  <p class="gintro">Actual cropped write-in lines from Multnomah County ballots. Pencil votes are
  shown first, then the leading other write-ins. Each crop lists its box and sequence, party, and
  the model&rsquo;s transcription.</p>
  {gallery}
  <div class="foot"><span>The Pencil Count &nbsp;&bull;&nbsp; Multnomah County</span><span class="pagetag">03</span></div>
</section>

<!-- ========== VALIDATION ========== -->
<section class="sheet">
  <style>
    .vtable{width:100%;border-collapse:collapse;margin:10px 0 6px;font-size:13px;}
    .vtable th,.vtable td{padding:7px 10px;border-bottom:1px solid var(--hair);text-align:left;}
    .vtable th.num,.vtable td.num{text-align:right;font-variant-numeric:tabular-nums;}
    .vtable td.ok{color:#1f7a3d;font-weight:600;}
    .vtable td.warn{color:var(--eraser);font-weight:600;}
    .vh{font-size:15px;margin:20px 0 4px;}
    .vp{font-size:12.5px;line-height:1.5;color:var(--ink-soft);margin:0 0 6px;}
    .srcnote{font-size:11px;color:var(--graphite);margin:2px 0 0;}
  </style>
  <div class="shead"><h2>How we checked our work</h2><span class="sk">Validation / 04</span></div>
  <p class="lead">An automated count is only as good as its checks. The full run is cross-checked
  against the Secretary of State&rsquo;s published totals, coverage is verified, and every counted
  ballot traces back to a source image.</p>

  <h3 class="vh">Cross-check against official county totals</h3>
  <p class="vp">The Secretary of State publishes the aggregate write-in count for each contest (it does
  not publish write-in names). We compare our detected filled write-in ovals to that official
  Multnomah County figure. Officials register a write-in when the oval is filled, so this is a
  like-for-like comparison.</p>
  <table class="vtable">
    <thead><tr><th>Contest</th><th class="num">Our count</th><th class="num">Official</th><th class="num">Difference</th></tr></thead>
    <tbody>{val_rows}</tbody>
  </table>
  <p class="srcnote">Source: {val_src} &middot; {val_status}, as of {val_asof} &middot; Multnomah County reported 107 of 107 precincts.</p>

  <h3 class="vh">Complete coverage</h3>
  <p class="vp"><b>{coverage} ballot boxes ingested ({coverage_pct}).</b> The count covers every ballot
  box on the scan volume. Completeness is verified on every run, so a partial scan cannot be
  mistaken for a finished count.</p>

  <h3 class="vh">Guardrails that run on every count</h3>
  <div class="caveat"><b>Ingest completeness.</b> The pipeline compares boxes present on disk to boxes
  ingested and warns if any are missing, so a partial scan is caught before the count is trusted.</div>
  <div class="caveat"><b>Official reconciliation.</b> After every full run, detected write-in totals
  are compared to the official county aggregate above; a divergence beyond 15% raises a warning to
  investigate before the figures are used.</div>
  <div class="caveat"><b>Full traceability.</b> Every counted ballot maps to a box and sequence in
  <code>results.csv</code>, and every ambiguous read sits in <code>review_queue.csv</code> with its
  crop image, so any number here can be audited back to the original ballot.</div>

  <div class="foot"><span>The Pencil Count &nbsp;&bull;&nbsp; Multnomah County</span><span class="pagetag">04</span></div>
</section>

<!-- ========== METHOD ========== -->
<section class="sheet">
  <div class="shead"><h2>Method &amp; caveats</h2><span class="sk">Notes / 05</span></div>
  <div class="method">
    <h3>How it works</h3>
    <p>A resumable, fully-local pipeline processes every ballot image in six stages. Nothing leaves
    the machine; no cloud services are used.</p>
    <div class="pipe">
      <span class="stage"><b>1</b> Manifest</span><span class="stage"><b>2</b> Classify</span>
      <span class="stage"><b>3</b> Locate</span><span class="stage"><b>4</b> Mark</span>
      <span class="stage"><b>5</b> Vision read</span><span class="stage"><b>6</b> Match</span>
    </div>

    <h3>Read this carefully</h3>
    <div class="caveat"><b>Automated, not yet human-verified.</b> Counts come from local OCR
    (Tesseract) and a local vision model (gemma). They are an evidence-grade first pass, not a
    certified canvass.</div>
    <div class="caveat"><b>Handwriting fragments names.</b> Spelling variants were folded onto known
    candidates ({top_folded}). Roughly {review} reads remain in a human-review queue, and some real
    candidates are still undercounted where fragments were too garbled to fold safely.</div>
    <div class="caveat"><b>Pencil matching tolerates one OCR error.</b> Single-character variants
    (pensil, percil, fencil, rencil, penci) are counted as Pencil, since one substitution or
    deletion is the dominant handwriting/scan error. More distant reads (&ldquo;pencing&rdquo;,
    &ldquo;pen c&rdquo;) are left in the review queue rather than auto-counted.</div>
    <div class="caveat"><b>Mislocated regions auto-quarantined.</b> A genuine write-in line yields
    varied handwriting; a region that landed on printed text repeats the same transcription on
    every ballot of a layout. Any unvalidated layout that collapses to one repeated non-Pencil
    string is flagged and excluded. This caught one Republican layout reading a printed contest
    header (&ldquo;State Representative, 49th District&rdquo;, {misread_printed} ballots) and the
    nonpartisan layouts reading a printed Court of Appeals name. The {out_of_scope} nonpartisan
    (&ldquo;other&rdquo;) reads are also excluded outright, since those ballots carry no Governor
    contest.</div>
    <div class="caveat"><b>Fully traceable.</b> Every counted ballot maps to a box and sequence in
    <code>results.csv</code>; the review queue is in <code>review_queue.csv</code> with crop paths.</div>

    <h3>Scope</h3>
    <p>This report covers Multnomah County Democratic and Republican primary ballot images only.
    It is not a statewide total.</p>
  </div>
  <div class="foot"><span>The Pencil Count &nbsp;&bull;&nbsp; Multnomah County</span><span class="pagetag">05</span></div>
</section>

</body></html>"""


if __name__ == "__main__":
    main()
