import ctypes
import os
import sqlite3
import time
from ctypes import wintypes

import psutil

DB_PATH = "monitor.db"
INTERVAL = 1.0
TOP_CPU = 5   # top processes by CPU stored each second
TOP_IO = 3    # plus top processes by disk activity
TOP_MEM = 3   # plus top processes by memory
NCPU = psutil.cpu_count()
ME = os.getpid()
IGNORE_NAMES = {"System Idle Process"}

user32 = ctypes.windll.user32
user32.GetForegroundWindow.restype = wintypes.HWND


def foreground_app():
    """Name of the program whose window you are currently using."""
    try:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value:
            return psutil.Process(pid.value).name()
    except Exception:
        pass
    return None


def add_column(conn, table, col, decl):
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    if col not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def init_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            cpu REAL, mem REAL, disk_mb REAL, net_kb REAL
        );
        CREATE TABLE IF NOT EXISTS process_samples (
            sample_id INTEGER NOT NULL,
            pid INTEGER, name TEXT,
            cpu REAL, mem_mb REAL,
            FOREIGN KEY(sample_id) REFERENCES samples(id)
        );
        CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts);
        CREATE INDEX IF NOT EXISTS idx_proc_sample ON process_samples(sample_id);
    """)
    # upgrades for databases created by the Day 2 recorder
    add_column(conn, "samples", "fg_app", "TEXT")
    add_column(conn, "process_samples", "io_kb", "REAL")
    conn.commit()
    return conn


def sample_processes(prev_io, dt):
    rows = []
    new_io = {}
    attrs = ["pid", "name", "cpu_percent", "memory_info", "io_counters"]
    for p in psutil.process_iter(attrs):
        info = p.info
        pid, name = info["pid"], info["name"]
        if pid in (0, ME) or name is None or name in IGNORE_NAMES:
            continue
        cpu = (info["cpu_percent"] or 0) / NCPU
        mi = info["memory_info"]
        mem = mi.rss / (1024 * 1024) if mi else 0
        io_kb = 0
        ioc = info["io_counters"]
        if ioc:
            total = ioc.read_bytes + ioc.write_bytes
            new_io[pid] = total
            if pid in prev_io and dt > 0:
                io_kb = max(0, total - prev_io[pid]) / dt / 1024
        rows.append((pid, name, cpu, mem, io_kb))
    return rows, new_io


def pick(rows):
    chosen = {}
    for key, n in ((2, TOP_CPU), (4, TOP_IO), (3, TOP_MEM)):
        for r in sorted(rows, key=lambda r: r[key], reverse=True)[:n]:
            if r[key] > 0:
                chosen[r[0]] = r
    return list(chosen.values())


def main():
    conn = init_db()

    psutil.cpu_percent(None)
    _, prev_io = sample_processes({}, 1)  # prime the counters
    prev_disk = psutil.disk_io_counters()
    prev_net = psutil.net_io_counters()
    prev_t = time.time()
    time.sleep(INTERVAL)

    print("Recording to", DB_PATH, "- press Ctrl+C to stop")
    try:
        while True:
            now = time.time()
            dt = now - prev_t

            cpu = psutil.cpu_percent(None)
            mem = psutil.virtual_memory().percent

            disk = psutil.disk_io_counters()
            disk_mb = (disk.read_bytes + disk.write_bytes
                       - prev_disk.read_bytes - prev_disk.write_bytes) / dt / (1024 * 1024)

            net = psutil.net_io_counters()
            net_kb = (net.bytes_sent + net.bytes_recv
                      - prev_net.bytes_sent - prev_net.bytes_recv) / dt / 1024

            prev_disk, prev_net, prev_t = disk, net, now

            rows, prev_io = sample_processes(prev_io, dt)
            procs = pick(rows)
            fg = foreground_app()

            try:
                cur = conn.execute(
                    "INSERT INTO samples (ts, cpu, mem, disk_mb, net_kb, fg_app) VALUES (?,?,?,?,?,?)",
                    (now, cpu, mem, disk_mb, net_kb, fg),
                )
                sid = cur.lastrowid
                conn.executemany(
                    "INSERT INTO process_samples (sample_id, pid, name, cpu, mem_mb, io_kb) "
                    "VALUES (?,?,?,?,?,?)",
                    [(sid, pid, name, c, m, io) for pid, name, c, m, io in procs],
                )
                conn.commit()
            except sqlite3.OperationalError as e:
                print("DB busy, skipping one sample:", e)
                conn.rollback()

            time.sleep(max(0, INTERVAL - (time.time() - now)))
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()