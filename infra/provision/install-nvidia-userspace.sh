#!/usr/bin/env bash
# Runs INSIDE a container that has the host GPU devices shared in. Installs ONLY the NVIDIA userspace
# driver libraries (no kernel module — the host supplies that), pinned to the host's driver version so
# libcuda/NVML match the loaded kernel module exactly. Source: NVIDIA's official CUDA repo (same as host).
set -e
VER="${1:?usage: install-nvidia-userspace.sh <version, e.g. 615.71.09>}"
export DEBIAN_FRONTEND=noninteractive

apt-get install -y -qq curl ca-certificates gnupg >/dev/null

if [ ! -e /usr/share/keyrings/cuda-archive-keyring.gpg ]; then
  curl -fsSL https://developer.download.nvidia.com/compute/cuda/repos/debian13/x86_64/cuda-keyring_1.1-1_all.deb -o /tmp/cuda-keyring.deb
  dpkg -i /tmp/cuda-keyring.deb >/dev/null
fi
apt-get update -qq

# Pin to the host's exact version. Only userspace libs — none of these pull a kernel module.
if apt-get install -y -qq "libcuda1=${VER}-2" "libnvidia-ml1=${VER}-2" "libnvidia-ptxjitcompiler1=${VER}-2"; then
  echo "OK: installed NVIDIA userspace ${VER}-2"
else
  echo "WARNING: exact ${VER}-2 not in repo; installing current libs — these MUST match host module ${VER}"
  apt-get install -y -qq libcuda1 libnvidia-ml1 libnvidia-ptxjitcompiler1
fi
ldconfig
echo "libcuda: $(ls /usr/lib/x86_64-linux-gnu/libcuda.so.* 2>/dev/null | tr '\n' ' ')"
