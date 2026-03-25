#!/usr/bin/env bash
set -euo pipefail

# One-command deploy for Ubuntu EC2
# Usage:
#   sudo bash deploy/ec2-one-command.sh
#
# Opcional (variaveis de ambiente antes do comando):
#   APP_DIR=/home/ubuntu/finance-ia
#   APP_USER=ubuntu
#   DOMAIN=api.seudominio.com
#   CERTBOT_EMAIL=voce@dominio.com
#   ENABLE_HTTPS=true
#   ENABLE_SWAP=true          # cria swap 4GB (recomendado em instancias pequenas)
#   SWAP_SIZE_GB=4
#   OLLAMA_PULL_MODEL=llama3.2:3b   # modelo baixado apos instalar Ollama
#   SKIP_OLLAMA=false               # true se Ollama rodar em outra maquina

APP_USER="${APP_USER:-ubuntu}"
APP_DIR="${APP_DIR:-/home/${APP_USER}/finance-ia}"
DOMAIN="${DOMAIN:-}"
CERTBOT_EMAIL="${CERTBOT_EMAIL:-}"
ENABLE_HTTPS="${ENABLE_HTTPS:-false}"
ENABLE_SWAP="${ENABLE_SWAP:-true}"
SWAP_SIZE_GB="${SWAP_SIZE_GB:-4}"
OLLAMA_PULL_MODEL="${OLLAMA_PULL_MODEL:-llama3.2:3b}"
SKIP_OLLAMA="${SKIP_OLLAMA:-false}"

export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Execute como root (sudo). Exemplo: sudo bash deploy/ec2-one-command.sh"
  exit 1
fi

if [[ ! -d "$APP_DIR" ]]; then
  echo "Diretorio da aplicacao nao encontrado: $APP_DIR"
  echo "Defina APP_DIR correto. Exemplo:"
  echo "  sudo APP_DIR=/home/ubuntu/finance-ia bash deploy/ec2-one-command.sh"
  exit 1
fi

if [[ ! -f "${APP_DIR}/.env" ]]; then
  echo "Arquivo .env nao encontrado em ${APP_DIR}/.env"
  echo "Crie o .env com DATABASE_URL e ALLOWED_GROUP_IDS antes de continuar."
  exit 1
fi

if [[ "${SKIP_OLLAMA,,}" == "true" ]]; then
  API_AFTER="network.target"
  WA_AFTER="network.target finance-api.service"
else
  API_AFTER="network.target ollama.service"
  WA_AFTER="network.target ollama.service finance-api.service"
fi

SYSTEM_PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

echo "[1/10] Swap opcional (alivia OOM em 4GB RAM)..."
if [[ "${ENABLE_SWAP,,}" == "true" ]]; then
  if ! swapon --show | grep -q '/swapfile'; then
    if [[ ! -f /swapfile ]]; then
      fallocate -l "${SWAP_SIZE_GB}G" /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=$((SWAP_SIZE_GB * 1024))
      chmod 600 /swapfile
      mkswap /swapfile
    fi
    swapon /swapfile || true
    grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
    echo "Swap ativo: $(swapon --show)"
  else
    echo "Swap ja configurado."
  fi
else
  echo "ENABLE_SWAP=false, pulando."
fi

echo "[2/10] Pacotes de sistema (ffmpeg, nginx, libs Chromium)..."
apt-get update -y
apt-get install -y \
  git curl nginx ffmpeg python3-venv python3-pip ca-certificates gnupg \
  libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 \
  libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 \
  libasound2 libpango-1.0-0 libcairo2 libatspi2.0-0 libgtk-3-0 \
  libnss3 libnspr4 libx11-6 libx11-xcb1 libxcb1 libxext6 libxshmfence1

echo "[3/10] Node.js 20..."
if ! command -v node >/dev/null 2>&1 || [[ "$(node -v | cut -d. -f1 | tr -d v)" -lt 20 ]]; then
  curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
  apt-get install -y nodejs
fi

echo "[4/10] Ollama (servidor local do agente)..."
if [[ "${SKIP_OLLAMA,,}" != "true" ]]; then
  if ! command -v ollama >/dev/null 2>&1; then
    curl -fsSL https://ollama.com/install.sh | sh
  fi
  systemctl enable ollama 2>/dev/null || true
  systemctl restart ollama
  sleep 2
  sudo -u "$APP_USER" bash -lc "OLLAMA_HOST=127.0.0.1:11434 ollama pull ${OLLAMA_PULL_MODEL}"
else
  echo "SKIP_OLLAMA=true: instale/configure Ollama em outro host e aponte a API."
fi

echo "[5/10] Dependencias Python..."
sudo -u "$APP_USER" bash -lc "cd \"$APP_DIR\" && python3 -m venv .venv && . .venv/bin/activate && pip install --upgrade pip && pip install -r requirements.txt"

echo "[6/10] Node/Prisma..."
sudo -u "$APP_USER" bash -lc "cd \"$APP_DIR\" && npm install && npm run prisma:deploy"

echo "[7/10] systemd: finance-api..."
cat >/etc/systemd/system/finance-api.service <<EOF
[Unit]
Description=Finance IA API
After=${API_AFTER}
Wants=network-online.target

[Service]
User=${APP_USER}
Group=${APP_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${APP_DIR}/.env
Environment=PATH=${APP_DIR}/.venv/bin:${SYSTEM_PATH}
ExecStart=${APP_DIR}/.venv/bin/uvicorn api:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

echo "[8/10] systemd: finance-whatsapp..."
cat >/etc/systemd/system/finance-whatsapp.service <<EOF
[Unit]
Description=Finance IA WhatsApp Bot
After=${WA_AFTER}

[Service]
User=${APP_USER}
Group=${APP_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${APP_DIR}/.env
Environment=PATH=/usr/bin:/usr/local/bin:${APP_DIR}/.venv/bin:${SYSTEM_PATH}
ExecStart=/usr/bin/npm run bot:whatsapp
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

echo "[9/10] Nginx..."
SERVER_NAME="${DOMAIN:-_}"
cat >/etc/nginx/sites-available/finance-ia <<EOF
server {
    listen 80;
    server_name ${SERVER_NAME};

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF

ln -sf /etc/nginx/sites-available/finance-ia /etc/nginx/sites-enabled/finance-ia
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl enable nginx
systemctl restart nginx

echo "[10/10] Recarregar e subir servicos..."
systemctl daemon-reload
systemctl enable finance-api finance-whatsapp
systemctl restart finance-api finance-whatsapp

if [[ "${ENABLE_HTTPS,,}" == "true" ]]; then
  if [[ -z "$DOMAIN" || "$DOMAIN" == "_" ]]; then
    echo "ENABLE_HTTPS=true requer DOMAIN valido."
    exit 1
  fi
  if [[ -z "$CERTBOT_EMAIL" ]]; then
    echo "ENABLE_HTTPS=true requer CERTBOT_EMAIL."
    exit 1
  fi

  echo "[extra] HTTPS com Certbot..."
  apt-get install -y certbot python3-certbot-nginx
  certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m "$CERTBOT_EMAIL" --redirect
  systemctl enable certbot.timer
  systemctl restart nginx
fi

echo ""
echo "Deploy finalizado."
echo "Ollama:  curl -s http://127.0.0.1:11434/api/tags"
echo "API:     curl -s http://127.0.0.1:8000/ | head"
echo ""
if [[ "${SKIP_OLLAMA,,}" != "true" ]]; then
  systemctl --no-pager --full status ollama 2>/dev/null | sed -n '1,8p' || true
  echo ""
fi
systemctl --no-pager --full status finance-api | sed -n '1,12p'
echo ""
systemctl --no-pager --full status finance-whatsapp | sed -n '1,12p'
echo ""
if [[ "${SKIP_OLLAMA,,}" != "true" ]]; then
  echo "Logs Ollama:  journalctl -u ollama -f"
fi
echo "Logs API:     journalctl -u finance-api -f"
echo "Logs WhatsApp: journalctl -u finance-whatsapp -f"
if [[ "${ENABLE_HTTPS,,}" == "true" ]]; then
  echo "HTTPS: https://${DOMAIN}"
fi
echo "QR WhatsApp: ${APP_DIR}/whatsapp-qr.png"
