#!/usr/bin/env bash
# Host script: create the inference LXC (CT 101), share the host GPU into it (reusing the packaged
# NVIDIA driver already on the host), install the matching userspace driver inside, and verify the GPU
# is usable. Ollama + model restore is a separate step (provision-ollama.sh). Idempotent. ADR-0010.
set -euo pipefail

CTID=101
NAME=inference
DISK_STORAGE="${DISK_STORAGE:-local-lvm}"
TEMPL_STORAGE="${TEMPL_STORAGE:-local}"
MEM=16384; CORES=8; DISK=300; BRIDGE=vmbr0
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
CONF="/etc/pve/lxc/${CTID}.conf"

log(){ printf '[create-inference] %s\n' "$*"; }

DRV_VER="$(sed -nE 's/.*Module for x86_64 *([0-9.]+).*/\1/p' /proc/driver/nvidia/version | head -1)"
[ -n "$DRV_VER" ] || { echo "cannot read host NVIDIA driver version"; exit 1; }
log "host NVIDIA driver: ${DRV_VER}"

log "1/6 template"
TEMPL="$(pveam list "$TEMPL_STORAGE" 2>/dev/null | awk '/debian-13-standard/{print $1}' | head -1 || true)"
if [ -z "${TEMPL:-}" ]; then
  pveam update
  pveam download "$TEMPL_STORAGE" "$(pveam available --section system | awk '/debian-13-standard/{print $2}' | head -1)"
  TEMPL="$(pveam list "$TEMPL_STORAGE" | awk '/debian-13-standard/{print $1}' | head -1)"
fi

log "2/6 create CT ${CTID}"
if ! pct config "$CTID" >/dev/null 2>&1; then
  pct create "$CTID" "$TEMPL" \
    --hostname "$NAME" --cores "$CORES" --cpuunits 2048 --memory "$MEM" --swap 0 \
    --rootfs "${DISK_STORAGE}:${DISK}" \
    --net0 "name=eth0,bridge=${BRIDGE},ip=dhcp,firewall=1" \
    --nameserver "1.1.1.1 8.8.8.8" --searchdomain home.arpa \
    --unprivileged 1 --features nesting=1 --onboot 1 --start 0 \
    --tags "pat;gpu" --description "inference: Ollama + GPU (Project PAT)"
else
  log "    CT ${CTID} exists — keeping"
fi

log "3/6 share GPU devices"
# Reuse the host's loaded driver; pass the device nodes through. mode=0666 so the container's ollama
# user can open them regardless of the unprivileged uid mapping.
i=0
for d in /dev/nvidia0 /dev/nvidiactl /dev/nvidia-modeset /dev/nvidia-uvm /dev/nvidia-uvm-tools \
         /dev/nvidia-caps/nvidia-cap1 /dev/nvidia-caps/nvidia-cap2; do
  [ -e "$d" ] || continue
  if ! grep -qE "^dev[0-9]+: ${d}(,|$)" "$CONF"; then
    pct set "$CTID" "--dev${i}" "${d},gid=0,mode=0666"
  fi
  i=$((i+1))
done

pct status "$CTID" | grep -q running || pct start "$CTID"

log "4/6 prep container + wait for network"
pct push "$CTID" "$REPO/infra/provision/prep-container.sh" /opt/prep-container.sh --perms 0755
pct exec "$CTID" -- bash /opt/prep-container.sh
for _ in $(seq 1 30); do
  pct exec "$CTID" -- getent hosts developer.download.nvidia.com >/dev/null 2>&1 && break
  sleep 2
done

log "5/6 install matching userspace driver by COPYING host libs (exact ${DRV_VER})"
# NVIDIA's CUDA repo may have moved past the host's module version (seen: host 615.71.09 vs repo
# 615.71.10) → apt gives a mismatched userspace ("NVML: Driver/library version mismatch"), and
# libnvidia-ptxjitcompiler pulls the whole driver + DKMS. Copying the host's exact-version libs
# guarantees a match with the loaded kernel module and needs no repo.
# First strip any apt-installed nvidia bits (wrong version) from the container.
pct exec "$CTID" -- bash -c '
  export DEBIAN_FRONTEND=noninteractive
  pkgs=$(dpkg -l 2>/dev/null | awk "/nvidia/{print \$2}")
  [ -n "$pkgs" ] && apt-get purge -y -qq $pkgs >/dev/null 2>&1 || true
  apt-get autoremove -y -qq >/dev/null 2>&1 || true
  rm -f /etc/apt/sources.list.d/cuda*.list /usr/share/keyrings/cuda-archive-keyring.gpg 2>/dev/null || true'
n=0
for f in /usr/lib/x86_64-linux-gnu/*.so."${DRV_VER}"; do
  [ -e "$f" ] || continue
  pct push "$CTID" "$f" "$f" --perms 0644
  n=$((n+1))
done
log "    copied ${n} driver libs at ${DRV_VER}"
pct exec "$CTID" -- ldconfig   # recreates the SONAME links (libcuda.so.1, libnvidia-ml.so.1, ...)

log "6/6 verify GPU in container"
# Use the host's nvidia-smi binary (it only needs libnvidia-ml, now copied in).
pct push "$CTID" /usr/bin/nvidia-smi /usr/local/bin/nvidia-smi --perms 0755
echo "----- nvidia-smi (inside CT ${CTID}) -----"
pct exec "$CTID" -- /usr/local/bin/nvidia-smi || { echo "GPU NOT visible in container"; exit 1; }
log "GPU verified in CT ${CTID}. Next: provision-ollama.sh"
