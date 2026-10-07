#!/usr/bin/env bash
# Installed as /usr/local/bin/kalshi-add-key. Lets a new laptop key log in to the server.
# Run it as root from DigitalOcean's Droplet Console (SERVER.md, "Locked out").
#   kalshi-add-key 'ssh-ed25519 AAAA... kalshi-logger laptop key'             add a key
#   kalshi-add-key --replace 'ssh-ed25519 AAAA... kalshi-logger laptop key'   add it and remove all others
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Run this as root (the Droplet Console logs you in as root)."; exit 1; }
replace=0
if [ "${1:-}" = "--replace" ]; then replace=1; shift; fi
key="$*"
if [ -z "${key}" ] || ! printf '%s\n' "${key}" | ssh-keygen -l -f - >/dev/null 2>&1; then
    echo "That doesn't look like a public key. Paste the whole line, starting with ssh-ed25519, inside quotes."
    exit 1
fi
for f in /root/.ssh/authorized_keys /home/kalshi/.ssh/authorized_keys; do
    mkdir -p "$(dirname "${f}")"
    touch "${f}"
    if [ "${replace}" = 1 ]; then
        printf '%s\n' "${key}" > "${f}"
    elif ! grep -qxF "${key}" "${f}"; then
        printf '%s\n' "${key}" >> "${f}"
    fi
    chmod 600 "${f}"
done
chown -R kalshi:kalshi /home/kalshi/.ssh
echo "Done. Keys that can now log in:"
ssh-keygen -l -f /home/kalshi/.ssh/authorized_keys
