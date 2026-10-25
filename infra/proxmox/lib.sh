#!/usr/bin/env bash
# Shared helpers for Project PAT host scripts. Sourced, not executed.
# Conventions: every script accepts --check (read-only; exit 0 = already applied, 1 = pending, 2 = cannot apply)
# and --apply (default). Scripts are idempotent and never drop the caller's SSH session.
set -euo pipefail

PAT_STATE_DIR="${PAT_STATE_DIR:-/var/lib/project-pat}"
PAT_BACKUP_DIR="${PAT_BACKUP_DIR:-$PAT_STATE_DIR/backups}"
PAT_ETC="${PAT_ETC:-/etc/project-pat}"
MODE="apply"
for a in "$@"; do case "$a" in --check) MODE="check";; --apply) MODE="apply";; esac; done

log()  { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
warn() { printf '[%s] WARN: %s\n' "$(date +%H:%M:%S)" "$*" >&2; }
die()  { printf '[%s] ERROR: %s\n' "$(date +%H:%M:%S)" "$*" >&2; exit 2; }

require_root() { [[ "$(id -u)" == 0 ]] || die "must run as root"; }
require_pve()  { command -v pveversion >/dev/null 2>&1 || die "pveversion not found: not a Proxmox host"; }
ensure_dirs()  { mkdir -p "$PAT_STATE_DIR" "$PAT_BACKUP_DIR" "$PAT_ETC"; chmod 700 "$PAT_ETC"; }

# backup_file PATH — timestamped copy before first modification in this run
backup_file() {
  local f="$1"; [[ -e "$f" ]] || return 0
  local name; name="$(basename "$f").$(date +%Y%m%d-%H%M%S)"
  cp -a "$f" "$PAT_BACKUP_DIR/$name"; log "backup: $f -> $PAT_BACKUP_DIR/$name"
}

# ensure_line FILE LINE — append LINE if not present verbatim
ensure_line() { local f="$1" l="$2"; grep -qxF -- "$l" "$f" 2>/dev/null || { backup_file "$f"; printf '%s\n' "$l" >> "$f"; }; }

# set_kv FILE KEY VALUE [SEP] — idempotent "KEY SEP VALUE" line (sshd_config style, SEP default ' ')
set_kv() {
  local f="$1" k="$2" v="$3" sep="${4:- }"
  if grep -qE "^[#[:space:]]*${k}[[:space:]=]" "$f" 2>/dev/null; then
    grep -qE "^${k}${sep}${v}\$" "$f" && return 0
    backup_file "$f"; sed -i -E "s|^[#[:space:]]*${k}[[:space:]=].*|${k}${sep}${v}|" "$f"
  else
    backup_file "$f"; printf '%s%s%s\n' "$k" "$sep" "$v" >> "$f"
  fi
}

pkg_installed() { dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q "install ok installed"; }
apt_install()   { local missing=(); for p in "$@"; do pkg_installed "$p" || missing+=("$p"); done
                  [[ ${#missing[@]} -eq 0 ]] || { log "apt install: ${missing[*]}"; DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${missing[@]}"; }; }

mark_done()   { ensure_dirs; date -Is > "$PAT_STATE_DIR/step.$1.done"; }
is_marked()   { [[ -f "$PAT_STATE_DIR/step.$1.done" ]]; }
need_reboot() { ensure_dirs; touch "$PAT_STATE_DIR/reboot-required"; warn "reboot required before continuing"; }
