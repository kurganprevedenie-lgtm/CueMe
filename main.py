"""Точка входа бота CueMe. Сам код разнесён по модулям в порядке исходного
main.py (app_state, handlers/*): порядок импорта ниже = порядок регистрации
хендлеров, менять его нельзя (см. tests/test_handler_registry.py)."""
import app_state  # noqa: E402
import handlers.subscription  # noqa: E402
import handlers.referrals_gate  # noqa: E402
import handlers.stars_tribute  # noqa: E402
import handlers.admin_broadcast  # noqa: E402
import handlers.redeem_menu  # noqa: E402
import handlers.profile  # noqa: E402
import handlers.cards  # noqa: E402
import handlers.analysis  # noqa: E402
import handlers.business  # noqa: E402
import handlers.start_menu  # noqa: E402
import handlers.admin_tools  # noqa: E402
import handlers.reply  # noqa: E402
import handlers.service_cmds  # noqa: E402
import handlers.premium_screen  # noqa: E402
import handlers.watch_delete  # noqa: E402
import handlers.admin_users  # noqa: E402

# Имена, которые нужны самому коду запуска ниже, — явно (проверяемо pyflakes,
# а не только через реэкспорт).
import asyncio  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import time  # noqa: E402

from aiogram import Bot  # noqa: E402
from aiogram.types import BotCommand  # noqa: E402

from config import (  # noqa: E402
    ADMIN_GROUP_CHAT_ID, BOT_TOKEN, GEMINI_API_KEY, GROQ_API_KEY, OPENROUTER_API_KEY,
)
from llm import get_invalid_output_counts, set_invalid_output_hook  # noqa: E402
from storage import init_db, record_event  # noqa: E402
from app_state import _watch_event_loop_lag, dp  # noqa: E402
from handlers.referrals_gate import _reconcile_promo_channel_premium  # noqa: E402
from handlers.stars_tribute import _start_tribute_webhook_server  # noqa: E402

# Обратная совместимость: main.<имя> для тестов и ops-скриптов (import main).
# Подменять в тестах надо там, где имя вызывается, — в его модуле.
for _m in (app_state, handlers.subscription, handlers.referrals_gate, handlers.stars_tribute, handlers.admin_broadcast, handlers.redeem_menu, handlers.profile, handlers.cards, handlers.analysis, handlers.business, handlers.start_menu, handlers.admin_tools, handlers.reply, handlers.service_cmds, handlers.premium_screen, handlers.watch_delete, handlers.admin_users,):
    globals().update({_k: _v for _k, _v in vars(_m).items() if not _k.startswith("__")})
del _m

# ── запуск ────────────────────────────────────────────────────────────────────

def _validate_startup_config() -> None:
    """Fail-fast проверка до запуска polling. Хотя бы один LLM-ключ обязателен —
    иначе бот не сможет генерировать ответы. Отсутствие отдельных ключей — warning
    (каскад их просто пропустит)."""
    keys = {
        "GEMINI_API_KEY":     GEMINI_API_KEY,
        "GROQ_API_KEY":       GROQ_API_KEY,
        "OPENROUTER_API_KEY": OPENROUTER_API_KEY,
    }
    present = [name for name, val in keys.items() if val]
    for name, val in keys.items():
        if not val:
            logging.warning("%s не задан — провайдер будет пропускаться в каскаде.", name)
    if not present:
        raise RuntimeError(
            "Не задан ни один LLM-ключ (GEMINI_API_KEY / GROQ_API_KEY / OPENROUTER_API_KEY). "
            "Бот не сможет генерировать ответы — заполни .env."
        )
    if not GROQ_API_KEY:
        logging.warning(
            "GROQ_API_KEY не задан — распознавание голоса пойдёт только "
            "через Gemini-fallback."
        )
    logging.info("Конфиг проверен. Доступные LLM-ключи: %s", ", ".join(present))


_INVALID_ALERT_THRESHOLD = 5  # невалидных за час от одного провайдера → алерт в группу
_invalid_alerted_at: dict[str, float] = {}


def _make_invalid_output_hook(bot: Bot):
    """Хук для llm.set_invalid_output_hook: событие gen_invalid_output и, если
    у провайдера за час больше _INVALID_ALERT_THRESHOLD невалидных, — одно
    сообщение в ADMIN_GROUP_CHAT_ID (не чаще раза в час на провайдера)."""
    def hook(provider: str, tag: str, reason: str) -> None:
        try:
            record_event(None, "gen_invalid_output", json.dumps(
                {"provider": provider, "tag": tag, "reason": reason}, ensure_ascii=False))
        except Exception:
            logging.exception("gen_invalid_output: не удалось записать событие")
        base = provider.split(" ", 1)[0]
        hour, _ = get_invalid_output_counts().get(base, (0, 0))
        now = time.time()
        if ADMIN_GROUP_CHAT_ID and hour > _INVALID_ALERT_THRESHOLD and now - _invalid_alerted_at.get(base, 0) > 3600:
            _invalid_alerted_at[base] = now
            asyncio.get_running_loop().create_task(bot.send_message(
                int(ADMIN_GROUP_CHAT_ID),
                f"⚠️ {base}: {hour} невалидных ответов за час (последний — {tag}: {reason}). "
                "Бот их отбраковывает и идёт к следующему провайдеру, но стоит посмотреть.",
            ))
    return hook


async def main() -> None:
    _validate_startup_config()
    init_db()
    bot = Bot(token=BOT_TOKEN)
    set_invalid_output_hook(_make_invalid_output_hook(bot))
    await bot.set_my_commands([
        BotCommand(command="start",       description="Начало работы"),
        BotCommand(command="menu",        description="Главное меню"),
        BotCommand(command="gender",      description="Сменить пол"),
        BotCommand(command="help",        description="Список команд"),
        BotCommand(command="connect",     description="Подключить Автоматизацию чатов"),
        BotCommand(command="me",          description="Мой стиль общения"),
        # BotCommand("screenshot", ...) убрана вместе с функцией «скриншот
        # переписки → ответ» — команда /screenshot закомментирована в коде.
        BotCommand(command="reply",       description="Помочь ответить собеседнику"),
        BotCommand(command="contacts",    description="Загруженные чаты"),
        BotCommand(command="progress",    description="Прогресс накопления по контактам"),
        BotCommand(command="deep_analysis", description="Анализ собеседника"),
        BotCommand(command="premium",     description="Статус подписки"),
        BotCommand(command="delete",      description="Удалить свои данные"),
        BotCommand(command="rebuild",     description="Пересобрать все карточки"),
    ])
    asyncio.create_task(_reconcile_promo_channel_premium(bot))
    asyncio.create_task(_watch_event_loop_lag())
    await _start_tribute_webhook_server(bot)  # no-op, если TRIBUTE_API_KEY не задан
    await dp.start_polling(
        bot,
        allowed_updates=[
            "message",
            "callback_query",
            "business_connection",
            "business_message",
            "edited_business_message",
            "deleted_business_messages",
            "pre_checkout_query",
            "chat_member",
        ],
    )


if __name__ == "__main__":
    asyncio.run(main())
