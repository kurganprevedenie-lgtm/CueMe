#!/usr/bin/env bash
# Включает вебхук Tribute: https-адрес через Caddy (сертификат сам) + ключ в .env.
#   bash /root/setup_tribute_webhook.sh
# Спросит API-ключ Tribute (ввод скрыт), допишет его в /root/CueMe/.env,
# поставит Caddy, если порты 80/443 свободны, и перезапустит бота.
set -euo pipefail

ENV=/root/CueMe/.env
HOST="185-130-224-151.sslip.io"
PATH_WH=$(grep -E '^TRIBUTE_WEBHOOK_PATH=' "$ENV" 2>/dev/null | cut -d= -f2- || true)
PATH_WH=${PATH_WH:-/tribute/webhook}
PORT=$(grep -E '^TRIBUTE_WEBHOOK_PORT=' "$ENV" 2>/dev/null | cut -d= -f2- || true)
PORT=${PORT:-8081}

echo "== 1/4 Порты 80 и 443"
BUSY=$(ss -ltnp '( sport = :80 or sport = :443 )' | tail -n +2 || true)
if [ -n "$BUSY" ] && ! echo "$BUSY" | grep -q caddy; then
  echo "Порты 80/443 уже заняты, Caddy ставить нельзя — пришли это сообщение:"
  echo "$BUSY"
  exit 1
fi
echo "   свободны (или уже Caddy)"

echo "== 2/4 Caddy"
if ! command -v caddy >/dev/null; then
  apt-get update -qq
  apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https curl gpg >/dev/null
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq
  apt-get install -y -qq caddy >/dev/null
fi
cat > /etc/caddy/Caddyfile <<EOF
$HOST {
	handle $PATH_WH {
		reverse_proxy 127.0.0.1:$PORT
	}
	handle {
		respond 404
	}
}
EOF
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
fi
systemctl enable --now caddy >/dev/null 2>&1 || true
systemctl reload caddy || systemctl restart caddy
echo "   готово"

echo "== 3/4 Ключ Tribute"
read -r -s -p "Вставь API-ключ из Tribute и нажми Enter (на экране не видно): " KEY; echo
if [ -z "$KEY" ]; then echo "Пустой ключ — выхожу, ничего не менял в .env"; exit 1; fi
cp "$ENV" "$ENV.bak.$(date +%s)"
grep -v '^TRIBUTE_API_KEY=' "$ENV" > "$ENV.tmp" && mv "$ENV.tmp" "$ENV"
echo "TRIBUTE_API_KEY=$KEY" >> "$ENV"
chmod 600 "$ENV"
systemctl restart cueme-bot
echo "   ключ записан, бот перезапущен"

echo "== 4/4 Проверка (ждём сертификат, до минуты)"
for i in $(seq 1 12); do
  sleep 5
  CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST "https://$HOST$PATH_WH" -d '{}' || true)
  [ "$CODE" = "401" ] && break
done
echo "   https://$HOST$PATH_WH → HTTP $CODE  (401 = всё работает: бот ответил «нет подписи»)"
echo
echo "Вставь в Tribute в поле WEBHOOK URL:"
echo "   https://$HOST$PATH_WH"
echo "и нажми «Отправить тестовый запрос»."
