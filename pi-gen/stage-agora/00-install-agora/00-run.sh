#!/bin/bash -e
# pi-gen stage: Install Agora from APT repository and configure for captive portal boot.

on_chroot <<'CHEOF'

# ── Add Agora apt repository ──
REPO_URL="https://sslivins.github.io/agora"
echo "deb [arch=arm64 trusted=yes] ${REPO_URL} stable main" > /etc/apt/sources.list.d/agora.list
apt-get update -qq

# ── Install Agora (pulls in network-manager, dnsmasq, avahi-daemon) ──
apt-get install -y agora

# ── Ensure device boots into captive portal (no provisioned flag) ──
rm -f /opt/agora/persist/provisioned

# ── Disable Pi OS first-boot wizard (user already configured by pi-gen) ──
systemctl disable userconfig 2>/dev/null || true
rm -f /etc/xdg/autostart/piwiz.desktop 2>/dev/null || true

# ── Enable SSH (disabled by default on Pi OS) ──
systemctl enable ssh

# ── DEBUG: Disable player so console stays visible on HDMI ──
systemctl disable agora-player 2>/dev/null || true

# ── DEBUG: Show boot messages on console (remove quiet/splash) ──
sed -i 's/ quiet//g; s/ splash//g' /boot/firmware/cmdline.txt 2>/dev/null || true

# ── Clean up ──
apt-get clean
rm -rf /var/lib/apt/lists/*

CHEOF
