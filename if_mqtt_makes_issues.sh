# 0) Stop + disable the service
sudo systemctl stop mosquitto || true
sudo systemctl disable mosquitto || true
sudo systemctl reset-failed mosquitto || true

# 1) Uninstall packages (purge removes package-owned config files)
sudo apt-get purge -y mosquitto mosquitto-clients

# 2) Remove any leftover Mosquitto dirs/files (certs, passwd, conf.d, logs, state)
sudo rm -rf \
  /etc/mosquitto \
  /var/lib/mosquitto \
  /var/log/mosquitto \
  /run/mosquitto

# 3) Remove Mosquitto user/group if you want it *really* clean
# (Safe: userdel/groupdel will fail harmlessly if not present)
sudo userdel -r mosquitto 2>/dev/null || true
sudo groupdel mosquitto 2>/dev/null || true

# 4) Clean apt dependencies + cached packages (optional but nice)
sudo apt-get autoremove -y --purge
sudo apt-get autoclean -y

# 5) Verify nothing is listening + no mosquitto unit loaded
sudo ss -lntp | grep -E ':(1883|8883)\b' || echo "no mqtt listeners"
systemctl status mosquitto --no-pager -l || true
