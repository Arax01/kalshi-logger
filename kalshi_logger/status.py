"""Human-readable status: is it running, what has been collected, any gaps or errors."""
import time
from datetime import datetime

from . import config, db
from .runner import SingleInstanceLock


def _ago(ts):
    if not ts:
        return "never"
    sec = time.time() - ts
    if sec < 120:
        return f"{sec:.0f} seconds ago"
    if sec < 7200:
        return f"{sec / 60:.0f} minutes ago"
    return f"{sec / 3600:.1f} hours ago"


def print_status():
    lock = SingleInstanceLock(config.PID_FILE)
    running = not lock.acquire()
    if lock.handle:
        lock.handle.close()
    print(f"Logger is {'RUNNING' if running else 'NOT running'}.")
    if not config.DB_PATH.exists():
        print("No data collected yet.")
        return
    db.init()
    conn = db.connect()
    size = sum(p.stat().st_size for p in config.DB_PATH.parent.glob(config.DB_PATH.name + "*"))
    print(f"Database: {config.DB_PATH} ({size / 1e6:,.0f} MB)\n")
    print("Last successful run of each job:")
    for r in conn.execute("SELECT * FROM job_state ORDER BY job"):
        err = f"   last error: {r['last_error']}" if r["last_error"] else ""
        print(f"  {r['job']:<8} {_ago(r['last_ok_ts'])}{err}")
    day = time.time() - 86400
    q = lambda sql: conn.execute(sql, (day,)).fetchone()[0]
    print("\nLast 24 hours:")
    counts = [
        ("Scans completed", "SELECT COUNT(*) FROM scans WHERE started_ts > ? AND status='ok'"),
        ("Market snapshots", "SELECT COUNT(*) FROM scan_snapshots WHERE ts > ?"),
        ("Crypto fair values", "SELECT COUNT(*) FROM crypto_fv WHERE ts > ?"),
        ("In-game snapshots", "SELECT COUNT(*) FROM game_snapshots WHERE ts > ?"),
    ]
    for label, sql in counts:
        print(f"  {label + ':':<24}{q(sql):,}")
    gaps = conn.execute(
        "SELECT * FROM gaps WHERE end_ts > ? ORDER BY start_ts DESC LIMIT 10", (time.time() - 7 * 86400,)).fetchall()
    print("\nData gaps in the last 7 days:" if gaps else "\nNo data gaps in the last 7 days.")
    for g in gaps:
        print(f"  {g['job']:<8} {datetime.fromtimestamp(g['start_ts']):%a %b %d %H:%M} -> "
              f"{datetime.fromtimestamp(g['end_ts']):%a %b %d %H:%M}  {g['reason']}")
    reps = conn.execute(
        "SELECT * FROM reports_done WHERE report_type NOT LIKE '%preview' ORDER BY generated_ts DESC LIMIT 5").fetchall()
    if reps:
        print("\nLatest reports (in the reports folder):")
        for r in reps:
            print(f"  {r['path']}")
