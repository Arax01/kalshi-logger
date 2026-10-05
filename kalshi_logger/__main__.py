"""Command line entry point.

    python -m kalshi_logger run        start logging (what start.bat does)
    python -m kalshi_logger stop       ask a running logger to stop (what stop.bat does)
    python -m kalshi_logger status     show what has been collected and any data gaps
    python -m kalshi_logger report     write any due reports, plus a preview of the latest data
    python -m kalshi_logger once JOB   run one job once (scanner, crypto, ingame, results, books)
    python -m kalshi_logger backfill   download 2026 football games, then write the overreaction report
    python -m kalshi_logger overreaction   rewrite the overreaction report from backfilled data
    python -m kalshi_logger calibration    sample historical trades (resumable), then write the calibration study
    python -m kalshi_logger calibration --rerun   pre-registered clean re-test (see docs/preregistration-...)
"""
import sys

from . import config


def build_jobs():
    from . import books, crypto, ingame, reports, results, scanner
    from .runner import Job
    return [
        Job("scanner", config.SCAN_INTERVAL_SEC, scanner.run_scan),
        Job("crypto", config.CRYPTO_INTERVAL_SEC, crypto.run),
        Job("ingame", config.INGAME_INTERVAL_SEC, ingame.run),
        Job("results", config.RESULTS_INTERVAL_SEC, results.run),
        Job("reports", config.REPORT_CHECK_INTERVAL_SEC, reports.run_due),
        Job("books", config.BOOKS_INTERVAL_SEC, books.run),
    ]


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "run"
    from . import runner
    if cmd == "run":
        runner.setup_logging()
        return runner.run(build_jobs())
    if cmd == "stop":
        runner.request_stop()
        print("Stop requested. The logger will finish its current step and exit (usually within a minute).")
        return 0
    if cmd == "status":
        from .status import print_status
        print_status()
        return 0
    if cmd == "report":
        runner.setup_logging(console=False)
        from . import db, reports, results
        db.init()
        print("Checking for final results and writing any due reports...")
        results.run()
        paths = reports.run_due() + reports.preview(db.connect())
        for path in paths:
            print("Wrote", path)
        return 0
    if cmd in ("backfill", "overreaction"):
        runner.setup_logging(console=False)
        from . import backfill, db, overreaction
        db.init()
        if cmd == "backfill":
            print("Downloading 2026 NFL and college football games from Kalshi (read-only)...")
            backfill.run()
        path = overreaction.write_report(db.connect())
        print("Wrote", path)
        return 0
    if cmd == "calibration":
        runner.setup_logging(console=False)
        from . import calib_pull, calib_report, db
        db.init()
        if "--rerun" in argv:
            from . import calib_rerun
            if "--report-only" not in argv:
                print("Extending the sample with fresh trades from October 6, 2026 (read-only, resumable)...")
                calib_rerun.pull(db.connect())
            print("Wrote", calib_rerun.write_report(db.connect()))
            return 0
        if "--report-only" not in argv:
            print("Sampling historical Kalshi trades (read-only). This takes 1.5-2 hours the first time and can be")
            print("stopped and restarted; it picks up where it left off.")
            calib_pull.run(db.connect())
        print("Wrote", calib_report.write_report(db.connect()))
        return 0
    if cmd == "once":
        runner.setup_logging()
        from . import db
        db.init()
        job = {j.name: j for j in build_jobs()}[argv[2]]
        job.func(after_gap=False)
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
