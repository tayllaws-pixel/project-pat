#!/usr/bin/env bash
# Install Ollama INSIDE a container from the official binary tarball — no `curl | sh`. Mirrors what
# ollama's install script does (binary to /usr, ollama user, systemd unit) without executing remote code.
set -e
export DEBIAN_FRONTEND=noninteractive

if command -v ollama >/dev/null; then
  echo "ollama already installed: $(ollama --version 2>/dev/null || echo present)"
  exit 0
fi

apt-get update -qq
apt-get install -y -qq curl ca-certificates >/dev/null

curl -fsSL https://github.com/ollama/ollama/releases/latest/download/ollama-linux-amd64.tgz -o /tmp/ollama.tgz
tar -C /usr -xzf /tmp/ollama.tgz          # -> /usr/bin/ollama and /usr/lib/ollama/*
rm -f /tmp/ollama.tgz

id ollama >/dev/null 2>&1 || useradd -r -s /usr/sbin/nologin -U -m -d /usr/share/ollama ollama

cat > /etc/systemd/system/ollama.service <<'EOF'
[Unit]
Description=Ollama Service
After=network-online.target

[Service]
ExecStart=/usr/bin/ollama serve
User=ollama
Group=ollama
Restart=always
RestartSec=3
Environment="PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload

echo "ollama installed: $(/usr/bin/ollama --version 2>/dev/null || echo ok)"
