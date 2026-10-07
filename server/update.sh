#!/usr/bin/env bash
# Brings the server up to date with GitHub's main branch (update_server.bat runs this).
# The new version is tested first; if the tests fail, the server keeps running the old version.
set -euo pipefail

APP_DIR="/home/kalshi/kalshi-logger"
cd "${APP_DIR}"
old=$(git rev-parse HEAD)
git fetch --quiet origin main
new=$(git rev-parse origin/main)
if [ "${old}" = "${new}" ]; then
    echo "Already up to date ($(git log --oneline -1))."
    exit 0
fi
echo "Updating from $(git log --oneline -1 "${old}")"
echo "           to $(git log --oneline -1 "${new}")"
git merge --ff-only --quiet origin/main
.venv/bin/pip install --quiet -r requirements.txt
echo "Running the tests..."
if ! .venv/bin/python -m unittest discover -q tests; then
    echo
    echo "The new version's tests FAILED, so the server keeps running the old version. Please tell Claude."
    git reset --quiet --hard "${old}"
    .venv/bin/pip install --quiet -r requirements.txt
    exit 1
fi
changed_units=""
for f in kalshi-logger.service kalshi-backup.service kalshi-backup.timer; do
    cmp -s "server/${f}" "/etc/systemd/system/${f}" || changed_units="${changed_units} ${f}"
done
cmp -s server/add-key.sh /usr/local/bin/kalshi-add-key || changed_units="${changed_units} add-key.sh"
if [ -f data/kalshi.db ]; then
    sudo systemctl restart kalshi-logger
    echo "Restarted the logger on the new version."
fi
if [ -n "${changed_units}" ]; then
    echo
    echo "This update also changes server settings (${changed_units# }). To apply them, double-click"
    echo "setup_server.bat once (it's safe to run again)."
fi
