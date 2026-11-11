#!/usr/bin/env bash
# IP gateway: each partner's Angel One account sends its API calls from its own static public IP
# (SEBI / Angel One: one static IP per client code). Run on the server as root:  sudo bash setup.sh
#
#   public IP        private IP (AWS)   gateway port   partner
#   65.0.62.220      172.31.12.108      (direct)       Angel A/c 1
#   13.126.29.140    172.31.11.102      3129           RAM
#   13.205.116.78    172.31.0.179       3130           Mehak
#
# 1. Ubuntu learns the secondary private IPs (AWS attaches them, but only the primary comes via DHCP).
# 2. squid listens on the app's docker network gateway (172.18.0.1) only: port 3129 leaves from
#    172.31.11.102 (= 13.126.29.140), port 3130 from 172.31.0.179 (= 13.205.116.78). It only allows the
#    app's containers, and only HTTPS to Angel One and the IP-check site.
set -euo pipefail
cd "$(dirname "$0")"

install -m 600 60-secondary-ips.yaml /etc/netplan/60-secondary-ips.yaml
netplan generate
netplan apply
ip -4 -br addr show ens5

if ! command -v squid >/dev/null; then
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq squid
fi
install -m 644 squid.conf /etc/squid/squid.conf
mkdir -p /etc/systemd/system/squid.service.d
install -m 644 squid-override.conf /etc/systemd/system/squid.service.d/override.conf
systemctl daemon-reload
squid -k parse
systemctl enable squid
systemctl restart squid
sleep 2
systemctl is-active squid
