"""Runs the logging jobs on their schedules, detects gaps, and handles start/stop."""
import logging
import logging.handlers
import os
import signal
import sys
import threading
import time

from . import config, db

log = logging.getLogger("kalshi_logger")

stop_event = threading.Event()
HEARTBEAT_SEC = 60
# Set at start-up from the previous run: when it was last known alive, and whether it stopped cleanly.
_prev_run = {"end": None, "clean": True}
_suspend_lock = threading.Lock()
_last_suspend = {"start": None, "end": None}


def setup_logging(console=True):
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = logging.handlers.RotatingFileHandler(
        config.LOG_DIR / "logger.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if console:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(sh)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def sleep_until(deadline):
    """Sleep in 1-second steps so stop requests and laptop sleep are noticed promptly.

    Returns early if a stop is requested. If one 1-second step takes much longer in wall-clock
    time, the computer was suspended; that is remembered so gaps can be labelled correctly.
    """
    while not stop_event.is_set():
        now = time.time()
        if now >= deadline:
            return
        before = time.time()
        stop_event.wait(min(1.0, deadline - now))
        after = time.time()
        if after - before > 30:
            with _suspend_lock:
                _last_suspend["start"], _last_suspend["end"] = before, after
            log.info("Computer appears to have been asleep for %.0f minutes", (after - before) / 60)
        if config.STOP_FILE.exists():
            stop_event.set()


def _gap_reason(gap_start, gap_end, process_start, last_error):
    with _suspend_lock:
        s, e = _last_suspend["start"], _last_suspend["end"]
    if s and e and s < gap_end and e > gap_start:
        return "computer asleep"
    if process_start > gap_start:
        if not _prev_run["clean"]:
            return "logger was closed without stop.bat (window closed, crash or power loss)"
        return "logger not running (stopped, or computer off/asleep)"
    if last_error:
        return f"errors: {last_error[:200]}"
    return "job overran or network unavailable"


class Job:
    def __init__(self, name, interval, func, run_at_start=True):
        self.name = name
        self.interval = interval
        self.func = func
        self.run_at_start = run_at_start

    def loop(self, process_start):
        conn = db.connect()
        next_run = time.time() if self.run_at_start else time.time() + self.interval
        while not stop_event.is_set():
            sleep_until(next_run)
            if stop_event.is_set():
                break
            started = time.time()
            row = conn.execute(
                "SELECT last_ok_ts, last_error FROM job_state WHERE job=?", (self.name,)
            ).fetchone()
            last_ok = row["last_ok_ts"] if row else None
            last_error = row["last_error"] if row else None
            if last_ok is None and _prev_run["end"]:
                # This job never finished a run before the logger last stopped (e.g. it was killed
                # during the first scan); measure the gap from when the previous run was last alive.
                last_ok = _prev_run["end"]
            after_gap = bool(last_ok and started - last_ok > config.GAP_FACTOR * self.interval)
            try:
                self.func(after_gap=after_gap)
            except Exception as exc:  # keep running; record the failure
                log.exception("Job %s failed", self.name)
                conn.rollback()
                conn.execute(
                    "INSERT INTO job_state(job, last_attempt_ts, last_error) VALUES(?,?,?) "
                    "ON CONFLICT(job) DO UPDATE SET last_attempt_ts=excluded.last_attempt_ts, "
                    "last_error=excluded.last_error",
                    (self.name, int(started), f"{type(exc).__name__}: {exc}"[:500]),
                )
                conn.commit()
                # Retry sooner than a full interval, but not in a tight loop.
                next_run = time.time() + min(self.interval, 120)
                continue
            if after_gap:
                reason = _gap_reason(last_ok, started, process_start, last_error)
                conn.execute(
                    "INSERT INTO gaps(job, start_ts, end_ts, reason) VALUES(?,?,?,?)",
                    (self.name, int(last_ok), int(started), reason),
                )
                log.info("Recorded %s data gap of %.0f min (%s)", self.name, (started - last_ok) / 60, reason)
            conn.execute(
                "INSERT INTO job_state(job, last_ok_ts, last_attempt_ts, last_error) VALUES(?,?,?,NULL) "
                "ON CONFLICT(job) DO UPDATE SET last_ok_ts=excluded.last_ok_ts, "
                "last_attempt_ts=excluded.last_attempt_ts, last_error=NULL",
                (self.name, int(started), int(started)),
            )
            conn.commit()
            # Schedule from the planned time so runs don't drift; skip missed slots after a gap.
            next_run += self.interval
            if next_run < time.time():
                next_run = time.time() + self.interval


class SingleInstanceLock:
    """Prevents two loggers writing to the same database at once."""

    def __init__(self, path):
        self.path = path
        self.handle = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            self.handle = None
            return False
        return True


def run(jobs):
    if config.MOVED_FILE.exists():
        print("This copy of the logger has been moved to the server (see SERVER.md), so it won't start here.")
        print("Use server_status.bat to check on the server. To log on this computer again anyway, delete the")
        print(f"file {config.MOVED_FILE.name} in this folder first - but never run both at once.")
        return 1
    if config.STOP_FILE.exists():
        config.STOP_FILE.unlink()
    lock = SingleInstanceLock(config.PID_FILE)
    if not lock.acquire():
        print("The logger is already running. Use stop.bat to stop it first.")
        return 1
    db.init()
    process_start = time.time()
    conn = db.connect()
    run_id = _recover_previous_run(conn, int(process_start))
    # The server's service manager stops the logger with SIGTERM: treat it like stop.bat.
    try:
        signal.signal(signal.SIGTERM, lambda signum, frame: stop_event.set())
    except (ValueError, AttributeError):
        pass
    log.info("Logger started (read-only). Database: %s", config.DB_PATH)
    threads = []
    for job in jobs:
        t = threading.Thread(target=job.loop, args=(process_start,), name=job.name, daemon=True)
        t.start()
        threads.append(t)
    last_beat = 0.0
    try:
        while not stop_event.is_set():
            if time.time() - last_beat >= HEARTBEAT_SEC:
                last_beat = time.time()
                conn.execute("UPDATE process_runs SET heartbeat_ts=? WHERE run_id=?", (int(last_beat), run_id))
                conn.commit()
            sleep_until(time.time() + 5)
    except KeyboardInterrupt:
        stop_event.set()
    log.info("Stopping: waiting for jobs to finish their current step...")
    for t in threads:
        t.join(timeout=120)
    conn.execute("UPDATE process_runs SET heartbeat_ts=?, ended_ts=?, clean=1 WHERE run_id=?",
                 (int(time.time()), int(time.time()), run_id))
    conn.commit()
    if config.STOP_FILE.exists():
        config.STOP_FILE.unlink()
    log.info("Logger stopped.")
    return 0


def _recover_previous_run(conn, now):
    """Tidy up after a previous run that was killed rather than stopped, and start a new run record.

    SQLite keeps every committed write when a process is killed, so the data itself is safe. What a
    kill leaves behind is bookkeeping: the run is never marked as ended, and a scan in progress is
    left marked 'running'. Both are fixed here, and the time the previous run was last alive is
    remembered so the downtime is recorded as a gap like any other.
    """
    prev = conn.execute("SELECT * FROM process_runs ORDER BY run_id DESC LIMIT 1").fetchone()
    if prev:
        _prev_run["end"] = prev["ended_ts"] or prev["heartbeat_ts"] or prev["started_ts"]
        _prev_run["clean"] = bool(prev["clean"])
        if prev["ended_ts"] is None:
            conn.execute("UPDATE process_runs SET ended_ts=?, clean=0 WHERE run_id=?",
                         (_prev_run["end"], prev["run_id"]))
            log.info("The previous run was not stopped with stop.bat (last alive %s); "
                     "its data is kept and the downtime will be recorded as a gap.",
                     time.strftime("%Y-%m-%d %H:%M", time.localtime(_prev_run["end"])))
    n = conn.execute(
        "UPDATE scans SET status='interrupted', finished_ts=COALESCE("
        "(SELECT MAX(ts) FROM scan_snapshots s WHERE s.scan_id=scans.scan_id), started_ts) "
        "WHERE status='running'").rowcount
    if n:
        log.info("Marked %d unfinished scan(s) as interrupted", n)
    run_id = conn.execute("INSERT INTO process_runs(started_ts, heartbeat_ts, clean) VALUES(?,?,0)",
                          (now, now)).lastrowid
    conn.commit()
    return run_id


def request_stop():
    config.STOP_FILE.write_text("stop requested\n")
