import os

from dotenv import load_dotenv

load_dotenv()

APP_NAME = "CueMe"

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN не найден. Скопируй .env.example в .env и впиши токен от BotFather."
    )

# Groq — llama-3.3-70b. LLM_API_KEY поддерживается как алиас.
GROQ_API_KEY = os.getenv("GROQ_API_KEY") or os.getenv("LLM_API_KEY")
# Gemini — основной (gemini-2.5-flash).
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
# OpenRouter — llama-3.1-8b-instruct:free.
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

# Прокси ТОЛЬКО для Gemini (в РФ и др. регионах его API заблокирован по гео).
# Напр. http://127.0.0.1:10809 или socks5://127.0.0.1:10808. Пусто = напрямую.
GEMINI_PROXY = os.getenv("GEMINI_PROXY")

# Vision — распознавание текста со скриншотов (Groq, аналог Whisper для голоса).
VISION_MODEL = os.getenv("VISION_MODEL", "qwen/qwen3.6-27b")

REBUILD_THRESHOLD = int(os.getenv("REBUILD_THRESHOLD", "50"))
SAMPLE_SIZE = int(os.getenv("SAMPLE_SIZE", "150"))

# Стили ответа для фичи «Ответить по скриншоту».
# [0] — подпись на кнопке, [1] — инструкция стиля для промпта генерации.
REPLY_STYLES = {
    "flirt":     ("💘 Флирт",     "с лёгким флиртом, игриво, с намёком на интерес, не пошло"),
    "humor":     ("😄 Юмор",      "с юмором и самоиронией, живо"),
    "tender":    ("🥰 Нежно",     "нежно, мягко, тепло и с заботой"),
    "confident": ("😎 Уверенно",  "прямо, без воды, с характером"),
    "friendly":  ("🤝 Дружески",  "по-свойски, как с другом, непринуждённо"),
    "formal":    ("💼 Формально", "вежливо, чётко, по делу"),
}
