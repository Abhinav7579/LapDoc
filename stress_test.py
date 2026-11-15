import multiprocessing as mp
import time

SECONDS = 40  # how long to keep the CPU busy


def burn():
    while True:
        pass


if __name__ == "__main__":
    print(f"Burning all CPU cores for {SECONDS} seconds...")
    workers = [mp.Process(target=burn) for _ in range(mp.cpu_count())]
    for w in workers:
        w.start()
    time.sleep(SECONDS)
    for w in workers:
        w.terminate()
    print("Done. Wait about 30 seconds for the detector to report it.")
