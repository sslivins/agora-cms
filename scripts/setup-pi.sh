#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────
# Agora — Raspberry Pi Zero 2 W Setup Script
# Base OS: Raspberry Pi OS 64-bit Lite
# Run as root: sudo bash scripts/setup-pi.sh
# ──────────────────────────────────────────────────────────────
set -euo pipefail

AGORA_USER="mpv"
AGORA_BASE="/opt/agora"
AGORA_SRC="${AGORA_BASE}/src"
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# ── Colors ──
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ── Preflight ──
[[ $EUID -eq 0 ]] || error "This script must be run as root (sudo)"
[[ "$(uname -m)" == "aarch64" ]] || warn "Expected aarch64 — are you on Pi Zero 2 W?"

info "Starting Agora setup on $(hostname)"

# ── 1. Create user ──
if id "${AGORA_USER}" &>/dev/null; then
    info "User '${AGORA_USER}' already exists"
else
    info "Creating user '${AGORA_USER}'"
    useradd -m -s /bin/bash "${AGORA_USER}"
    echo "${AGORA_USER}:${AGORA_USER}" | chpasswd
fi

# ── 2. System packages ──
info "Updating package lists"
apt-get update -qq

info "Installing system dependencies"
apt-get install -y -qq \
    python3 \
    python3-pip \
    python3-gi \
    gir1.2-gstreamer-1.0 \
    gir1.2-glib-2.0 \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-alsa \
    gstreamer1.0-v4l2 \
    kbd

# ── 3. Directory structure ──
info "Creating directory structure"
mkdir -p "${AGORA_BASE}"/{assets/{videos,images,splash},state,logs,tmp,src}
chown -R "${AGORA_USER}:${AGORA_USER}" "${AGORA_BASE}"

# ── 4. Copy source code ──
info "Deploying source code to ${AGORA_SRC}"
for dir in api player shared systemd; do
    if [[ -d "${SCRIPT_DIR}/${dir}" ]]; then
        cp -r "${SCRIPT_DIR}/${dir}" "${AGORA_SRC}/"
    fi
done

# Copy supporting files
for f in requirements-api.txt requirements-player.txt; do
    [[ -f "${SCRIPT_DIR}/${f}" ]] && cp "${SCRIPT_DIR}/${f}" "${AGORA_SRC}/"
done

chown -R "${AGORA_USER}:${AGORA_USER}" "${AGORA_SRC}"

# Install boot-splash.png as the default application splash
BOOT_SPLASH="${SCRIPT_DIR}/config/boot-splash.png"
if [[ -f "${BOOT_SPLASH}" ]]; then
    cp "${BOOT_SPLASH}" "${AGORA_BASE}/assets/splash/default.png"
    chown "${AGORA_USER}:${AGORA_USER}" "${AGORA_BASE}/assets/splash/default.png"
    info "Installed default application splash screen"
fi

# ── 5. Python packages ──
info "Installing Python dependencies"
pip3 install --break-system-packages \
    -r "${AGORA_SRC}/requirements-api.txt" \
    -r "${AGORA_SRC}/requirements-player.txt" 2>/dev/null \
|| pip3 install \
    -r "${AGORA_SRC}/requirements-api.txt" \
    -r "${AGORA_SRC}/requirements-player.txt"

# ── 6. Configuration ──
BOOT_CONFIG="/boot/agora-config.json"
if [[ ! -f "${BOOT_CONFIG}" ]]; then
    info "Creating default config at ${BOOT_CONFIG}"
    API_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
    SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
    cat > "${BOOT_CONFIG}" <<EOF
{
    "api_key": "${API_KEY}",
    "web_username": "admin",
    "web_password": "agora",
    "secret_key": "${SECRET_KEY}",
    "device_name": "$(hostname)",
    "default_splash": "splash/default.png"
}
EOF
    chmod 600 "${BOOT_CONFIG}"
    info "Generated secure API key and secret key"
    warn "Default web password is 'agora' — change it after first login"
else
    info "Config already exists at ${BOOT_CONFIG}, skipping"
fi

# ── 7. Plymouth boot splash ──
info "Setting up Plymouth boot splash"
apt-get install -y -qq plymouth

PLYMOUTH_THEME_DIR="/usr/share/plymouth/themes/splash"
mkdir -p "${PLYMOUTH_THEME_DIR}"

# Use the boot splash image from the repo
SPLASH_SRC="${SCRIPT_DIR}/config/boot-splash.png"
if [[ -f "${SPLASH_SRC}" ]]; then
    cp "${SPLASH_SRC}" "${PLYMOUTH_THEME_DIR}/splash.png"
else
    warn "No splash image found at ${SPLASH_SRC} — place splash.png manually in ${PLYMOUTH_THEME_DIR}/"
fi

cat > "${PLYMOUTH_THEME_DIR}/splash.plymouth" <<'EOF'
[Plymouth Theme]
Name=Splash
Description=Simple PNG Splash Screen
ModuleName=script

[script]
ImageDir=/usr/share/plymouth/themes/splash
ScriptFile=/usr/share/plymouth/themes/splash/splash.script
EOF

cat > "${PLYMOUTH_THEME_DIR}/splash.script" <<'EOF'
image = Image("splash.png");

screen_width = Window.GetWidth();
screen_height = Window.GetHeight();

image_width = image.GetWidth();
image_height = image.GetHeight();

x = (screen_width - image_width) / 2;
y = (screen_height - image_height) / 2;

sprite = Sprite(image);
sprite.SetPosition(x, y, 0);
EOF

plymouth-set-default-theme splash
update-initramfs -u

# ── 8. Systemd services ──
info "Installing systemd services"
cp "${AGORA_SRC}/systemd/agora-player.service" /etc/systemd/system/
cp "${AGORA_SRC}/systemd/agora-api.service" /etc/systemd/system/

systemctl daemon-reload
systemctl enable agora-player agora-api

# ── 9. Start services ──
info "Starting services"
systemctl start agora-player
systemctl start agora-api

# ── 10. Verify ──
sleep 3
echo ""
info "=== Service Status ==="
systemctl is-active agora-player && info "agora-player: running" || warn "agora-player: not running"
systemctl is-active agora-api && info "agora-api: running" || warn "agora-api: not running"

echo ""
info "=== Setup Complete ==="
info "Web UI:  http://$(hostname -I | awk '{print $1}'):8000"
info "Login:   admin / agora"
info "Config:  ${BOOT_CONFIG}"
echo ""
