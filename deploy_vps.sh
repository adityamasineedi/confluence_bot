#!/bin/bash
# ═══════════════════════════════════════════════════════════
# VPS DEPLOYMENT SCRIPT — confluence_bot
# Run this on a fresh Ubuntu 22.04 VPS
#
# Usage:
#   1. SSH into your VPS: ssh root@YOUR_IP
#   2. Upload this script: scp deploy_vps.sh root@YOUR_IP:~
#   3. Run: bash deploy_vps.sh
# ═══════════════════════════════════════════════════════════

set -e

echo "═══════════════════════════════════════════════"
echo "  Confluence Bot — VPS Deployment"
echo "═══════════════════════════════════════════════"

# ── 1. System setup ──────────────────────────────────────
echo "[1/7] Installing system packages..."
apt update && apt upgrade -y
apt install -y python3.11 python3.11-venv python3-pip git screen htop

# ── 2. Create bot user (don't run as root) ───────────────
echo "[2/7] Creating bot user..."
useradd -m -s /bin/bash botuser 2>/dev/null || true
mkdir -p /home/botuser/confluence_bot
chown -R botuser:botuser /home/botuser

# ── 3. Upload code ───────────────────────────────────────
echo "[3/7] Code should be uploaded to /home/botuser/confluence_bot/"
echo "  Use: scp -r * botuser@YOUR_IP:/home/botuser/confluence_bot/"
echo "  Or:  git clone YOUR_REPO /home/botuser/confluence_bot"

# ── 4. Setup Python environment ─────────────────────────
echo "[4/7] Setting up Python venv..."
cd /home/botuser/confluence_bot
su botuser -c "python3.11 -m venv venv"
su botuser -c "venv/bin/pip install --upgrade pip"
su botuser -c "venv/bin/pip install -r requirements.txt"

# ── 5. Create systemd service (auto-start + auto-restart)
echo "[5/7] Creating systemd service..."
cat > /etc/systemd/system/confluence-bot.service << 'EOF'
[Unit]
Description=Confluence Trading Bot
After=network.target
Wants=network-online.target

[Service]
Type=simple
User=botuser
WorkingDirectory=/home/botuser/confluence_bot
ExecStart=/home/botuser/confluence_bot/venv/bin/python main.py
Restart=always
RestartSec=30
StartLimitInterval=600
StartLimitBurst=10
StandardOutput=append:/home/botuser/confluence_bot/logs/bot.log
StandardError=append:/home/botuser/confluence_bot/logs/bot.log
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

# ── 6. Enable and start ─────────────────────────────────
echo "[6/7] Enabling service..."
systemctl daemon-reload
systemctl enable confluence-bot
systemctl start confluence-bot

# ── 7. Setup log rotation ───────────────────────────────
echo "[7/7] Setting up log rotation..."
cat > /etc/logrotate.d/confluence-bot << 'EOF'
/home/botuser/confluence_bot/logs/*.log {
    daily
    rotate 14
    compress
    missingok
    notifempty
    copytruncate
}
EOF

echo ""
echo "═══════════════════════════════════════════════"
echo "  DEPLOYMENT COMPLETE"
echo "═══════════════════════════════════════════════"
echo ""
echo "  Check status:  systemctl status confluence-bot"
echo "  View logs:     journalctl -u confluence-bot -f"
echo "  Restart:       systemctl restart confluence-bot"
echo "  Stop:          systemctl stop confluence-bot"
echo ""
echo "  Bot auto-starts on reboot"
echo "  Bot auto-restarts on crash (30s delay)"
echo "  Max 10 restarts per 10 min (prevents crash loops)"
echo "  Logs rotate daily, keep 14 days"
echo ""
