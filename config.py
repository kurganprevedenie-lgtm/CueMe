import os

from dotenv import load_dotenv

# Загружаем переменные из .env (файл лежит локально, в git не попадает)
load_dotenv()

# Имя продукта держим в одном месте — сменить название = поправить одну строку
APP_NAME = "CueMe"

# Токен бота от BotFather. Читается из .env, в код не вшивается.
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN не найден. Скопируй .env.example в .env и впиши токен от BotFather."
    )

# Ключ LLM-API понадобится позже, на этапе анализа. Пока может быть пустым.
LLM_API_KEY = os.getenv("LLM_API_KEY")

# Порог накопления сообщений для автоматической пересборки карточек.
REBUILD_THRESHOLD = int(os.getenv("REBUILD_THRESHOLD", "50"))
# Размер выборки для LLM-анализа.
SAMPLE_SIZE = int(os.getenv("SAMPLE_SIZE", "150"))
