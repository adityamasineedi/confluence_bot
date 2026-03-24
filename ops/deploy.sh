#!/bin/bash
# confluence_bot VPS deployment script
# Run this ON THE SERVER after SSH-ing in as root:
#   bash <(curl -s https://raw.githubusercontent.com/.../deploy.sh)
# OR copy this file to the server and run: bash deploy.sh

set -e

BOT_DIR="/opt/confluence_bot"
PYTHON_MIN="3.11"

echo "=== confluence_bot VPS deploy ==="

# 1. System packages
apt-get update -qq
apt-get install -y python3.12 python3.12-venv python3-pip git curl screen tmux

# 2. Clone or update repo
if [ -d "$BOT_DIR/.git" ]; then
    echo "Updating existing repo..."
    cd "$BOT_DIR" && git pull
else
    echo "Cloning repo..."
    # Replace with your actual repo URL if using git
    # git clone https://github.com/YOUR_USER/confluence_bot.git "$BOT_DIR"
    mkdir -p "$BOT_DIR"
    echo "[!] Copy your bot files to $BOT_DIR manually (scp or rsync)"
    echo "    Example: scp -r C:/projects/confluence_bot/* root@YOUR_SERVER_IP:$BOT_DIR/"
    echo "    Then re-run this script."
    exit 0
fi

# 3. Python venv
cd "$BOT_DIR"
python3.12 -m venv venv
venv/bin/pip install --upgrade pip -q
venv/bin/pip install -r requirements.txt -q

# 4. Systemd service
cat > /etc/systemd/system/confluence_bot.service << 'EOF'
[Unit]
Description=confluence_bot crypto trading bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/confluence_bot
EnvironmentFile=/opt/confluence_bot/.env
ExecStart=/opt/confluence_bot/venv/bin/python main.py
Restart=always
RestartSec=5
StandardOutput=append:/opt/confluence_bot/logs/bot.log
StandardError=append:/opt/confluence_bot/logs/bot.log

[Install]
WantedBy=multi-user.target
EOF

mkdir -p "$BOT_DIR/logs"
systemctl daemon-reload
systemctl enable confluence_bot
systemctl restart confluence_bot

echo ""
echo "=== Done ==="
echo "  Status:  systemctl status confluence_bot"
echo "  Logs:    tail -f $BOT_DIR/logs/bot.log"
echo "  Stop:    systemctl stop confluence_bot"
echo "  Start:   systemctl start confluence_bot"
echo "  Dashboard via SSH tunnel:"
echo "    ssh -L 8000:localhost:8000 root@YOUR_SERVER_IP"
echo "    then open http://localhost:8000"
