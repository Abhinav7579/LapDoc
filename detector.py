import sqlite3
import statistics
import time
from collections import deque

SRC_DB = "monitor.db"
OUT_DB = "incidents.db"
WINDOW = 60         # seconds of history used as the baseline
BASELINE_MIN = 30   # samples needed before detecting anything
SIGMA = 3           # standard deviations above baseline
CHECK_EVERY = 10    # seconds between checks for new data
END_AFTER = 5       # calm samples in a row that end an incident
MAX_LEN = 600       # force-close an incident after this many seconds
IGNORE = "System Idle Process"

# floor = minimum value to count, jump = minimum rise above baseline,
# pcol = which per-process column to blame, min_delta = minimum per-process rise to be a suspect
METRICS = {
    "cpu": {"floor": 60, "jump": 25, "label": "CPU", "unit": "%", "pcol": "cpu",
            "min_delta": 2, "fmt": lambda v: f"{v:.0f}% CPU"},
    "mem": {"floor": 85, "jump": 10, "label": "Memory", "unit": "%", "pcol": "mem_mb",
            "min_delta": 50, "fmt": lambda v: f"{v:.0f} MB"},
    "disk_mb": {"floor": 50, "jump": 25, "label": "Disk activity", "unit": " MB/s", "pcol": "io_kb",
                "min_delta": 500, "fmt": lambda v: f"{v / 1024:.1f} MB/s"},
    "net_kb": {"floor": 5000, "jump": 2500, "label": "Network traffic", "unit": " KB/s", "pcol": "cpu",
               "min_delta": 2, "fmt": lambda v: f"{v:.0f}% CPU"},
}


def init_out(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS incidents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            metric TEXT, start_ts REAL, end_ts REAL, duration REAL,
            baseline REAL, peak REAL,
            suspects TEXT, fg_app TEXT, explanation TEXT,
            UNIQUE(metric, start_ts)
        );
    """)
    conn.commit()


def schema_ok():
    try:
        src = sqlite3.connect(f"file:{SRC_DB}?mode=ro", uri=True, timeout=30)
    except sqlite3.OperationalError:
        return False
    try:
        s = [r[1] for r in src.execute("PRAGMA table_info(samples)")]
        p = [r[1] for r in src.execute("PRAGMA table_info(process_samples)")]
    finally:
        src.close()
    return "fg_app" in s and "io_kb" in p


def analyze(src, metric, peak_sid, start_ts):
    """Rank processes by how much they rose compared with their own normal level."""
    cfg = METRICS[metric]
    col = cfg["pcol"]
    n = src.execute("SELECT COUNT(*) FROM samples WHERE ts BETWEEN ? AND ?",
                    (start_ts - WINDOW, start_ts - 1)).fetchone()[0] or 1
    before = dict(src.execute(
        f"SELECT p.name, SUM(COALESCE(p.{col}, 0)) FROM process_samples p "
        "JOIN samples s ON s.id = p.sample_id "
        "WHERE s.ts BETWEEN ? AND ? AND p.name != ? GROUP BY p.name",
        (start_ts - WINDOW, start_ts - 1, IGNORE)).fetchall())

    lo, hi = peak_sid - 2, peak_sid + 2
    during = src.execute(
        f"SELECT name, SUM(COALESCE({col}, 0)) FROM process_samples "
        "WHERE sample_id BETWEEN ? AND ? AND name != ? GROUP BY name",
        (lo, hi, IGNORE)).fetchall()
    m = hi - lo + 1

    ranked = []
    for name, total in during:
        after = total / m
        base = before.get(name, 0) / n
        if after - base >= cfg["min_delta"]:
            ranked.append((after - base, name, base, after, name not in before))
    ranked.sort(key=lambda x: x[0], reverse=True)
    return [(name, base, after, new) for _, name, base, after, new in ranked[:3]]


def describe(metric, suspects):
    fmt = METRICS[metric]["fmt"]
    return [f"{name} ({fmt(b)} to {fmt(a)}{', newly active' if new else ''})"
            for name, b, a, new in suspects]


def explain(metric, base, peak, start_ts, dur, parts, fg):
    cfg = METRICS[metric]
    when = time.strftime("%H:%M:%S", time.localtime(start_ts))
    text = (f"{cfg['label']} jumped to {peak:.0f}{cfg['unit']} at {when} "
            f"(normally about {base:.0f}{cfg['unit']}) and stayed high for {max(dur, 1):.0f}s.")
    if parts:
        text += f" Most likely cause: {parts[0]}."
        if len(parts) > 1:
            text += " Also involved: " + ", ".join(parts[1:]) + "."
    else:
        text += " No single process stood out."
    if metric == "net_kb":
        text += " (Per-app network use is not recorded, so this is a best guess from CPU activity.)"
    if fg:
        text += f" You were using: {fg}."
    return text


def close_incident(src, out, metric, e):
    dur = e["last_high"] - e["start_ts"]
    suspects = analyze(src, metric, e["peak_sid"], e["start_ts"])
    parts = describe(metric, suspects)
    row = src.execute("SELECT fg_app FROM samples WHERE id=?", (e["peak_sid"],)).fetchone()
    fg = row[0] if row else None
    text = explain(metric, e["base"], e["peak"], e["start_ts"], dur, parts, fg)
    cur = out.execute(
        "INSERT OR IGNORE INTO incidents "
        "(metric, start_ts, end_ts, duration, baseline, peak, suspects, fg_app, explanation) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (metric, e["start_ts"], e["last_high"], dur, e["base"], e["peak"],
         "; ".join(parts), fg, text))
    out.commit()
    if cur.rowcount:  # only print incidents we have not stored before
        print("\n" + text)


def main():
    if not schema_ok():
        print("monitor.db is missing or not upgraded yet.")
        print("Start the new recorder.py first (it upgrades the database), then run this again.")
        return

    out = sqlite3.connect(OUT_DB)
    init_out(out)

    history = {m: deque(maxlen=WINDOW) for m in METRICS}
    episode = {m: None for m in METRICS}
    last_id = 0

    print("Detector running. Press Ctrl+C to stop.")
    try:
        while True:
            try:
                src = sqlite3.connect(f"file:{SRC_DB}?mode=ro", uri=True, timeout=30)
                rows = src.execute(
                    "SELECT id, ts, cpu, mem, disk_mb, net_kb FROM samples "
                    "WHERE id > ? ORDER BY id", (last_id,)).fetchall()
            except sqlite3.OperationalError as err:
                print("Waiting for recorder data:", err)
                time.sleep(CHECK_EVERY)
                continue

            for sid, ts, cpu, mem, disk_mb, net_kb in rows:
                values = {"cpu": cpu, "mem": mem, "disk_mb": disk_mb, "net_kb": net_kb}
                for metric, cfg in METRICS.items():
                    v = values[metric]
                    if v is None:
                        continue
                    h, e = history[metric], episode[metric]

                    if e is None:
                        if len(h) >= BASELINE_MIN:
                            base = statistics.mean(h)
                            sd = statistics.pstdev(h)
                            if (v >= cfg["floor"] and v - base >= cfg["jump"]
                                    and v > base + SIGMA * sd):
                                episode[metric] = {
                                    "start_ts": ts, "base": base, "peak": v,
                                    "peak_sid": sid, "last_high": ts, "calm": 0}
                                continue
                        h.append(v)
                    else:
                        if v > e["peak"]:
                            e["peak"], e["peak_sid"] = v, sid
                        if v >= cfg["floor"]:
                            e["last_high"], e["calm"] = ts, 0
                        else:
                            e["calm"] += 1
                        if e["calm"] >= END_AFTER or ts - e["start_ts"] > MAX_LEN:
                            close_incident(src, out, metric, e)
                            episode[metric] = None
                            h.clear()
                last_id = sid

            src.close()
            time.sleep(CHECK_EVERY)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        out.close()


if __name__ == "__main__":
    main()