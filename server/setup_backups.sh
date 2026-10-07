#!/usr/bin/env bash
# Sets up the daily Backblaze B2 backup (setup_backups.bat runs this). Safe to run again, e.g. to change keys.
# On a replacement server, enter the SAME bucket, keys and backup password to reach your old backups.
set -euo pipefail

CONF_DIR="${HOME}/.config/kalshi-backup"
CONF="${CONF_DIR}/env"

echo "Backblaze B2 backup setup. Have these ready (SERVER.md, part 4):"
echo "  the bucket name, the application key ID (keyID), the application key, and a backup password."
echo "Nothing you type in the hidden fields is shown on screen; that's normal."
echo
read -r -p "Bucket name: " bucket
read -r -p "keyID: " key_id
read -r -s -p "applicationKey (hidden): " app_key; echo
while true; do
    read -r -s -p "Backup password (hidden; keep it in your password manager): " pw1; echo
    read -r -s -p "Type it again: " pw2; echo
    if [ -z "${pw1}" ]; then echo "The password can't be empty."; continue; fi
    if [ "${pw1}" != "${pw2}" ]; then echo "They don't match; try again."; continue; fi
    break
done

install -d -m 700 "${CONF_DIR}"
umask 077
{
    printf 'RESTIC_REPOSITORY=%q\n' "b2:${bucket}:kalshi-logger"
    printf 'B2_ACCOUNT_ID=%q\n' "${key_id}"
    printf 'B2_ACCOUNT_KEY=%q\n' "${app_key}"
    printf 'RESTIC_PASSWORD=%q\n' "${pw1}"
} > "${CONF}"
set -a
# shellcheck disable=SC1090
. "${CONF}"
set +a

echo
echo "Connecting to Backblaze..."
if restic cat config >/dev/null 2>&1; then
    echo "Found your existing backups in this bucket; the password matches."
else
    out=$(restic init 2>&1) || {
        echo "${out}"
        echo
        if echo "${out}" | grep -qi "already"; then
            echo "This bucket already holds backups but the password doesn't match. Run setup_backups.bat again with"
            echo "the original backup password."
        else
            echo "Couldn't reach the bucket. Check the bucket name and both keys, then run setup_backups.bat again."
        fi
        rm -f "${CONF}"
        exit 1
    }
    echo "Created a new, encrypted backup store in the bucket."
fi

echo
echo "Running the first backup now..."
"$(dirname "$0")/backup.sh"
echo
echo "Done. From now on a backup runs every day at 5 a.m. Pacific. server_status.bat shows the last one."
