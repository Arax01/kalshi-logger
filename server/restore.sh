#!/usr/bin/env bash
# Restores the database from a backup (restore_backup.bat runs this). Works on this server or on a brand-new
# one after setup_server.bat and setup_backups.bat (with the same bucket, keys and password).
set -euo pipefail

APP_DIR="/home/kalshi/kalshi-logger"
CONF="${HOME}/.config/kalshi-backup/env"
DATA="${APP_DIR}/data"
DB="${DATA}/kalshi.db"
NEW="${DATA}/kalshi.restoring.db"

[ -f "${CONF}" ] || { echo "Backups aren't set up on this server yet. Run setup_backups.bat first."; exit 1; }
set -a
# shellcheck disable=SC1090
. "${CONF}"
set +a

echo "Backups available (newest last):"
restic snapshots --compact --tag daily
echo
read -r -p "Type the ID of the backup to restore, or just press Enter for the newest: " snap
snap="${snap:-latest}"

keep_old=1
if [ -f "${DB}" ]; then
    need=$(( $(du -sb "${DB}" | cut -f1) * 12 / 10 ))
    free=$(df --output=avail -B1 "${DATA}" | tail -1)
    echo
    echo "The server's current database will be stopped and set aside (not deleted)."
    if [ "${free}" -lt $(( need * 2 )) ]; then
        echo "There isn't enough free disk to keep the current database AND restore the backup."
        read -r -p "Type DELETE to replace the current database without keeping it, or anything else to stop: " ans
        [ "${ans}" = DELETE ] || { echo "Stopped; nothing changed."; exit 1; }
        keep_old=0
    fi
fi
read -r -p "Restore now? The logger stops for the restore (data from the gap shows as a gap). Type YES: " ans
[ "${ans}" = YES ] || { echo "Stopped; nothing changed."; exit 1; }

sudo systemctl stop kalshi-logger
if [ "${keep_old}" = 0 ]; then
    rm -f "${DB}" "${DB}-wal" "${DB}-shm"
fi
rm -f "${NEW}"
echo "Restoring (large databases take a while; about 1 minute per GB)..."
restic dump "${snap}" kalshi.sql | sqlite3 "${NEW}"
if [ "$(sqlite3 "${NEW}" 'PRAGMA integrity_check')" != ok ]; then
    echo "The restored copy failed its integrity check. Nothing was replaced. Please tell Claude."
    rm -f "${NEW}"
    [ -f "${DB}" ] && sudo systemctl start kalshi-logger
    exit 1
fi
if [ -f "${DB}" ]; then
    aside="${DATA}/kalshi.db.before-restore-$(date +%Y%m%d-%H%M)"
    mv "${DB}" "${aside}"
    for ext in wal shm; do [ -f "${DB}-${ext}" ] && mv "${DB}-${ext}" "${aside}-${ext}"; done
    echo "The previous database was kept as ${aside}. Delete it once you're happy (it uses disk space)."
fi
mv "${NEW}" "${DB}"
sudo systemctl start kalshi-logger
echo
echo "Restored. Rows in the main tables:"
for t in scan_snapshots crypto_fv book_snapshots paper_orders calib_trades; do
    printf '  %s: %s\n' "${t}" "$(sqlite3 "${DB}" "SELECT COUNT(*) FROM ${t}" 2>/dev/null || echo n/a)"
done
