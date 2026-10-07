"""Moving the database from the laptop to the server, once (move_to_server.bat).

On the laptop, export_for_server():
* refuses to run while the logger is running;
* checks the database is intact;
* makes one complete copy (including any changes still in the write-ahead log) with SQLite's backup;
* records its checksum and the number of rows in every table.

On the server, import_from_laptop():
* checks the upload against that checksum and those row counts;
* refuses if the server already has a database, so a second move can never overwrite data the server
  has collected since;
* puts it in place.
"""
import hashlib
import json
import os
import sqlite3
import time

from . import config
from .runner import SingleInstanceLock

UPLOAD = "kalshi_upload.db"
MANIFEST = "kalshi_upload.json"


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def row_counts(conn):
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}


def _intact(conn):
    return conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def export_for_server(db_path=None, out_dir=None):
    db_path = db_path or config.DB_PATH
    out_dir = out_dir or config.DATA_DIR
    lock = SingleInstanceLock(config.PID_FILE)
    if not lock.acquire():
        print("The logger is still running on this laptop. Double-click stop.bat, wait 30 seconds, then try again.")
        return 1
    try:
        if not db_path.exists():
            print(f"No database found at {db_path}.")
            return 1
        print("Checking the database is intact (this can take a few minutes for a large database)...")
        src = sqlite3.connect(db_path)
        if not _intact(src):
            print("The database failed SQLite's integrity check. Nothing was copied. Please tell Claude.")
            return 1
        out = out_dir / UPLOAD
        if out.exists():
            out.unlink()
        print("Making one complete copy to upload...")
        dst = sqlite3.connect(out)
        src.backup(dst)
        dst.execute("PRAGMA journal_mode=DELETE")   # a single self-contained file
        before, after = row_counts(src), row_counts(dst)
        src.close()
        dst.close()
        if before != after:
            print("The copy doesn't match the original. Nothing was uploaded. Please tell Claude.")
            return 1
        manifest = {"created_ts": int(time.time()), "sha256": _sha256(out), "bytes": out.stat().st_size,
                    "rows": after}
        (out_dir / MANIFEST).write_text(json.dumps(manifest, indent=1), encoding="utf-8")
        print(f"Copy ready: {out.stat().st_size / 1e6:,.0f} MB, {sum(after.values()):,} rows in {len(after)} tables.")
        return 0
    finally:
        lock.handle.close()


def import_from_laptop(incoming_dir, db_path=None):
    db_path = db_path or config.DB_PATH
    up, man = incoming_dir / UPLOAD, incoming_dir / MANIFEST
    if not up.exists() or not man.exists():
        print("The upload isn't there. Run move_to_server.bat again.")
        return 1
    manifest = json.loads(man.read_text(encoding="utf-8"))
    lock = SingleInstanceLock(config.PID_FILE)
    if not lock.acquire():
        print("The server's logger is running; it must be stopped before the database is put in place.")
        return 1
    try:
        if db_path.exists():
            print(f"The server already has a database ({db_path}). Nothing was changed, so no data the server has")
            print("collected can be overwritten. If this is unexpected, please tell Claude.")
            return 1
        print("Checking the upload...")
        if up.stat().st_size != manifest["bytes"] or _sha256(up) != manifest["sha256"]:
            print("The upload was damaged on the way (checksum mismatch). Run move_to_server.bat again.")
            return 1
        conn = sqlite3.connect(up)
        ok, rows = _intact(conn), row_counts(conn)
        conn.close()
        if not ok or rows != manifest["rows"]:
            print("The upload doesn't match the laptop's database. Nothing was changed. Run move_to_server.bat again.")
            return 1
        db_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(up, db_path)
        man.unlink()
        print(f"Database in place: {sum(rows.values()):,} rows in {len(rows)} tables, matching the laptop exactly.")
        for t in ("scan_snapshots", "crypto_fv", "book_snapshots", "paper_orders", "calib_trades"):
            if t in rows:
                print(f"  {t}: {rows[t]:,}")
        return 0
    finally:
        lock.handle.close()
