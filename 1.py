import time
import psutil

def top_processes(n=5):
    procs = []
    for p in psutil.process_iter(["pid", "name"]):
        try:
            # cpu_percent needs a previous call to measure against
            cpu = p.cpu_percent(None)
            mem = p.memory_info().rss / (1024 * 1024)
            procs.append((p.info["name"], p.info["pid"], cpu, mem))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    procs.sort(key=lambda x: x[2], reverse=True)
    return procs[:n]

def main():
    psutil.cpu_percent(None)  # prime the counters
    for p in psutil.process_iter():
        try:
            p.cpu_percent(None)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    prev_disk = psutil.disk_io_counters()
    prev_net = psutil.net_io_counters()
    prev_t = time.time()
    time.sleep(1)

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

        print(f"\n[{time.strftime('%H:%M:%S')}] CPU {cpu:5.1f}%  MEM {mem:5.1f}%  "
              f"DISK {disk_mb:6.2f} MB/s  NET {net_kb:8.1f} KB/s")
        for name, pid, c, m in top_processes():
            print(f"    {name:<28} pid {pid:<6} cpu {c:5.1f}%  mem {m:7.1f} MB")

        time.sleep(1)

if __name__ == "__main__":
    main()