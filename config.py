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

# telegram_id разработчика — единственный, кому доступна /provider (меняет
# LLM-каскад глобально для всего бота, поэтому не должна быть открыта всем).
ADMIN_TELEGRAM_ID = os.getenv("ADMIN_TELEGRAM_ID", "")

# Порядок каскада LLM (через запятую). Дефолт — Gemini основной. На сервере без
# GEMINI_PROXY имеет смысл поставить groq первым: "groq,gemini,openrouter".
LLM_PROVIDER_ORDER = os.getenv("LLM_PROVIDER_ORDER", "gemini,groq,openrouter")

# TTL кэша LLM-ответов (сек). Ключ контент-адресный (включает карточки стиля),
# поэтому смена карточек инвалидирует запись сама; TTL — страховка от разрастания.
LLM_CACHE_TTL_SEC = int(os.getenv("LLM_CACHE_TTL_SEC", str(7 * 24 * 3600)))  # 7 дней

# Прокси ТОЛЬКО для Gemini (в РФ и др. регионах его API заблокирован по гео).
# Напр. http://127.0.0.1:10809 или socks5://127.0.0.1:10808. Пусто = напрямую.
GEMINI_PROXY = os.getenv("GEMINI_PROXY")

# Vision — распознавание текста со скриншотов (Groq, аналог Whisper для голоса).
VISION_MODEL = os.getenv("VISION_MODEL", "qwen/qwen3.6-27b")

REBUILD_THRESHOLD = int(os.getenv("REBUILD_THRESHOLD", "50"))
SAMPLE_SIZE = int(os.getenv("SAMPLE_SIZE", "150"))
# Троттлинг обновления message_samples: не чаще, чем раз в N business-сообщений на контакт
# (перед реальной пересборкой карточек делается принудительный refresh).
REFRESH_SAMPLES_EVERY_N = int(os.getenv("REFRESH_SAMPLES_EVERY_N", "5"))
# Порог ПЕРВОЙ сборки карточек контакта (когда их ещё нет вообще) — ниже
# обычного REBUILD_THRESHOLD, чтобы новый юзер быстрее увидел первый результат.
FIRST_BUILD_THRESHOLD = int(os.getenv("FIRST_BUILD_THRESHOLD", "15"))

# ── Подписка (Tribute) ──────────────────────────────────────────────────────
# Приватный канал-пропуск: Tribute сам добавляет/убирает участников по оплате,
# бот только проверяет членство через get_chat_member. ID вида "-100...",
# известен после создания канала и добавления бота туда админом.
PREMIUM_CHANNEL_ID = os.getenv("PREMIUM_CHANNEL_ID")
# Ссылка на оформление подписки (со страницы канала в Tribute) — показываем в пейволле.
PREMIUM_SUBSCRIBE_URL = os.getenv("PREMIUM_SUBSCRIBE_URL", "")
# Сколько бесплатных генераций (Переписать/Ответить за меня/По скриншоту) даём
# до пейволла. Остальные функции (глубокий анализ и т.п.) только по подписке.
FREE_TRIAL_REQUESTS = int(os.getenv("FREE_TRIAL_REQUESTS", "5"))
# Кэш проверки членства в канале (сек) — не дёргать Telegram API на каждое сообщение.
PREMIUM_CACHE_TTL = int(os.getenv("PREMIUM_CACHE_TTL", "300"))

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
