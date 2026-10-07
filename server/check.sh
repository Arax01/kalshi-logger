#!/usr/bin/env bash
# What server_status.bat shows: the service, the logger's own status, disk, backups and restarts.
APP_DIR="/home/kalshi/kalshi-logger"
echo "Server time: $(date '+%a %b %d %Y %H:%M %Z')   Up since: $(uptime -s)"
state=$(systemctl is-active kalshi-logger 2>/dev/null)
since=$(systemctl show -p ActiveEnterTimestamp --value kalshi-logger 2>/dev/null)
restarts=$(systemctl show -p NRestarts --value kalshi-logger 2>/dev/null)
echo "Logger service: ${state}${since:+ since ${since}} (automatic restarts since the last boot: ${restarts:-0})"
[ -f "${APP_DIR}/data/kalshi.db" ] || echo "  No database on the server yet (move_to_server.bat puts it in place)."
echo "Code version: $(git -C "${APP_DIR}" log --oneline -1)"
[ -f /var/run/reboot-required ] && echo "A security update will restart the server at 4 a.m. Pacific (the logger restarts itself)."
echo
cd "${APP_DIR}" && .venv/bin/python -m kalshi_logger status
