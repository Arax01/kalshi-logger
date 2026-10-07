#!/usr/bin/env bash
# Daily backup (run by the kalshi-backup timer at 5 a.m., or by setup_backups.bat for the first one).
# Streams a consistent copy of the database (SQLite's .dump, which reads inside one transaction while the
# logger keeps running) into an encrypted restic repository on Backblaze B2. Only new data is uploaded.
# Keeps 7 daily, 4 weekly and 12 monthly backups. Needs no spare disk space on the server.
set -uo pipefail

APP_DIR="${APP_DIR:-/home/kalshi/kalshi-logger}"
CONF="${BACKUP_CONF:-${HOME}/.config/kalshi-backup/env}"
DB="${APP_DIR}/data/kalshi.db"
STATUS="${APP_DIR}/data/last_backup.txt"
stamp() { date '+%a %b %d %Y %H:%M %Z'; }

if [ ! -f "${CONF}" ]; then
    echo "Backups are not set up yet (setup_backups.bat)."
    [ -d "${APP_DIR}/data" ] && echo "$(stamp) - NOT SET UP: run setup_backups.bat" > "${STATUS}"
    exit 0
fi
if [ ! -f "${DB}" ]; then
    echo "No database on the server yet; nothing to back up."
    exit 0
fi
set -a
# shellcheck disable=SC1090
. "${CONF}"
set +a

start=$(date +%s)
if ! sqlite3 -readonly "${DB}" .dump | restic backup --stdin --stdin-filename kalshi.sql --tag daily --quiet; then
    echo "$(stamp) - FAILED. Details: journalctl -u kalshi-backup (or tell Claude)" > "${STATUS}"
    echo "Backup FAILED."
    exit 1
fi
note=""
# Tidy up old backups; the space is only reclaimed (pruned) once a week, on Sundays.
if [ "$(date +%u)" = 7 ]; then prune="--prune"; else prune=""; fi
# shellcheck disable=SC2086
if ! restic forget --quiet --tag daily --keep-daily 7 --keep-weekly 4 --keep-monthly 12 ${prune}; then
    note=" (but tidying up old backups failed; it will try again tomorrow)"
fi
mins=$(( ($(date +%s) - start + 59) / 60 ))
echo "$(stamp) - OK, took ${mins} min${note}" > "${STATUS}"
echo "Backup OK (${mins} min)${note}."
