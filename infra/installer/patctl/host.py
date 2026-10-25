"""Host command abstraction for the installer — the only place that shells out. Faked in tests."""
from __future__ import annotations

import os
import secrets
import shutil
import subprocess
from pathlib import Path


class Host:
    def __init__(self, repo_root: str, dry_run: bool = False, out=print):
        self.repo_root = Path(repo_root)
        self.dry_run = dry_run
        self.out = out

    # --- commands ---
    def run(self, cmd: list[str], check: bool = True, timeout: int = 3600, env: dict | None = None) -> tuple[int, str]:
        if self.dry_run:
            self.out("    $ " + " ".join(cmd))
            return 0, ""
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env={**os.environ, **(env or {})})
        if check and p.returncode != 0:
            raise RuntimeError(f"{' '.join(cmd)} failed ({p.returncode}): {(p.stderr or p.stdout).strip()[-2000:]}")
        return p.returncode, p.stdout + p.stderr

    def probe(self, cmd: list[str], timeout: int = 120) -> tuple[int, str]:
        """Read-only command for check(); never dry-run-suppressed, never raises."""
        if shutil.which(cmd[0]) is None:
            return 127, ""
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            return p.returncode, p.stdout + p.stderr
        except (subprocess.TimeoutExpired, OSError) as e:
            return 1, str(e)

    def script(self, name: str, *args: str, env: dict | None = None) -> None:
        self.run(["bash", str(self.repo_root / "infra/proxmox" / name), "--apply", *args], env=env)

    def script_check(self, name: str, *args: str) -> bool:
        rc, _ = self.probe(["bash", str(self.repo_root / "infra/proxmox" / name), "--check", *args])
        return rc == 0

    # --- files ---
    def read(self, path: str) -> str | None:
        try:
            return Path(path).read_text()
        except OSError:
            return None

    def write(self, path: str, content: str, mode: int = 0o644) -> None:
        if self.dry_run:
            self.out(f"    write {path} ({len(content)} bytes, mode {oct(mode)})")
            return
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(content)
        os.chmod(tmp, mode)
        os.replace(tmp, p)

    def exists(self, path: str) -> bool:
        return Path(path).exists()

    @staticmethod
    def new_secret(nbytes: int = 32) -> str:
        return secrets.token_urlsafe(nbytes)
