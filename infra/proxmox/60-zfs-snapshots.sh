#!/usr/bin/env bash
# Step 60 — Local recovery snapshots for rpool/data (PAT-009, ADR-0009): hourly×48, daily×14, weekly×8.
# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"
STEP=60
DATASET="${DATASET:-rpool/data}"
check() { systemctl is-active --quiet pat-zfs-snap-hourly.timer && systemctl is-active --quiet pat-zfs-snap-daily.timer; }
apply() {
  require_root; ensure_dirs
  zfs list "$DATASET" >/dev/null 2>&1 || die "dataset $DATASET not found"
  install -m 0755 /dev/stdin /usr/local/sbin/pat-zfs-snap <<'EOT'
#!/usr/bin/env bash
# pat-zfs-snap LABEL KEEP [DATASET] — recursive snapshot + prune oldest beyond KEEP
set -euo pipefail
label="$1"; keep="$2"; ds="${3:-rpool/data}"
zfs snapshot -r "${ds}@pat-${label}-$(date +%Y%m%d-%H%M)"
zfs list -H -t snapshot -o name -s creation -r "$ds" | grep -E "^${ds}@pat-${label}-" | head -n -"$keep" | while read -r s; do zfs destroy -r "$s"; done
EOT
  for spec in "hourly 48 hourly" "daily 14 daily" "weekly 8 weekly"; do
    set -- $spec; label=$1 keep=$2 cal=$3
    cat > "/etc/systemd/system/pat-zfs-snap-${label}.service" <<EOT
[Unit]
Description=project-pat ZFS ${label} snapshot of ${DATASET}
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/pat-zfs-snap ${label} ${keep} ${DATASET}
EOT
    cat > "/etc/systemd/system/pat-zfs-snap-${label}.timer" <<EOT
[Unit]
Description=project-pat ZFS ${label} snapshot timer
[Timer]
OnCalendar=${cal}
Persistent=true
RandomizedDelaySec=120
[Install]
WantedBy=timers.target
EOT
  done
  systemctl daemon-reload
  for l in hourly daily weekly; do systemctl enable --now "pat-zfs-snap-${l}.timer" >/dev/null; done
  mark_done $STEP; log "step $STEP applied"
}
if [[ "$MODE" == check ]]; then check && exit 0 || exit 1; fi
apply
