import os
import sqlite3
import sys
import threading
import time
import webbrowser
from collections import Counter
from pathlib import Path

from flask import Flask, jsonify, request

BASE = os.path.dirname(os.path.abspath(__file__))
MONITOR_DB = os.path.join(BASE, "monitor.db")
INCIDENT_DB = os.path.join(BASE, "incidents.db")
PORT = 5000

app = Flask(__name__)


def open_ro(path):
    """Open a database read-only so the dashboard can never disturb the recorder."""
    if not os.path.exists(path):
        return None
    try:
        uri = Path(path).resolve().as_uri() + "?mode=ro"
        return sqlite3.connect(uri, uri=True, timeout=10)
    except sqlite3.OperationalError:
        return None


@app.route("/api/samples")
def api_samples():
    now = time.time()
    if "start" in request.args:
        start, end = float(request.args["start"]), float(request.args["end"])
    else:
        start, end = now - float(request.args.get("minutes", 60)) * 60, now

    bucket = max(1, int((end - start) / 1200))  # about 1200 points max per chart
    out = {"t": [], "cpu": [], "mem": [], "disk_mb": [], "net_kb": [], "last": None}
    conn = open_ro(MONITOR_DB)
    if conn is None:
        return jsonify(out)
    try:
        # MAX per bucket so short spikes stay visible when zoomed out
        rows = conn.execute(
            "SELECT CAST(ts / ? AS INTEGER) * ? AS b, MAX(cpu), MAX(mem), MAX(disk_mb), MAX(net_kb) "
            "FROM samples WHERE ts BETWEEN ? AND ? GROUP BY b ORDER BY b",
            (bucket, bucket, start, end)).fetchall()
        last = conn.execute("SELECT MAX(ts) FROM samples").fetchone()[0]
    except sqlite3.OperationalError:
        rows, last = [], None
    finally:
        conn.close()

    for b, cpu, mem, disk, net in rows:
        out["t"].append(int(b * 1000))
        out["cpu"].append(cpu)
        out["mem"].append(mem)
        out["disk_mb"].append(disk)
        out["net_kb"].append(net)
    out["last"] = last
    return jsonify(out)


def fetch_incidents(seconds_back):
    conn = open_ro(INCIDENT_DB)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT id, metric, start_ts, end_ts, duration, baseline, peak, suspects, fg_app, explanation "
            "FROM incidents WHERE end_ts >= ? ORDER BY start_ts DESC",
            (time.time() - seconds_back,)).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        conn.close()
    keys = ["id", "metric", "start", "end", "duration", "baseline", "peak",
            "suspects", "fg_app", "explanation"]
    return [dict(zip(keys, r)) for r in rows]


@app.route("/api/incidents")
def api_incidents():
    hours = float(request.args.get("hours", 24))
    return jsonify(fetch_incidents(hours * 3600))


@app.route("/api/summary")
def api_summary():
    incs = fetch_incidents(24 * 3600)
    suspects = Counter()
    for i in incs:
        if i["suspects"]:
            first = i["suspects"].split(";")[0].split(" (")[0].strip()
            if first:
                suspects[first] += 1
    longest = max(incs, key=lambda i: i["duration"], default=None)
    return jsonify({
        "total": len(incs),
        "top_suspects": suspects.most_common(5),
        "longest": longest,
    })


_cache = {}


def cached(key, ttl, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (time.time(), val)
    return val


def compute_offenders(hours):
    out = {"cpu": [], "disk": [], "foreground": []}
    conn = open_ro(MONITOR_DB)
    if conn is None:
        return out
    since = time.time() - hours * 3600
    try:
        # each sample is about 1 second, so SUM(cpu)/100/60 = minutes of the whole CPU used
        out["cpu"] = [[n, v] for n, v in conn.execute(
            "SELECT p.name, SUM(p.cpu) / 100.0 / 60.0 AS v FROM process_samples p "
            "JOIN samples s ON s.id = p.sample_id WHERE s.ts >= ? "
            "GROUP BY p.name ORDER BY v DESC LIMIT 8", (since,))]
        out["disk"] = [[n, v] for n, v in conn.execute(
            "SELECT p.name, SUM(COALESCE(p.io_kb, 0)) / 1024.0 AS v FROM process_samples p "
            "JOIN samples s ON s.id = p.sample_id WHERE s.ts >= ? "
            "GROUP BY p.name HAVING v > 0 ORDER BY v DESC LIMIT 8", (since,))]
        out["foreground"] = [[n, v] for n, v in conn.execute(
            "SELECT fg_app, COUNT(*) / 60.0 AS v FROM samples "
            "WHERE ts >= ? AND fg_app IS NOT NULL GROUP BY fg_app ORDER BY v DESC LIMIT 8", (since,))]
    except sqlite3.OperationalError:
        pass
    finally:
        conn.close()
    return out


@app.route("/api/offenders")
def api_offenders():
    hours = min(float(request.args.get("hours", 24)), 24 * 30)
    return jsonify(cached(("off", hours), 60, lambda: compute_offenders(hours)))


def incidents_between(lo, hi):
    conn = open_ro(INCIDENT_DB)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT id, metric, start_ts, end_ts, duration, explanation FROM incidents "
            "WHERE end_ts >= ? AND start_ts <= ? ORDER BY start_ts", (lo, hi)).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        conn.close()
    keys = ["id", "metric", "start", "end", "duration", "explanation"]
    return [dict(zip(keys, r)) for r in rows]


def make_verdict(stats, procs, incs):
    cpu_avg, cpu_max, mem_avg, mem_max, disk_avg, disk_max, net_avg, net_max = stats
    if incs:
        return "A detected incident covers this time. " + incs[0]["explanation"]
    busy = []
    if cpu_max >= 60:
        busy.append(f"CPU peaked at {cpu_max:.0f}%")
    if mem_max >= 85:
        busy.append(f"memory reached {mem_max:.0f}%")
    if disk_max >= 50:
        busy.append(f"disk hit {disk_max:.0f} MB/s")
    if net_max >= 5000:
        busy.append(f"network hit {net_max / 1024:.1f} MB/s")
    if busy:
        text = "Resources were busy: " + ", ".join(busy) + "."
        if procs:
            text += f" The heaviest process was {procs[0]['name']} (averaging {procs[0]['cpu']:.0f}% CPU)."
        return text + " It was not unusual enough to count as an incident."
    return (f"Nothing unusual was recorded: CPU averaged {cpu_avg:.0f}% and memory {mem_avg:.0f}%. "
            "If it felt slow, the cause may be something this tool does not measure, such as GPU load, "
            "a slow internet connection, or one app stuck on a single busy core.")


@app.route("/api/whatwas")
def api_whatwas():
    ts = float(request.args["ts"])
    win = min(float(request.args.get("window", 60)), 1800)
    lo, hi = ts - win, ts + win
    conn = open_ro(MONITOR_DB)
    if conn is None:
        return jsonify({"found": False})
    try:
        row = conn.execute(
            "SELECT COUNT(*), AVG(cpu), MAX(cpu), AVG(mem), MAX(mem), AVG(disk_mb), MAX(disk_mb), "
            "AVG(net_kb), MAX(net_kb) FROM samples WHERE ts BETWEEN ? AND ?", (lo, hi)).fetchone()
        n = row[0]
        if not n:
            return jsonify({"found": False})
        stats = [x or 0 for x in row[1:]]

        # what each process normally uses: the 10 minutes before this window
        nb = conn.execute("SELECT COUNT(*) FROM samples WHERE ts BETWEEN ? AND ?",
                          (lo - 600, lo - 1)).fetchone()[0]
        before = {}
        if nb:
            before = dict(conn.execute(
                "SELECT p.name, SUM(p.cpu) FROM process_samples p JOIN samples s ON s.id = p.sample_id "
                "WHERE s.ts BETWEEN ? AND ? GROUP BY p.name", (lo - 600, lo - 1)).fetchall())

        rows = conn.execute(
            "SELECT p.name, SUM(p.cpu), MAX(p.cpu), SUM(COALESCE(p.io_kb, 0)), MAX(p.mem_mb) "
            "FROM process_samples p JOIN samples s ON s.id = p.sample_id "
            "WHERE s.ts BETWEEN ? AND ? GROUP BY p.name ORDER BY 2 DESC LIMIT 6", (lo, hi)).fetchall()
        procs = [{
            "name": name, "cpu": c / n, "cpu_peak": cmax,
            "usual": (before.get(name, 0) / nb) if nb else None,
            "io_mb": io / n / 1024.0, "mem_peak": mem,
        } for name, c, cmax, io, mem in rows]

        fg = conn.execute(
            "SELECT fg_app, COUNT(*) FROM samples WHERE ts BETWEEN ? AND ? AND fg_app IS NOT NULL "
            "GROUP BY fg_app ORDER BY 2 DESC LIMIT 3", (lo, hi)).fetchall()
    except sqlite3.OperationalError:
        return jsonify({"found": False})
    finally:
        conn.close()

    incs = incidents_between(lo - 30, hi + 30)
    return jsonify({
        "found": True, "samples": n,
        "stats": {"cpu": [stats[0], stats[1]], "mem": [stats[2], stats[3]],
                  "disk": [stats[4], stats[5]], "net": [stats[6], stats[7]]},
        "procs": procs,
        "foreground": [[a, c / n * 100] for a, c in fg],
        "incidents": incs,
        "verdict": make_verdict(stats, procs, incs),
    })


@app.route("/")
def index():
    return PAGE


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Why is this slow?</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
:root{--bg:#0f1419;--card:#171d25;--line:#262e3a;--text:#e6e9ee;--muted:#8b95a5;--bad:#ff5d5d;--good:#3ddc97}
*{box-sizing:border-box}
[hidden]{display:none!important}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,"Segoe UI",sans-serif}
header{display:flex;align-items:center;gap:14px;padding:12px 20px;border-bottom:1px solid var(--line);flex-wrap:wrap}
h1{font-size:18px;margin:0 auto 0 0}
h2{font-size:14px;margin:0 0 10px}
h3{font-size:12px;margin:14px 0 6px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}
small{color:var(--muted);font-weight:400}
label{color:var(--muted);display:flex;align-items:center;gap:6px}
select,button{background:var(--card);color:var(--text);border:1px solid var(--line);border-radius:8px;padding:6px 10px;font:inherit}
button{cursor:pointer}
button:hover{border-color:var(--muted)}
.pill{padding:3px 10px;border-radius:999px;font-size:12px;border:1px solid var(--line);color:var(--muted)}
.pill.ok{color:var(--good);border-color:var(--good)}
.pill.bad{color:var(--bad);border-color:var(--bad)}
main{display:grid;grid-template-columns:1fr 380px;gap:16px;padding:16px 20px}
@media(max-width:1000px){main{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin-bottom:16px}
#chart{height:calc(100vh - 170px);min-height:540px}
#banner{margin:0 0 10px;padding:10px 12px;border-left:3px solid var(--bad);background:#2a1a1d;border-radius:6px}
#empty{padding:40px 10px;text-align:center;color:var(--muted)}
.muted{color:var(--muted)}
.stats{display:flex;gap:12px}
.stats div{flex:1;background:var(--bg);border-radius:8px;padding:10px;text-align:center}
.stats b{display:block;font-size:22px}
.stats span{color:var(--muted);font-size:12px}
.row{display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid var(--line)}
#list{max-height:calc(100vh - 430px);min-height:240px;overflow-y:auto;padding-right:4px}
.inc{border-left:4px solid var(--muted);background:var(--bg);border-radius:6px;padding:8px 10px;margin-bottom:8px;cursor:pointer}
.inc:hover{outline:1px solid var(--line)}
.inc.sel{outline:1px solid var(--bad)}
.inc-h{display:flex;justify-content:space-between;gap:8px;margin-bottom:2px}
.inc-h span{color:var(--muted);font-size:12px}
.inc-t{font-size:12.5px;color:#c9cfd9}
.tabs{display:flex;gap:6px;margin-bottom:12px;flex-wrap:wrap}
.tab{border-radius:999px;padding:6px 14px}
.tab.active{background:#243044;border-color:#4da3ff}
.toolbar{display:flex;gap:14px;align-items:center;flex-wrap:wrap;margin-bottom:12px}
input[type=datetime-local]{background:var(--card);color:var(--text);border:1px solid var(--line);border-radius:8px;padding:5px 8px;font:inherit;color-scheme:dark}
.grid3{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px}
.bar{margin-bottom:10px}
.bar-top{display:flex;justify-content:space-between;gap:8px;font-size:13px}
.bar-bg{height:6px;background:var(--bg);border-radius:4px;margin-top:3px}
.bar-fg{height:6px;border-radius:4px}
.verdict{padding:12px 14px;border-left:3px solid #4da3ff;background:#16263a;border-radius:6px;margin-bottom:14px}
.statgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:14px}
.statgrid div{background:var(--bg);border-radius:8px;padding:10px;text-align:center}
.statgrid b{display:block;font-size:18px}
.statgrid span{color:var(--muted);font-size:12px}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line)}
th{color:var(--muted);font-weight:500;font-size:12px}
.note{color:var(--muted);font-size:12px;margin:6px 0 0}
</style>
</head>
<body>
<header>
  <h1>Why is this slow?</h1>
  <span id="status" class="pill">connecting&hellip;</span>
  <label>Range
    <select id="range">
      <option value="15">Last 15 min</option>
      <option value="60" selected>Last hour</option>
      <option value="360">Last 6 hours</option>
      <option value="1440">Last 24 hours</option>
    </select>
  </label>
  <label><input type="checkbox" id="auto" checked> Auto-refresh</label>
  <button id="back" hidden>&larr; Back to live</button>
</header>
<main>
  <section>
    <div class="tabs">
      <button class="tab active" data-tab="timeline">Timeline</button>
      <button class="tab" data-tab="offenders">Top offenders</button>
      <button class="tab" data-tab="machine">Why was it slow?</button>
    </div>
    <div id="tab-timeline">
      <div id="banner" hidden></div>
      <div id="chart"></div>
      <div id="empty" hidden>No data yet. Make sure <b>recorder.py</b> is running in your project folder.</div>
    </div>
    <div id="tab-offenders" hidden>
      <div class="toolbar">
        <label>Period
          <select id="off-range">
            <option value="24">Last 24 hours</option>
            <option value="168">Last 7 days</option>
          </select>
        </label>
      </div>
      <div id="off-body" class="muted">Loading&hellip;</div>
    </div>
    <div id="tab-machine" hidden>
      <div class="toolbar">
        <label>When <input type="datetime-local" id="m-time"></label>
        <label>Window
          <select id="m-win">
            <option value="60">&plusmn; 1 min</option>
            <option value="300">&plusmn; 5 min</option>
            <option value="900">&plusmn; 15 min</option>
          </select>
        </label>
        <button id="m-go">Investigate</button>
      </div>
      <div id="m-body" class="muted">Pick the moment your laptop felt slow, then press Investigate.</div>
    </div>
  </section>
  <aside>
    <div class="card"><h2>Last 24 hours</h2><div id="summary"></div></div>
    <div class="card"><h2>Incidents <small>click one to inspect it</small></h2><div id="list"></div></div>
  </aside>
</main>

<script>
const KEYS = ['cpu', 'mem', 'disk_mb', 'net_kb'];
const COLORS = {cpu:'#4da3ff', mem:'#b48cff', disk_mb:'#ffb454', net_kb:'#3ddc97'};
const FILLS = {cpu:'rgba(77,163,255,.12)', mem:'rgba(180,140,255,.12)', disk_mb:'rgba(255,180,84,.12)', net_kb:'rgba(61,220,151,.12)'};
const LABELS = {cpu:'CPU', mem:'Memory', disk_mb:'Disk', net_kb:'Network'};
const UNITS = {cpu:'%', mem:'%', disk_mb:' MB/s', net_kb:' KB/s'};
const AXIS = {cpu:'y', mem:'y2', disk_mb:'y3', net_kb:'y4'};
const $ = id => document.getElementById(id);

let tab = 'timeline', mode = 'live', selected = null, view = null, timer = null, incidents = [], lastSig = '';

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function fmtTime(sec) {
  const d = new Date(sec * 1000);
  const t = d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'});
  if (d.toDateString() === new Date().toDateString()) return t;
  return d.toLocaleDateString([], {day:'numeric', month:'short'}) + ' ' + t;
}

async function getJSON(url) {
  const r = await fetch(url);
  return r.json();
}

function draw(d) {
  const empty = d.t.length === 0;
  $('chart').hidden = empty;
  $('empty').hidden = !empty;
  if (empty) return;

  const x = d.t.map(t => new Date(t));
  const traces = KEYS.map((k, i) => ({
    x: x, y: d[k], type: 'scatter', mode: 'lines', name: LABELS[k],
    xaxis: 'x', yaxis: i === 0 ? 'y' : 'y' + (i + 1),
    line: {color: COLORS[k], width: 1.5}, fill: 'tozeroy', fillcolor: FILLS[k],
    hovertemplate: '%{y:.1f}' + UNITS[k] + '<extra>' + LABELS[k] + '</extra>'
  }));

  const first = d.t[0], last = d.t[d.t.length - 1];
  const minw = Math.max((last - first) / 1000 * 0.004, 2);
  const shapes = incidents
    .filter(i => i.end * 1000 >= first && i.start * 1000 <= last)
    .map(i => ({
      type: 'rect', xref: 'x', yref: AXIS[i.metric] + ' domain',
      x0: new Date(i.start * 1000), x1: new Date(Math.max(i.end, i.start + minw) * 1000),
      y0: 0, y1: 1, line: {width: 0}, layer: 'below',
      fillcolor: i.id === selected ? 'rgba(255,93,93,.45)' : 'rgba(255,93,93,.20)'
    }));

  const grid = '#262e3a';
  const layout = {
    grid: {rows: 4, columns: 1, pattern: 'coupled', roworder: 'top to bottom', ygap: 0.1},
    paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)',
    font: {color: '#8b95a5', size: 11},
    margin: {l: 60, r: 16, t: 8, b: 36}, showlegend: false, hovermode: 'x unified',
    uirevision: mode === 'live' ? 'live-' + $('range').value : 'inc-' + selected,
    xaxis: {gridcolor: grid},
    yaxis: {title: {text: 'CPU %'}, range: [0, 100], gridcolor: grid},
    yaxis2: {title: {text: 'Memory %'}, range: [0, 100], gridcolor: grid},
    yaxis3: {title: {text: 'Disk MB/s'}, rangemode: 'tozero', gridcolor: grid},
    yaxis4: {title: {text: 'Net KB/s'}, rangemode: 'tozero', gridcolor: grid},
    shapes: shapes
  };
  Plotly.react('chart', traces, layout, {displayModeBar: false, responsive: true});
}

function renderList() {
  const sig = JSON.stringify(incidents.map(i => i.id)) + '|' + selected;
  if (sig === lastSig) return;
  lastSig = sig;
  const el = $('list');
  if (!incidents.length) {
    el.innerHTML = '<p class="muted">No incidents yet. Use your laptop normally, or run stress_test.py to create one.</p>';
    return;
  }
  el.innerHTML = incidents.map(i =>
    '<div class="inc ' + (i.id === selected ? 'sel' : '') + '" data-id="' + i.id +
    '" style="border-left-color:' + COLORS[i.metric] + '">' +
    '<div class="inc-h"><b>' + LABELS[i.metric] + '</b><span>' + fmtTime(i.start) +
    ' &middot; ' + Math.max(1, Math.round(i.duration)) + 's</span></div>' +
    '<div class="inc-t">' + esc(i.explanation) + '</div></div>'
  ).join('');
  el.querySelectorAll('.inc').forEach(n => { n.onclick = () => selectIncident(Number(n.dataset.id)); });
}

function renderSummary(s) {
  let h = '<div class="stats"><div><b>' + s.total + '</b><span>incidents</span></div>' +
          '<div><b>' + (s.longest ? Math.round(s.longest.duration) + 's' : '-') + '</b><span>longest</span></div></div>';
  if (s.top_suspects.length) {
    h += '<h3>Top offenders</h3>' + s.top_suspects.map(p =>
      '<div class="row"><span>' + esc(p[0]) + '</span><b>' + p[1] + '</b></div>').join('');
  } else {
    h += '<p class="muted">No offenders yet.</p>';
  }
  $('summary').innerHTML = h;
}

function renderStatus(last) {
  const el = $('status');
  if (last == null) { el.textContent = 'no data'; el.className = 'pill bad'; return; }
  const age = Date.now() / 1000 - last;
  if (age < 10) { el.textContent = 'recording'; el.className = 'pill ok'; }
  else { el.textContent = 'recorder stopped? last sample ' + Math.round(age) + 's ago'; el.className = 'pill bad'; }
}

async function refresh() {
  try {
    const url = mode === 'live'
      ? '/api/samples?minutes=' + $('range').value
      : '/api/samples?start=' + view.s + '&end=' + view.e;
    const [d, incs, sum] = await Promise.all([
      getJSON(url), getJSON('/api/incidents?hours=24'), getJSON('/api/summary')
    ]);
    incidents = incs;
    if (tab === 'timeline') draw(d);
    renderList();
    renderSummary(sum);
    renderStatus(d.last);
  } catch (e) {
    $('status').textContent = 'dashboard error: ' + e;
    $('status').className = 'pill bad';
  }
}

function startTimer() {
  if (timer) clearInterval(timer);
  timer = null;
  if ($('auto').checked && mode === 'live') timer = setInterval(refresh, 2000);
}

function selectIncident(id) {
  const inc = incidents.find(i => i.id === id);
  if (!inc) return;
  mode = 'incident';
  selected = id;
  view = {s: inc.start - 120, e: inc.end + 120};
  $('back').hidden = false;
  $('banner').hidden = false;
  $('banner').innerHTML = '<b>' + LABELS[inc.metric] + ' incident, ' + fmtTime(inc.start) + '</b><br>' + esc(inc.explanation);
  lastSig = '';
  startTimer();
  refresh();
}

function backToLive() {
  mode = 'live';
  selected = null;
  view = null;
  $('back').hidden = true;
  $('banner').hidden = true;
  lastSig = '';
  startTimer();
  refresh();
}

function fmtMin(v) { return v >= 60 ? (v / 60).toFixed(1) + ' h' : v.toFixed(1) + ' min'; }
function fmtMB(v) { return v >= 1024 ? (v / 1024).toFixed(1) + ' GB' : v.toFixed(0) + ' MB'; }

function barList(title, sub, rows, fmt, color) {
  const max = Math.max.apply(null, rows.map(r => r[1]).concat([0.0001]));
  let h = '<div class="card"><h2>' + title + '</h2>';
  if (!rows.length) return h + '<p class="muted">Not enough data yet.</p></div>';
  h += rows.map(r =>
    '<div class="bar"><div class="bar-top"><span>' + esc(r[0]) + '</span><b>' + fmt(r[1]) + '</b></div>' +
    '<div class="bar-bg"><div class="bar-fg" style="background:' + color + ';width:' + (r[1] / max * 100).toFixed(1) + '%"></div></div></div>'
  ).join('');
  return h + '<p class="note">' + sub + '</p></div>';
}

async function loadOffenders() {
  const body = $('off-body');
  body.innerHTML = 'Loading...';
  const h = $('off-range').value;
  try {
    const [o, incs] = await Promise.all([getJSON('/api/offenders?hours=' + h), getJSON('/api/incidents?hours=' + h)]);
    const longest = incs.slice().sort((a, b) => b.duration - a.duration).slice(0, 5);
    let html = '<div class="grid3">' +
      barList('Most CPU used', 'Total CPU time, counted as minutes of the whole CPU. Best guide to battery drain.', o.cpu, fmtMin, '#4da3ff') +
      barList('Most disk activity', 'Data read and written. Only the busiest disk users are recorded.', o.disk, fmtMB, '#ffb454') +
      barList('Most time in front', 'How long each app was the window you were using.', o.foreground, fmtMin, '#3ddc97') +
      '</div>';
    html += '<div class="card"><h2>Longest incidents</h2>';
    if (!longest.length) html += '<p class="muted">No incidents in this period.</p>';
    else html += longest.map(i =>
      '<div class="inc" style="border-left-color:' + COLORS[i.metric] + ';cursor:default"><div class="inc-h"><b>' +
      LABELS[i.metric] + '</b><span>' + fmtTime(i.start) + ' &middot; ' + Math.max(1, Math.round(i.duration)) +
      's</span></div><div class="inc-t">' + esc(i.explanation) + '</div></div>').join('');
    html += '</div>';
    body.innerHTML = html;
  } catch (e) {
    body.innerHTML = '<p class="muted">Could not load: ' + esc(e) + '</p>';
  }
}

function localInputValue(d) {
  const p = n => String(n).padStart(2, '0');
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + 'T' + p(d.getHours()) + ':' + p(d.getMinutes());
}

async function investigate() {
  const v = $('m-time').value;
  const body = $('m-body');
  if (!v) { body.innerHTML = '<p class="muted">Pick a date and time first.</p>'; return; }
  body.innerHTML = 'Investigating...';
  const ts = new Date(v).getTime() / 1000;
  try {
    const r = await getJSON('/api/whatwas?ts=' + ts + '&window=' + $('m-win').value);
    if (!r.found) {
      body.innerHTML = '<p class="muted">Nothing was recorded around that time. The recorder may not have been running.</p>';
      return;
    }
    const s = r.stats;
    let h = '<div class="verdict">' + esc(r.verdict) + '</div>';
    h += '<div class="statgrid">' +
      '<div><b>' + s.cpu[0].toFixed(0) + '% / ' + s.cpu[1].toFixed(0) + '%</b><span>CPU avg / peak</span></div>' +
      '<div><b>' + s.mem[0].toFixed(0) + '% / ' + s.mem[1].toFixed(0) + '%</b><span>Memory avg / peak</span></div>' +
      '<div><b>' + s.disk[0].toFixed(1) + ' / ' + s.disk[1].toFixed(1) + '</b><span>Disk MB/s avg / peak</span></div>' +
      '<div><b>' + (s.net[0] / 1024).toFixed(1) + ' / ' + (s.net[1] / 1024).toFixed(1) + '</b><span>Network MB/s avg / peak</span></div></div>';
    h += '<div class="card"><h2>Busiest processes</h2><table><tr><th>Process</th><th>CPU now</th><th>CPU usually</th><th>Disk MB/s</th><th>Peak memory</th></tr>';
    h += r.procs.map(p =>
      '<tr><td>' + esc(p.name) + '</td><td>' + p.cpu.toFixed(1) + '%</td><td>' +
      (p.usual == null ? '-' : p.usual.toFixed(1) + '%') + '</td><td>' + p.io_mb.toFixed(2) + '</td><td>' +
      p.mem_peak.toFixed(0) + ' MB</td></tr>').join('');
    h += '</table><p class="note">"CPU usually" is the average over the 10 minutes before this window.</p></div>';
    if (r.foreground.length) {
      h += '<div class="card"><h2>What you were using</h2>' +
        r.foreground.map(f => '<div class="row"><span>' + esc(f[0]) + '</span><b>' + f[1].toFixed(0) + '%</b></div>').join('') + '</div>';
    }
    if (r.incidents.length) {
      h += '<div class="card"><h2>Detected incidents nearby</h2>' + r.incidents.map(i =>
        '<div class="inc" style="border-left-color:' + COLORS[i.metric] + ';cursor:default"><div class="inc-h"><b>' +
        LABELS[i.metric] + '</b><span>' + fmtTime(i.start) + '</span></div><div class="inc-t">' + esc(i.explanation) + '</div></div>').join('') + '</div>';
    }
    body.innerHTML = h;
  } catch (e) {
    body.innerHTML = '<p class="muted">Could not load: ' + esc(e) + '</p>';
  }
}

function showTab(name) {
  tab = name;
  document.querySelectorAll('.tab').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  ['timeline', 'offenders', 'machine'].forEach(n => { $('tab-' + n).hidden = n !== name; });
  if (name === 'timeline') refresh();
  if (name === 'offenders') loadOffenders();
}

document.querySelectorAll('.tab').forEach(b => { b.onclick = () => showTab(b.dataset.tab); });
$('off-range').onchange = loadOffenders;
$('m-go').onclick = investigate;
$('m-time').value = localInputValue(new Date(Date.now() - 5 * 60000));

$('back').onclick = backToLive;
$('auto').onchange = startTimer;
$('range').onchange = () => { if (mode === 'incident') backToLive(); else refresh(); };

refresh();
startTimer();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    print(f"Dashboard running at http://127.0.0.1:{PORT}  (Ctrl+C to stop)")
    if "--no-browser" not in sys.argv:  # start_monitor.vbs uses this flag at login
        threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}")).start()
    app.run(host="127.0.0.1", port=PORT, debug=False)