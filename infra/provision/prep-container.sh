#!/usr/bin/env bash
# Runs INSIDE a fresh unprivileged Debian 13 LXC before any provisioning. Fixes the gotchas found on
# PVE 9.2.2 with the debian-13-standard template (see docs/STATUS.md / ADR-0010):
#   1. /etc ships as 0700, so non-root users (postgres, _apt) can't read /etc/passwd → initdb and others
#      fail with "could not look up effective user ID: Permission denied". Make /etc traversable.
#   2. apt's download methods run as the unprivileged _apt user, which can't resolve DNS in an
#      unprivileged container → "Temporary failure resolving". Run them as root instead.
#   3. Force IPv4 for apt (the LAN's IPv6 path to mirrors is unreliable here).
#   4. Pin a reliable resolver; DHCP hands out a flaky one.
set -euo pipefail

chmod 755 /etc

printf 'nameserver 1.1.1.1\nnameserver 8.8.8.8\noptions single-request\n' > /etc/resolv.conf

mkdir -p /etc/apt/apt.conf.d
echo 'APT::Sandbox::User "root";' > /etc/apt/apt.conf.d/10no-sandbox
echo 'Acquire::ForceIPv4 "true";'  > /etc/apt/apt.conf.d/99force-ipv4

echo "prep-container: /etc traversable, apt sandbox off, IPv4 forced, resolver pinned"
