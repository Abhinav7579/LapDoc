import sqlite3
import time
import psutil

DB_PATH = "monitor.db"
INTERVAL = 1.0
TOP_N = 5
NCPU = psutil.cpu_count()

def init_db():
    conn = sqlite3.connect(DB_PATH)
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
    conn.commit()
    return conn

def top_processes(n=TOP_N):
    procs = []
    for p in psutil.process_iter(["pid", "name"]):
        try:
            cpu = p.cpu_percent(None) / NCPU   # normalize to 0-100
            mem = p.memory_info().rss / (1024 * 1024)
            procs.append((p.info["pid"], p.info["name"], cpu, mem))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    procs.sort(key=lambda x: x[2], reverse=True)
    return procs[:n]

def main():
    conn = init_db()

    # prime counters
    psutil.cpu_percent(None)
    for p in psutil.process_iter():
        try:
            p.cpu_percent(None)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

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

            procs = top_processes()

            cur = conn.execute(
                "INSERT INTO samples (ts, cpu, mem, disk_mb, net_kb) VALUES (?,?,?,?,?)",
                (now, cpu, mem, disk_mb, net_kb),
            )
            sid = cur.lastrowid
            conn.executemany(
                "INSERT INTO process_samples (sample_id, pid, name, cpu, mem_mb) VALUES (?,?,?,?,?)",
                [(sid, pid, name, c, m) for pid, name, c, m in procs],
            )
            conn.commit()

            time.sleep(max(0, INTERVAL - (time.time() - now)))
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        conn.close()

if __name__ == "__main__":
    main()