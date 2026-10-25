#!/usr/bin/env bash
# Step 20 — NVIDIA driver on the Proxmox host via .run + DKMS (PAT-007, ADR-0001). Requires a reboot afterwards.
# NVIDIA_VERSION must be set explicitly (no silent "latest"): check https://www.nvidia.com/en-us/drivers/unix/ for the
# current production branch and pass e.g. NVIDIA_VERSION=580.xx.xx. The same version is later installed in CT 101.
# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"
STEP=20
NVIDIA_VERSION="${NVIDIA_VERSION:-}"
RUNFILE_URL="${RUNFILE_URL:-https://us.download.nvidia.com/XFree86/Linux-x86_64/${NVIDIA_VERSION}/NVIDIA-Linux-x86_64-${NVIDIA_VERSION}.run}"

check() {
  command -v nvidia-smi >/dev/null && nvidia-smi -L 2>/dev/null | grep -qi "4070 Ti SUPER" && \
  grep -qs '^nvidia-uvm' /etc/modules-load.d/nvidia.conf && apt-mark showhold | grep -q proxmox-kernel
}
apply() {
  require_root; require_pve; ensure_dirs
  [[ -n "$NVIDIA_VERSION" ]] || die "set NVIDIA_VERSION=<production branch version> (see script header)"
  lspci -nn | grep -qi 'NVIDIA' || die "no NVIDIA device on PCI bus"

  log "blacklist nouveau"
  cat > /etc/modprobe.d/blacklist-nouveau.conf <<'EOT'
blacklist nouveau
options nouveau modeset=0
EOT
  log "headers + build tools for running kernel $(uname -r)"
  apt_install "proxmox-headers-$(uname -r)" proxmox-default-headers build-essential dkms pkg-config
  local run="/var/lib/project-pat/NVIDIA-Linux-x86_64-${NVIDIA_VERSION}.run"
  [[ -f "$run" ]] || curl -fL "$RUNFILE_URL" -o "$run"
  chmod +x "$run"
  if ! (command -v nvidia-smi >/dev/null && nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | grep -q "^${NVIDIA_VERSION}$"); then
    log "installing driver ${NVIDIA_VERSION} with DKMS (no X, no 32-bit)"
    "$run" --silent --dkms --no-x-check --no-nouveau-check --no-opengl-files --no-install-compat32-libs --no-questions
  fi
  log "module autoload + uvm device node at boot"
  cat > /etc/modules-load.d/nvidia.conf <<'EOT'
nvidia
nvidia-modeset
nvidia-uvm
EOT
  cat > /etc/udev/rules.d/70-nvidia.rules <<'EOT'
KERNEL=="nvidia", RUN+="/bin/bash -c '/usr/bin/nvidia-smi -L && /bin/chmod 666 /dev/nvidia*'"
KERNEL=="nvidia_uvm", RUN+="/bin/bash -c '/usr/bin/nvidia-modprobe -c0 -u && /bin/chmod 0666 /dev/nvidia-uvm*'"
EOT
  # Persistence daemon keeps the driver loaded so first inference does not pay init cost
  if [[ -x /usr/bin/nvidia-persistenced ]]; then
    cat > /etc/systemd/system/nvidia-persistenced.service <<'EOT'
[Unit]
Description=NVIDIA Persistence Daemon
After=syslog.target
[Service]
Type=forking
ExecStart=/usr/bin/nvidia-persistenced --verbose
ExecStopPost=/bin/rm -rf /var/run/nvidia-persistenced
[Install]
WantedBy=multi-user.target
EOT
    systemctl daemon-reload; systemctl enable nvidia-persistenced >/dev/null
  fi
  log "holding kernel packages until the kernel-upgrade runbook is used"
  apt-mark hold proxmox-default-kernel "proxmox-kernel-$(uname -r)" >/dev/null 2>&1 || apt-mark hold proxmox-default-kernel >/dev/null
  update-initramfs -u -k all >/dev/null 2>&1
  echo "$NVIDIA_VERSION" > "$PAT_ETC/nvidia-version"
  need_reboot
  log "step $STEP applied; reboot, then 'patctl install' resumes and verifies nvidia-smi"
}
if [[ "$MODE" == check ]]; then check && exit 0 || exit 1; fi
apply
