#!/bin/bash
set -e
cd ~/CueMe
TOKEN=$(grep BOT_TOKEN .env | cut -d= -f2)

echo "Останавливаю бота..."
sudo systemctl stop cueme-bot

echo ""
echo ">>> Теперь напиши в группе команду /id (там, где уже есть @CueMeChatBot) <<<"
echo "Жду сообщение..."

CHAT_ID=""
for i in $(seq 1 60); do
    RESULT=$(curl -s "https://api.telegram.org/bot${TOKEN}/getUpdates" | grep -o '"chat":{"id":-[0-9]*' | head -1)
    if [ -n "$RESULT" ]; then
        CHAT_ID=$(echo "$RESULT" | grep -o -- '-[0-9]*')
        break
    fi
    sleep 2
done

if [ -z "$CHAT_ID" ]; then
    echo "Не дождался сообщения за 2 минуты. Запусти бота обратно и попробуй снова:"
    sudo systemctl start cueme-bot
    exit 1
fi

echo "Нашёл chat_id: $CHAT_ID"
echo "ADMIN_GROUP_CHAT_ID=${CHAT_ID}" >> .env
echo "Записал в .env"

sudo systemctl start cueme-bot
echo "Бот перезапущен. Готово!"
