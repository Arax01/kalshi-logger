"""Command line entry point.

    python -m kalshi_logger run        start logging (what start.bat does)
    python -m kalshi_logger stop       ask a running logger to stop (what stop.bat does)
    python -m kalshi_logger status     show what has been collected and any data gaps
    python -m kalshi_logger report     write any due reports, plus a preview of the latest data
    python -m kalshi_logger once JOB   run one job once (scanner, crypto, ingame, results)
"""
import sys

from . import config


def build_jobs():
    from . import crypto, ingame, reports, results, scanner
    from .runner import Job
    return [
        Job("scanner", config.SCAN_INTERVAL_SEC, scanner.run_scan),
        Job("crypto", config.CRYPTO_INTERVAL_SEC, crypto.run),
        Job("ingame", config.INGAME_INTERVAL_SEC, ingame.run),
        Job("results", config.RESULTS_INTERVAL_SEC, results.run),
        Job("reports", config.REPORT_CHECK_INTERVAL_SEC, reports.run_due),
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
        from . import db, reports
        db.init()
        print("Checking for final results and writing any due reports...")
        paths = reports.run_due() + reports.preview(db.connect())
        for path in paths:
            print("Wrote", path)
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
