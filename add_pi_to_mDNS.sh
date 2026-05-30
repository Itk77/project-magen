sudo apt update
sudo apt install -y avahi-daemon
sudo systemctl enable --now avahi-daemon
hostname
hostname -f
