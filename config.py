import os

from dotenv import load_dotenv

load_dotenv()

APP_NAME = "CueMe"

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN не найден. Скопируй .env.example в .env и впиши токен от BotFather."
    )

# Groq — основной провайдер. LLM_API_KEY поддерживается как алиас.
GROQ_API_KEY = os.getenv("GROQ_API_KEY") or os.getenv("LLM_API_KEY")
# Gemini — fallback 1 (gemini-2.5-flash).
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
# OpenRouter — fallback 2 (llama-3.1-8b-instruct:free).
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

REBUILD_THRESHOLD = int(os.getenv("REBUILD_THRESHOLD", "50"))
SAMPLE_SIZE = int(os.getenv("SAMPLE_SIZE", "150"))
