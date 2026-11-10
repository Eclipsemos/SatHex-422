#!/bin/sh
# Run as root on the board after tests. Existing legacy files remain untouched.
set -eu
test "$(id -u)" = 0
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
test -f /etc/shanhe-serial.conf
test -f "$root/dist/gs422_2.2.0_all.deb"
stamp=$(date +%Y%m%d-%H%M%S)
mkdir -p /var/backups/gs422
cp -p /etc/shanhe-serial.conf "/var/backups/gs422/shanhe-serial.conf.$stamp"
if test -f /etc/gs422.conf; then
    cp -p /etc/gs422.conf "/var/backups/gs422/gs422.conf.$stamp"
fi
dpkg --force-confold -i "$root/dist/gs422_2.2.0_all.deb"
cp -p /etc/shanhe-serial.conf /etc/gs422.conf
chmod 644 /etc/gs422.conf
/usr/bin/gs422 --show-interfaces /etc/gs422.conf
systemd-analyze verify /lib/systemd/system/gs422.service
rollback() {
    systemctl disable --now gs422.service || true
    systemctl enable --now shanhe-serial.service
}
trap 'rollback' HUP INT TERM
systemctl disable --now rs422.service shanhe-serial.service
if ! systemctl enable --now gs422.service; then
    rollback
    exit 1
fi
sleep 4
if ! systemctl is-active --quiet gs422.service; then
    rollback
    exit 1
fi
trap - HUP INT TERM
systemctl is-enabled gs422.service
journalctl -u gs422.service -n 12 --no-pager
