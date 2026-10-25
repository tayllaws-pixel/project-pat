# Runbook — Proxmox kernel upgrade with the NVIDIA DKMS driver installed

Why: step 20 holds the kernel packages so an unattended upgrade can never leave the host with a kernel the
NVIDIA module was not built for (which would take CT 101 inference offline).

Do this during a window when nobody needs Pat. ~10 minutes, one reboot.

```bash
cd /opt/project-pat
python3 -m infra.installer status                     # confirm nothing is mid-flight; Sentinel e-stop recommended
apt-mark unhold $(apt-mark showhold | grep -E 'proxmox-(default-)?kernel')
apt-get update -qq && apt-get install -y proxmox-default-kernel "proxmox-headers-$(apt-cache depends proxmox-default-headers | awk '/Depends/{print $2}' | sed 's/proxmox-headers-//')"
dkms autoinstall                                       # builds nvidia for the newly installed kernel
dkms status | grep nvidia                              # expect: nvidia/<ver>, <new-kernel>, x86_64: installed
reboot
```
After reconnecting:
```bash
uname -r; nvidia-smi -L                                # new kernel, GPU visible
cd /opt/project-pat && python3 -m infra.installer verify --phase 1
apt-mark hold proxmox-default-kernel "proxmox-kernel-$(uname -r)"
```
If `nvidia-smi` fails after reboot: select the previous kernel in the boot menu (or
`proxmox-boot-tool kernel pin <old-version>`), then `patctl rollback --step 20` and re-run step 20 with the
same `NVIDIA_VERSION`, or move to the newer production driver branch if NVIDIA has dropped support for the kernel.
