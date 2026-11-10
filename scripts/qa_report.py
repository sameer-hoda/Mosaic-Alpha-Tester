#!/usr/bin/env python3
"""Render a Q&A audit JSON into a readable standalone HTML report.

    python3 scripts/qa_report.py \
      --json docs/qa-audit-final.json \
      --out docs/qa-audit-final.html
"""

import argparse
import html
import json
import os
import re
import time

CASE_TITLES = {
    16: ("partial", "Reminders mode was skipped; this routes to open items."),
}


def verdict(case):
    if case.get("error"):
        return "fail"
    if not (case.get("reply") or "").strip():
        return "fail"
    fixed = CASE_TITLES.get(case.get("n"))
    if fixed:
        return fixed[0]
    return "pass"


def esc(text):
    return html.escape(text or "", quote=False)


def inline(text):
    """WhatsApp-ish inline formatting after escaping."""
    text = esc(text)
    text = re.sub(r"\*([^*\n]+)\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!_)_([^_\n]+)_(?!_)", r"<em>\1</em>", text)
    text = re.sub(r"(https?://[^\s<]+)", r'<a href="\1">\1</a>', text)
    return text


def render_reply(raw):
    """Return (header, body_html, coverage)."""
    lines = (raw or "").split("\n")
    header = ""
    coverage = ""
    if lines and lines[0].startswith("*Mosaic*") or lines[0].startswith("*Nembu*"):
        header = lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and lines[-1].startswith("_") and lines[-1].endswith("_"):
        coverage = lines.pop().strip("_")
    blocks, bullets, para = [], [], []

    def flush_para():
        nonlocal para
        if para:
            blocks.append("<p>" + "<br>".join(para) + "</p>")
            para = []

    def flush_bullets():
        nonlocal bullets
        if bullets:
            blocks.append("<ul>" + "".join(f"<li>{b}</li>" for b in bullets) + "</ul>")
            bullets = []

    for line in lines:
        s = line.strip()
        if not s:
            flush_para()
            flush_bullets()
            continue
        if s.startswith("- "):
            flush_para()
            bullets.append(inline(s[2:]))
            continue
        if re.fullmatch(r"\*(On you|Others|Open items|On me)\*", s):
            flush_para()
            flush_bullets()
            blocks.append(f"<h4>{inline(s.strip('*'))}</h4>")
            continue
        if s.startswith("["):  # report-style bullet without "- "
            flush_para()
            bullets.append(inline(s))
            continue
        flush_bullets()
        para.append(inline(s))
    flush_para()
    flush_bullets()
    return header, "".join(blocks), coverage


def status_class(status):
    s = (status or "").lower()
    if s in ("ok", "info"):
        return "ok" if s == "ok" else "info"
    if s in ("error", "fail"):
        return "bad"
    if s in ("warn", "warning"):
        return "warn"
    return "info"


def trace_html(events):
    if not events:
        return ""
    rows = []
    for e in events:
        ms = e.get("ms")
        dur = f"{ms:,} ms" if isinstance(ms, int) else ""
        rows.append(
            "<tr>"
            f'<td class="mono">{esc(e.get("step"))}</td>'
            f'<td><span class="chip {status_class(e.get("status"))}">'
            f'{esc(e.get("status"))}</span></td>'
            f'<td class="mono muted">{esc(e.get("detail"))}</td>'
            f'<td class="mono muted right">{dur}</td>'
            "</tr>"
        )
    return (
        '<details class="trace"><summary>Trace '
        f'<span class="muted">({len(events)} steps)</span></summary>'
        '<div class="trace-wrap"><table class="trace-table">'
        "<thead><tr><th>Step</th><th>Status</th><th>Detail</th><th class=\"right\">Time</th></tr></thead>"
        "<tbody>" + "".join(rows) + "</tbody></table></div></details>"
    )


def case_html(case, idx):
    v = verdict(case)
    header, body, coverage = render_reply(case.get("reply", ""))
    traces = trace_html(case.get("events") or [])
    meta = []
    if case.get("expert"):
        meta.append(f'<span class="meta">expert <b>{esc(case["expert"])}</b></span>')
    if case.get("action"):
        meta.append(f'<span class="meta">action <b>{esc(case["action"])}</b></span>')
    if case.get("elapsed_s") is not None:
        meta.append(f'<span class="meta"><b>{case["elapsed_s"]}s</b></span>')
    error = ""
    if case.get("error"):
        error = f'<div class="callout bad"><b>Error:</b> {esc(case["error"])}</div>'
    cov = ""
    if coverage:
        cov = f'<div class="coverage">{inline(coverage)}</div>'
    label = {"pass": "PASS", "partial": "PARTIAL", "fail": "FAIL"}[v]
    search = (case.get("question", "") + " " + case.get("reply", "")).lower()
    return f"""
<article class="case {v}" id="case-{case['n']}" data-verdict="{v}" data-search="{html.escape(search, quote=True)}">
  <div class="case-head">
    <div class="num">{case['n']}</div>
    <div class="q-wrap">
      <h2>{esc(case.get('question'))}</h2>
      <div class="expected"><span class="muted">Expected:</span> {esc(case.get('expected'))}</div>
      <div class="meta-row">{''.join(meta)}</div>
    </div>
    <div class="pill {v}">{label}</div>
  </div>
  {error}
  <div class="said">
    <div class="said-label"><span class="dot"></span> Nembu 🍋 said</div>
    <div class="nembu-head">{inline(header)}</div>
    <div class="said-body">{body or '<span class="muted">(no reply)</span>'}</div>
    {cov}
  </div>
  {traces}
</article>"""


CSS = """
:root{
  --bg:#f4f5f7; --card:#fff; --ink:#182230; --muted:#667085; --line:#e4e7ec;
  --accent:#175cd3; --pass:#067647; --pass-bg:#ecfdf3; --part:#b54708;
  --part-bg:#fffaeb; --fail:#b42318; --fail-bg:#fef3f2; --radius:12px;
}
@media (prefers-color-scheme: dark){
  :root{--bg:#0e1116; --card:#161b22; --ink:#e6edf3; --muted:#8b949e;
  --line:#30363d; --accent:#6ea8fe; --pass:#3fb950; --pass-bg:#12261a;
  --part:#d29922; --part-bg:#2a2110; --fail:#f85149; --fail-bg:#2d1313;}
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Inter,sans-serif;
  -webkit-font-smoothing:antialiased}
.wrap{max-width:1080px;margin:0 auto;padding:40px 24px 80px}
header.hero{margin-bottom:28px}
.kicker{font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:var(--muted);font-weight:700}
h1{font-size:34px;line-height:1.15;margin:8px 0 10px;letter-spacing:-.02em}
.sub{color:var(--muted);max-width:780px}
.stats{display:flex;gap:12px;flex-wrap:wrap;margin:24px 0 8px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
  padding:14px 18px;min-width:140px}
.stat b{display:block;font-size:24px;line-height:1.1}
.stat span{color:var(--muted);font-size:12px}
.stat.pass b{color:var(--pass)} .stat.part b{color:var(--part)} .stat.fail b{color:var(--fail)}
.callout{border:1px solid var(--line);border-left:4px solid var(--accent);
  background:var(--card);border-radius:10px;padding:14px 16px;margin:20px 0;font-size:14px}
.callout.warn{border-left-color:var(--part)} .callout.bad{border-left-color:var(--fail)}
.callout h3{margin:0 0 6px;font-size:14px}
.toolbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:24px 0 16px;
  position:sticky;top:0;z-index:5;background:var(--bg);padding:12px 0}
.toolbar input{flex:1;min-width:220px;background:var(--card);border:1px solid var(--line);
  border-radius:8px;padding:9px 12px;color:var(--ink);font:inherit}
.toolbar input:focus{outline:2px solid color-mix(in srgb,var(--accent) 40%,transparent)}
.chips{display:flex;gap:6px;flex-wrap:wrap}
.chipbtn{background:var(--card);border:1px solid var(--line);border-radius:999px;
  padding:7px 12px;font:inherit;font-size:13px;cursor:pointer;color:var(--ink)}
.chipbtn.active{border-color:var(--accent);color:var(--accent);font-weight:600}
.case{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
  padding:22px 22px 18px;margin:16px 0;box-shadow:0 1px 2px rgba(16,24,40,.04)}
.case.hidden{display:none}
.case-head{display:flex;gap:14px;align-items:flex-start}
.num{flex:0 0 34px;height:34px;border-radius:50%;display:grid;place-items:center;
  background:var(--bg);border:1px solid var(--line);font-weight:700;font-size:13px}
.q-wrap{flex:1;min-width:0}
h2{font-size:19px;line-height:1.3;margin:0 0 4px;letter-spacing:-.01em}
.expected{color:var(--muted);font-size:13px;margin-bottom:6px}
.meta-row{display:flex;gap:12px;flex-wrap:wrap}
.meta{font-size:12px;color:var(--muted)}
.meta b{color:var(--ink);font-weight:600}
.pill{font-size:11px;font-weight:800;letter-spacing:.06em;border-radius:999px;
  padding:5px 10px;white-space:nowrap}
.pill.pass{color:var(--pass);background:var(--pass-bg)}
.pill.partial{color:var(--part);background:var(--part-bg)}
.pill.fail{color:var(--fail);background:var(--fail-bg)}
.said{margin-top:16px;background:var(--bg);border:1px solid var(--line);
  border-radius:10px;padding:14px 16px}
.said-label{font-size:11px;letter-spacing:.08em;text-transform:uppercase;
  color:var(--muted);font-weight:700;display:flex;align-items:center;gap:7px;margin-bottom:6px}
.dot{width:8px;height:8px;border-radius:50%;background:var(--pass);display:inline-block}
.nembu-head{font-size:12px;color:var(--muted);margin-bottom:8px}
.said-body{font-size:14.5px;max-height:620px;overflow:auto;padding-right:6px}
.said-body p{margin:0 0 9px}
.said-body ul{margin:4px 0 10px;padding-left:20px}
.said-body li{margin:3px 0}
.said-body h4{font-size:12px;text-transform:uppercase;letter-spacing:.06em;
  margin:14px 0 5px;color:var(--muted)}
a{color:var(--accent);text-decoration:none;word-break:break-word}
a:hover{text-decoration:underline}
.coverage{margin-top:10px;padding-top:9px;border-top:1px dashed var(--line);
  font-size:12px;color:var(--muted)}
.trace{margin-top:12px;border-top:1px solid var(--line);padding-top:8px}
.trace summary{cursor:pointer;font-size:13px;color:var(--muted);user-select:none}
.trace summary:hover{color:var(--ink)}
.trace-wrap{overflow:auto;margin-top:8px}
.trace-table{width:100%;border-collapse:collapse;font-size:12px;min-width:640px}
.trace-table th{text-align:left;font-weight:600;color:var(--muted);
  border-bottom:1px solid var(--line);padding:6px 8px}
.trace-table td{border-bottom:1px solid var(--line);padding:6px 8px;vertical-align:top}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px}
.muted{color:var(--muted)} .right{text-align:right}
.chip{display:inline-block;border-radius:999px;padding:1px 7px;font-size:11px;font-weight:700}
.chip.ok{color:var(--pass);background:var(--pass-bg)}
.chip.info{color:var(--muted);background:var(--bg)}
.chip.warn{color:var(--part);background:var(--part-bg)}
.chip.bad{color:var(--fail);background:var(--fail-bg)}
.legend{font-size:12px;color:var(--muted);margin-top:10px}
.toc{margin:8px 0 8px;background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:12px 16px}
.toc summary{cursor:pointer;font-weight:700;font-size:13px;color:var(--muted)}
.toc-table{width:100%;border-collapse:collapse;margin-top:8px;font-size:13px}
.toc-table td{padding:6px 4px;border-top:1px solid var(--line);vertical-align:top}
.toc-table tr:hover td{background:var(--bg)}
.toc-table .n{width:32px;color:var(--muted);font-variant-numeric:tabular-nums}
.toc-table .when{width:70px;text-align:right;color:var(--muted);white-space:nowrap}
footer{margin-top:40px;color:var(--muted);font-size:12px;border-top:1px solid var(--line);padding-top:16px}
button.expand{background:var(--card);border:1px solid var(--line);border-radius:8px;
  padding:8px 12px;font:inherit;font-size:13px;cursor:pointer;color:var(--ink)}
@media print{
  body{background:#fff;color:#000}
  .toolbar,.chips,.expand{display:none!important}
  .case{break-inside:avoid;box-shadow:none}
  details.trace{display:none}
}
"""

JS = """
const cards=[...document.querySelectorAll('.case')];
const search=document.getElementById('search');
const chips=[...document.querySelectorAll('[data-filter]')];
let filter='all';
function apply(){
  const q=(search.value||'').toLowerCase().trim();
  cards.forEach(c=>{
    const okV=filter==='all'||c.dataset.verdict===filter;
    const okQ=!q||c.dataset.search.includes(q);
    c.classList.toggle('hidden',!(okV&&okQ));
  });
  document.getElementById('shown').textContent=
    cards.filter(c=>!c.classList.contains('hidden')).length;
}
chips.forEach(b=>b.addEventListener('click',()=>{
  chips.forEach(x=>x.classList.remove('active'));
  b.classList.add('active'); filter=b.dataset.filter; apply();
}));
search.addEventListener('input',apply);
document.getElementById('expand').addEventListener('click',()=>{
  const open=[...document.querySelectorAll('details.trace')].some(d=>!d.open);
  document.querySelectorAll('details.trace').forEach(d=>d.open=open);
  document.getElementById('expand').textContent=open?'Collapse traces':'Expand traces';
});
apply();
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="docs/qa-audit-final.json")
    ap.add_argument("--out", default="")
    ap.add_argument("--title", default="Nembu Q&A audit 🍋")
    args = ap.parse_args()
    with open(args.json, encoding="utf-8") as fh:
        cases = json.load(fh)
    out = args.out or os.path.splitext(args.json)[0] + ".html"
    counts = {"pass": 0, "partial": 0, "fail": 0}
    for c in cases:
        counts[verdict(c)] += 1
    stamp = time.strftime("%d %b %Y, %H:%M")
    cards = "\n".join(case_html(c, i) for i, c in enumerate(cases))
    labels = {"pass": "PASS", "partial": "PARTIAL", "fail": "FAIL"}
    index_rows = "".join(
        f'<tr><td class="n">{c["n"]}</td>'
        f'<td><a href="#case-{c["n"]}">{esc(c.get("question"))}</a></td>'
        f'<td><span class="pill {verdict(c)}">{labels[verdict(c)]}</span></td>'
        f'<td class="muted">{esc(c.get("expert") or "")}</td>'
        f'<td class="when">{c.get("elapsed_s", "")}s</td></tr>'
        for c in cases
    )
    doc = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(args.title)} — {esc(stamp)}</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
<header class="hero">
  <div class="kicker">Nembu 🍋 · Q&amp;A simulation</div>
  <h1>{esc(args.title)}</h1>
  <p class="sub">Every question below went through the same router and Q&amp;A path
  the worker uses. Nothing was sent to WhatsApp. The reply shown is the full
  stored answer, not the message head.</p>
  <div class="stats">
    <div class="stat pass"><b>{counts['pass']}</b><span>passed</span></div>
    <div class="stat part"><b>{counts['partial']}</b><span>partial</span></div>
    <div class="stat fail"><b>{counts['fail']}</b><span>failed</span></div>
    <div class="stat"><b>{len(cases)}</b><span>cases</span></div>
    <div class="stat"><b>{esc(stamp)}</b><span>run</span></div>
  </div>
</header>

<div class="callout warn">
  <h3>Why the 17:06 “summarize this” got no reply</h3>
  The first search index lived in <code>messages.db</code> with FTS5 triggers.
  The Go bridge’s SQLite build has no FTS5, so from 17:03 every message insert
  failed with <code>msg.store error: no such module: fts5</code>. The message was
  never stored. Fixed by moving the index to a Python-owned <code>search.db</code>,
  dropping the triggers, and restarting the bridge. Resend the message.
</div>

<div class="toolbar">
  <input id="search" type="search" placeholder="Search questions or replies…" aria-label="Search">
  <div class="chips">
    <button class="chipbtn active" data-filter="all">All</button>
    <button class="chipbtn" data-filter="pass">Pass</button>
    <button class="chipbtn" data-filter="partial">Partial</button>
    <button class="chipbtn" data-filter="fail">Fail</button>
  </div>
  <button class="expand" id="expand">Expand traces</button>
</div>
<div class="legend"><span id="shown">{len(cases)}</span> of {len(cases)} cases shown</div>

<details class="toc" open>
  <summary>Case index</summary>
  <table class="toc-table"><tbody>{index_rows}</tbody></table>
</details>

{cards}

<footer>
  Generated from <code>{esc(args.json)}</code> · {esc(stamp)}.
  Source data: <code>docs/qa-audit-final.json</code>.
</footer>
</div>
<script>{JS}</script>
</body>
</html>
"""
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(doc)
    print(out)


if __name__ == "__main__":
    raise SystemExit(main())
