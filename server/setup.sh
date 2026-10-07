#!/usr/bin/env bash
# One-time setup of the Kalshi logger server. setup_server.bat copies this to the server and runs it as
# root. It is safe to run again (for example after an update that changes the server settings).
#
# What it does: Pacific time; system packages; SSH key login only; a firewall with only SSH open;
# automatic security updates; swap; a 'kalshi' user that runs the logger; read-only GitHub access to
# download the code; tests and a live check; the logger service and the daily backup timer.
# The logger itself only starts once a database is in place (move_to_server.bat or restore_backup.bat).
set -euo pipefail

REPO="git@github.com:Arax01/kalshi-logger.git"
TZ_NAME="America/Los_Angeles"
APP_USER="kalshi"
APP_HOME="/home/${APP_USER}"
APP_DIR="${APP_HOME}/kalshi-logger"
# GitHub's published SSH host key (fingerprint SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU).
GITHUB_HOST_KEY="github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"

step() { echo; echo "=== $* ==="; }
as_app() { sudo -u "${APP_USER}" -H "$@"; }

[ "$(id -u)" = 0 ] || { echo "This must run as root (setup_server.bat does that)."; exit 1; }
. /etc/os-release
[ "${ID}" = ubuntu ] || { echo "This script expects Ubuntu (the server should be Ubuntu 24.04)."; exit 1; }
if [ ! -s /root/.ssh/authorized_keys ]; then
    echo "No SSH key is set up for root, so turning off passwords would lock you out. Stopping."
    echo "Create the server with your SSH key (SERVER.md, part 2)."
    exit 1
fi

step "1/9 Setting the clock to Pacific time"
timedatectl set-timezone "${TZ_NAME}"
date

step "2/9 Installing system updates and packages (a few minutes)"
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a
apt-get update -q
apt-get -y -q -o Dpkg::Options::=--force-confold upgrade
apt-get -y -q install python3 python3-venv git sqlite3 restic ufw unattended-upgrades

step "3/9 SSH: key login only, no passwords"
mkdir -p /etc/ssh/sshd_config.d
cat > /etc/ssh/sshd_config.d/00-kalshi.conf <<'CONF'
# Kalshi logger server: log in with an SSH key only (never a password).
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
CONF
sshd -t
systemctl reload ssh 2>/dev/null || systemctl restart ssh

step "4/9 Firewall: nothing open except SSH"
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw --force enable
ufw status verbose | head -8

step "5/9 Automatic security updates (restarting at 4 a.m. Pacific when needed)"
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'CONF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
CONF
cat > /etc/apt/apt.conf.d/52kalshi-upgrades <<'CONF'
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "04:00";
CONF
systemctl enable --now unattended-upgrades

step "6/9 Swap space (lets the occasional big report run on a 1 GB server) and log size limits"
if ! swapon --show=NAME --noheadings | grep -qx /swapfile; then
    fallocate -l 2G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nSystemMaxUse=200M\n' > /etc/systemd/journald.conf.d/kalshi.conf
systemctl restart systemd-journald
free -h | head -3

step "7/9 The 'kalshi' user and read-only GitHub access"
id "${APP_USER}" >/dev/null 2>&1 || adduser --disabled-password --gecos "" "${APP_USER}"
install -d -m 700 -o "${APP_USER}" -g "${APP_USER}" "${APP_HOME}/.ssh"
touch "${APP_HOME}/.ssh/authorized_keys"
cat /root/.ssh/authorized_keys "${APP_HOME}/.ssh/authorized_keys" | grep -v '^\s*$' | sort -u > /tmp/kalshi-keys
install -m 600 -o "${APP_USER}" -g "${APP_USER}" /tmp/kalshi-keys "${APP_HOME}/.ssh/authorized_keys"
rm -f /tmp/kalshi-keys
if [ ! -f "${APP_HOME}/.ssh/github_deploy" ]; then
    as_app ssh-keygen -q -t ed25519 -N "" -C "kalshi-logger server (read-only deploy key)" \
        -f "${APP_HOME}/.ssh/github_deploy"
fi
echo "${GITHUB_HOST_KEY}" > "${APP_HOME}/.ssh/known_hosts_github"
cat > "${APP_HOME}/.ssh/config" <<'CONF'
Host github.com
    IdentityFile ~/.ssh/github_deploy
    IdentitiesOnly yes
    UserKnownHostsFile ~/.ssh/known_hosts_github
    StrictHostKeyChecking yes
CONF
chown "${APP_USER}:${APP_USER}" "${APP_HOME}/.ssh/config" "${APP_HOME}/.ssh/known_hosts_github"
chmod 600 "${APP_HOME}/.ssh/config"
until as_app git ls-remote "${REPO}" >/dev/null 2>&1; do
    echo
    echo "The server needs read-only access to your GitHub repository. Add this as a deploy key:"
    echo "  github.com > Arax01/kalshi-logger > Settings > Deploy keys > Add deploy key"
    echo "  Title: kalshi server.  Key: the line below.  Leave 'Allow write access' UNTICKED."
    echo
    cat "${APP_HOME}/.ssh/github_deploy.pub"
    echo
    read -r -p "Press Enter once you've added it (or Ctrl+C to stop and come back later)... " _
done
echo "GitHub access works."
if [ -d "${APP_DIR}/.git" ]; then
    as_app git -C "${APP_DIR}" fetch --quiet origin main
    as_app git -C "${APP_DIR}" merge --ff-only --quiet origin/main
else
    as_app git clone --quiet --branch main "${REPO}" "${APP_DIR}"
fi
as_app git -C "${APP_DIR}" log --oneline -1
if [ ! -f "${APP_DIR}/server/kalshi-logger.service" ]; then
    echo "GitHub's main branch doesn't have the server files yet. Merge the server pull request on GitHub"
    echo "first, then double-click setup_server.bat again."
    exit 1
fi

step "8/9 Python environment, tests and a live check against Kalshi"
[ -x "${APP_DIR}/.venv/bin/python" ] || as_app python3 -m venv "${APP_DIR}/.venv"
as_app "${APP_DIR}/.venv/bin/pip" install --quiet --upgrade pip
as_app "${APP_DIR}/.venv/bin/pip" install --quiet -r "${APP_DIR}/requirements.txt"
(cd "${APP_DIR}" && as_app .venv/bin/python -m unittest discover -q tests)
(cd "${APP_DIR}" && as_app .venv/bin/python -m kalshi_logger check)

step "9/9 Logger service, daily backup timer and helper commands"
install -m 644 "${APP_DIR}"/server/kalshi-logger.service "${APP_DIR}"/server/kalshi-backup.service \
    "${APP_DIR}"/server/kalshi-backup.timer /etc/systemd/system/
install -m 755 "${APP_DIR}/server/add-key.sh" /usr/local/bin/kalshi-add-key
cat > /etc/sudoers.d/kalshi <<'CONF'
# The kalshi user may start, stop and restart the logger and run a backup, and nothing else as root.
kalshi ALL=(root) NOPASSWD: /usr/bin/systemctl start kalshi-logger, /usr/bin/systemctl stop kalshi-logger, /usr/bin/systemctl restart kalshi-logger, /usr/bin/systemctl start kalshi-backup.service
CONF
chmod 440 /etc/sudoers.d/kalshi
visudo -cf /etc/sudoers.d/kalshi
systemctl daemon-reload
systemctl enable kalshi-logger.service kalshi-backup.timer
systemctl start kalshi-backup.timer
if [ -f "${APP_DIR}/data/kalshi.db" ]; then
    systemctl restart kalshi-logger.service
    echo "The logger is running."
else
    echo "The logger will start as soon as a database is in place (move_to_server.bat)."
fi

echo
echo "=============================================================="
echo " Server setup finished. Next: SERVER.md, part 4 (backups) and part 5 (moving your data)."
echo "=============================================================="
