#!/usr/bin/env bash
set -euo pipefail

# One-command deploy for Ubuntu EC2
# Usage:
#   bash deploy/ec2-one-command.sh
# Optional env vars:
#   APP_DIR=/home/ubuntu/finance-ia
#   APP_USER=ubuntu
#   DOMAIN=api.seudominio.com
#   CERTBOT_EMAIL=voce@dominio.com
#   ENABLE_HTTPS=true

APP_USER="${APP_USER:-ubuntu}"
APP_DIR="${APP_DIR:-/home/${APP_USER}/finance-ia}"
DOMAIN="${DOMAIN:-}"
CERTBOT_EMAIL="${CERTBOT_EMAIL:-}"
ENABLE_HTTPS="${ENABLE_HTTPS:-false}"

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

echo "[1/8] Instalando pacotes de sistema..."
apt-get update -y
apt-get install -y git curl nginx ffmpeg python3-venv python3-pip ca-certificates gnupg

echo "[2/8] Instalando Node.js 20..."
if ! command -v node >/dev/null 2>&1 || [[ "$(node -v | cut -d. -f1 | tr -d v)" -lt 20 ]]; then
  curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
  apt-get install -y nodejs
fi

echo "[3/8] Instalando dependencias Python..."
sudo -u "$APP_USER" bash -lc "cd \"$APP_DIR\" && python3 -m venv .venv && . .venv/bin/activate && pip install --upgrade pip && pip install -r requirements.txt"

echo "[4/8] Instalando dependencias Node/Prisma..."
sudo -u "$APP_USER" bash -lc "cd \"$APP_DIR\" && npm install && npm run prisma:deploy"

echo "[5/8] Criando service da API..."
cat >/etc/systemd/system/finance-api.service <<EOF
[Unit]
Description=Finance IA API
After=network.target

[Service]
User=${APP_USER}
Group=${APP_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${APP_DIR}/.env
Environment=PATH=${APP_DIR}/.venv/bin
ExecStart=${APP_DIR}/.venv/bin/uvicorn api:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

echo "[6/8] Criando service do bot WhatsApp..."
cat >/etc/systemd/system/finance-whatsapp.service <<EOF
[Unit]
Description=Finance IA WhatsApp Bot
After=network.target finance-api.service

[Service]
User=${APP_USER}
Group=${APP_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${APP_DIR}/.env
ExecStart=/usr/bin/npm run bot:whatsapp
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

echo "[7/8] Configurando Nginx..."
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

echo "[8/8] Subindo servicos..."
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

  echo "[extra] Configurando HTTPS com Certbot..."
  apt-get install -y certbot python3-certbot-nginx
  certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m "$CERTBOT_EMAIL" --redirect
  systemctl enable certbot.timer
  systemctl restart nginx
fi

echo ""
echo "Deploy finalizado."
echo "Status API:"
systemctl --no-pager --full status finance-api | sed -n '1,12p'
echo ""
echo "Status Bot:"
systemctl --no-pager --full status finance-whatsapp | sed -n '1,12p'
echo ""
echo "Logs da API:      journalctl -u finance-api -f"
echo "Logs do WhatsApp: journalctl -u finance-whatsapp -f"
if [[ "${ENABLE_HTTPS,,}" == "true" ]]; then
  echo "HTTPS habilitado para: https://${DOMAIN}"
fi
echo ""
echo "Se for primeira execucao do WhatsApp, verifique o QR:"
echo "  ${APP_DIR}/whatsapp-qr.png"
