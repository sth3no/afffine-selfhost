#!/bin/bash
# Affine residential-tunnel — Pi (or any always-on Linux machine) setup.
#
# What this does:
#   1. Installs Tailscale (mesh VPN — joins this machine onto the same
#      private network as your Affine VPS via WireGuard).
#   2. Installs gost — a tiny HTTP forward-proxy.
#   3. Runs gost as a systemd service listening on :8118 (HTTP proxy).
#
# After running this:
#   - This Pi is reachable from your VPS at its Tailscale IP/hostname.
#   - Anything POSTed to http://<pi-tailscale-ip>:8118 is forwarded
#     out via this Pi's normal residential internet connection.
#   - Set RESIDENTIAL_PROXY_URL=http://<pi-tailscale-ip>:8118 in your
#     Affine stack .env, redeploy, and YouTube traffic egresses via
#     your home IP — bypassing the cloud-IP bot wall.
#
# Run on the Pi (as root):
#   sudo bash pi-residential-tunnel-setup.sh
#
# Idempotent: safe to re-run if anything fails partway through.

set -euo pipefail

# ── Sanity checks ──────────────────────────────────────────────

if [[ $EUID -ne 0 ]]; then
    echo "This script needs root. Re-run with: sudo bash $0"
    exit 1
fi

if ! command -v apt-get &>/dev/null; then
    echo "Targets Raspberry Pi OS / Debian / Ubuntu. apt-get not found."
    exit 1
fi

ARCH=$(dpkg --print-architecture)
case "$ARCH" in
    arm64|armhf|amd64) ;;
    *) echo "Unsupported architecture: $ARCH. Tailscale + gost may not have a binary."; exit 1 ;;
esac

echo "=== Affine residential-tunnel setup ==="
echo "Architecture: $ARCH"
echo

# ── Step 1: apt update ─────────────────────────────────────────

echo "[1/5] Refreshing apt cache"
apt-get update -qq

# ── Step 2: Tailscale ──────────────────────────────────────────

if command -v tailscale &>/dev/null; then
    echo "[2/5] Tailscale already installed (version $(tailscale version | head -1))"
else
    echo "[2/5] Installing Tailscale via the official script"
    curl -fsSL https://tailscale.com/install.sh | sh
fi

# ── Step 3: gost ───────────────────────────────────────────────

GOST_VERSION="3.0.0"
GOST_BIN="/usr/local/bin/gost"

needs_install=true
if [[ -x $GOST_BIN ]]; then
    if "$GOST_BIN" -V 2>&1 | grep -q "$GOST_VERSION"; then
        needs_install=false
    fi
fi

if $needs_install; then
    echo "[3/5] Installing gost ${GOST_VERSION}"
    case "$ARCH" in
        arm64) GOST_ARCH="armv8" ;;
        armhf) GOST_ARCH="armv7" ;;
        amd64) GOST_ARCH="amd64" ;;
    esac
    cd /tmp
    curl -fsSL "https://github.com/go-gost/gost/releases/download/v${GOST_VERSION}/gost_${GOST_VERSION}_linux_${GOST_ARCH}.tar.gz" -o gost.tar.gz
    tar -xzf gost.tar.gz
    install -m 0755 gost "$GOST_BIN"
    rm -f gost.tar.gz gost README.md LICENSE 2>/dev/null || true
    echo "  installed: $($GOST_BIN -V 2>&1 | head -1)"
else
    echo "[3/5] gost ${GOST_VERSION} already installed"
fi

# ── Step 4: gost systemd service ───────────────────────────────

echo "[4/5] Configuring gost systemd service (HTTP proxy on :8118)"
cat >/etc/systemd/system/affine-gost-proxy.service <<'EOF'
[Unit]
Description=Affine residential-tunnel HTTP proxy (gost)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/gost -L http://:8118
Restart=always
RestartSec=5
User=nobody
Group=nogroup
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now affine-gost-proxy.service

# Wait for gost to actually bind :8118
for i in {1..10}; do
    if ss -tln 2>/dev/null | grep -q ':8118 '; then
        break
    fi
    sleep 1
done
if ! ss -tln 2>/dev/null | grep -q ':8118 '; then
    echo "  ⚠ gost is not listening on :8118 after 10s. Check: journalctl -u affine-gost-proxy -e"
    exit 1
fi
echo "  ✓ gost is listening on :8118"

# ── Step 5: Tailscale up ───────────────────────────────────────

echo "[5/5] Bringing up Tailscale"

if tailscale status &>/dev/null && tailscale ip -4 &>/dev/null; then
    echo "  ✓ Tailscale is already authenticated"
else
    echo "  Tailscale needs authentication. Running 'tailscale up' — open the printed URL"
    echo "  in any browser to log in (one-time)."
    tailscale up --hostname=affine-residential-pi --accept-routes
fi

# ── Final report ───────────────────────────────────────────────

echo
echo "=== Setup complete ==="
TS_IP=$(tailscale ip -4 2>/dev/null | head -1 || echo "(not authenticated)")
TS_HOST=$(tailscale status --self --json 2>/dev/null \
            | grep -o '"DNSName": *"[^"]*"' | head -1 \
            | cut -d'"' -f4 | sed 's/\.$//' \
          || echo "(unknown)")
echo "Tailscale IP:       $TS_IP"
echo "Tailscale hostname: $TS_HOST"
echo
echo "On your Affine VPS host, install Tailscale + log into the same"
echo "tailnet so it can reach this Pi:"
echo "  curl -fsSL https://tailscale.com/install.sh | sudo sh"
echo "  sudo tailscale up --hostname=affine-vps"
echo
echo "Then in your stack's .env (or Portainer stack environment):"
echo "  RESIDENTIAL_PROXY_URL=http://${TS_IP}:8118"
echo
echo "Redeploy the stack. Verify the proxy is being used:"
echo "  docker exec affine_cobalt env | grep -i proxy"
echo "  docker exec affine_ingest python3 -c \\"
echo "    'import urllib.request; print(urllib.request.urlopen(\"https://api.ipify.org\",timeout=10).read().decode())'"
echo
echo "The egress IP printed should be your home IP, NOT your VPS IP."
echo "If it's still your VPS IP, the proxy isn't being applied — recheck"
echo "RESIDENTIAL_PROXY_URL in the stack env and 'docker compose up -d'."
