#!/usr/bin/env bash
set -euo pipefail

sudo cp "$(dirname "$0")/systemd/magen-main-server.service" /etc/systemd/system/magen-main-server.service
sudo systemctl daemon-reload
sudo systemctl enable magen-main-server.service
sudo systemctl restart magen-main-server.service
sudo systemctl status --no-pager magen-main-server.service
