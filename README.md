# Why is this slow?

A flight recorder for your Windows laptop. It records CPU, memory, disk, and network every second, notices when something spikes, and tells you in plain English which app probably caused it.

Task Manager shows you what is happening right now, but by the time you open it, the spike is over. This tool keeps the history, so you can ask "why was my laptop slow at 3:14 pm?" and get an answer.

```
CPU jumped to 94% at 15:14:07 (normally about 12%) and stayed high for 38s.
Most likely cause: SearchIndexer.exe (1% CPU to 41% CPU, newly active).
You were using: chrome.exe.
```

<!-- Add a screenshot of the dashboard here:
![Dashboard](screenshot.png)
-->

## Features

- **Always-on recorder:** system-wide stats plus the busiest processes by CPU, disk, and memory, once per second.
- **Spike detection:** compares each reading to a rolling 60-second baseline and groups a spike into one incident with a start time, duration, and peak.
- **Root-cause scoring:** ranks processes by how much they rose compared with their own normal level, not just by who is biggest.
- **Foreground app tracking:** records which window you were actually using.
- **Plain-English reports** for every incident.
- **Local dashboard:** live charts with incidents marked, an incident list, and top offenders by CPU time, disk activity, and time in front.
- **"Why was it slow?" time picker:** pick any moment and get a verdict, the busiest processes, and what you were using.
- **Automatic cleanup:** raw samples older than 7 days are deleted, so the database stays small.

Everything runs on your machine. Nothing is uploaded anywhere, and the dashboard only listens on `127.0.0.1`.

## How it works

1. `recorder.py` samples the system every second with [psutil](https://github.com/giampaolo/psutil) and saves it to a local SQLite database.
2. `detector.py` reads that data, keeps a rolling baseline for each metric, and flags a reading that is far above it. When the spike ends, it compares every process to its own usual level to find the likely cause, then writes a plain-English report.
3. `dashboard.py` is a small Flask app that reads both databases and draws the charts with Plotly.

## Requirements

- Windows 10 or 11
- Python 3.10 or newer

## Install

```
git clone <your-repo-url>
cd <repo-folder>
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

## Run

**Quick start (hidden, no windows):** double-click `start_monitor.vbs`, then open http://127.0.0.1:5000.
To stop everything, double-click `stop_monitor.bat`.

**Start automatically at login:** press `Win + R`, type `shell:startup`, and put a shortcut to `start_monitor.vbs` in that folder.

**Manual mode** (three terminals, each with the venv activated):

```
python recorder.py     # records data
python detector.py     # finds and explains incidents
python dashboard.py    # opens the dashboard in your browser
```

Start the recorder first. The detector needs about 30 seconds of data before it can spot anything.

## Try it

Run `python stress_test.py`. It uses every CPU core for 40 seconds. About 30 to 40 seconds after it ends, an incident naming `python.exe` appears in the detector and on the dashboard.

## Files

| File | What it does |
|---|---|
| `recorder.py` | Samples the system every second into `monitor.db` |
| `detector.py` | Finds incidents, scores suspects, writes `incidents.db` |
| `dashboard.py` | Local web dashboard (Flask and Plotly) |
| `stress_test.py` | Makes a CPU spike on purpose, for testing |
| `start_monitor.vbs` / `stop_monitor.bat` | Start and stop everything in the background |
| `requirements.txt` | Python dependencies |

## Tuning

Edit the settings at the top of `detector.py`.

- **Too many false alarms?** Raise `MIN_DURATION`, or raise the `floor` and `jump` values in `METRICS`.
- **Missing things you care about?** Lower the `floor` and `jump` values.
- **Keep data longer or shorter?** Change `RETENTION_DAYS` in `recorder.py`.

Restart the detector after changing anything.

## Privacy

`monitor.db` records which apps you ran and when. It never leaves your computer, and `.gitignore` keeps it out of the repository. Don't upload your `.db` files anywhere public.

## Limitations

- Windows only. Foreground detection uses the Windows API.
- Per-app network usage is not available through `psutil` on Windows, so network incidents are a best guess from CPU activity.
- Only the busiest processes are stored each second, so the offender lists are approximate and small consumers are missed.
- Total CPU is measured across all cores. One app maxing out a single core can make the laptop feel slow without raising total CPU enough to trigger an incident.
- GPU load is not measured.

## Roadmap

- Time-of-day baselines, so "normal" depends on the hour
- Trend prediction, for example catching a memory leak before memory fills up
- Battery-drain ranking over weeks
- Signed installer and system tray icon
- React dashboard with WebSockets

## License

MIT. Add a `LICENSE` file before publishing.
