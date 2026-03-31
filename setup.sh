#!/usr/bin/env bash
set -euo pipefail

# Agora CMS — production setup script
# Run on a fresh Linux VM to install Docker and start CMS with auto-updates.

INSTALL_DIR="${1:-/opt/agora-cms}"
REPO_URL="https://raw.githubusercontent.com/sslivins/agora-cms/main"

echo "==> Agora CMS setup (install dir: $INSTALL_DIR)"

# ── Install Docker if missing ──
if ! command -v docker &>/dev/null; then
    echo "==> Installing Docker..."
    curl -fsSL https://get.docker.com | sh
    sudo systemctl enable --now docker
    sudo usermod -aG docker "$USER"
    echo "    Docker installed. You may need to log out and back in for group changes."
fi

# ── Create install directory ──
sudo mkdir -p "$INSTALL_DIR"
sudo chown "$USER":"$USER" "$INSTALL_DIR"
cd "$INSTALL_DIR"

# ── Download compose file ──
echo "==> Downloading docker-compose.prod.yml..."
curl -fsSL "$REPO_URL/docker-compose.prod.yml" -o docker-compose.yml

# ── Create .env if missing ──
if [ ! -f .env ]; then
    echo "==> Creating .env from template..."
    curl -fsSL "$REPO_URL/.env.example" -o .env

    # Generate a random secret key
    SECRET_KEY=$(openssl rand -hex 32)
    sed -i "s|AGORA_CMS_SECRET_KEY=.*|AGORA_CMS_SECRET_KEY=$SECRET_KEY|" .env

    echo ""
    echo "    ┌─────────────────────────────────────────────────┐"
    echo "    │  Edit .env before starting:                     │"
    echo "    │    $INSTALL_DIR/.env                             "
    echo "    │                                                 │"
    echo "    │  At minimum, change:                            │"
    echo "    │    POSTGRES_PASSWORD                             │"
    echo "    │    AGORA_CMS_ADMIN_PASSWORD                     │"
    echo "    └─────────────────────────────────────────────────┘"
    echo ""
else
    echo "==> .env already exists, skipping."
fi

# ── Start services ──
echo "==> Starting Agora CMS..."
docker compose up -d

echo ""
echo "==> Done! CMS is running at http://$(hostname -I | awk '{print $1}'):8080"
echo "    Watchtower will auto-update the CMS image every 5 minutes."
