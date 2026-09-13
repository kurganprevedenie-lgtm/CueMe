"""CueMe — точка входа бота: Bot/Dispatcher, middleware, подключение роутеров
и запуск polling.

Вся функциональность разложена по модулям:
  handlers/  — хендлеры по функциональным областям (каждый со своим Router)
  services/  — логика без хендлеров (карточки стиля), плюс storage/llm/features/
               compatibility_metrics/tg_parser/tools.export в корне проекта
Карта модулей и что где искать — в CLAUDE.md.
"""
import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, ErrorEvent

from config import BOT_TOKEN, GEMINI_API_KEY, GROQ_API_KEY, OPENROUTER_API_KEY
from handlers import account, admin, analysis, business, date_ideas, main_menu
from handlers import onboarding, referral, reply_flow, subscription, support
from handlers.common import GenderGateMiddleware
from llm import RateLimitError
from storage import init_db

logging.basicConfig(level=logging.INFO)

dp = Dispatcher(storage=MemoryStorage())


@dp.errors()
async def on_unhandled_error(event: ErrorEvent) -> bool:
    """Глобальная сетка на необработанные исключения в хендлерах. Без неё сбой
    (например, недоступный LLM при генерации карточек) тихо убивал кнопку:
    спиннер гас, а пользователь не понимал, что произошло. Теперь — понятное
    сообщение вместо молчания."""
    logging.exception("unhandled update error: %s", event.exception)
    text = ("Лимит запросов исчерпан — попробуй через пару минут."
            if isinstance(event.exception, RateLimitError)
            else "Что-то пошло не так — попробуй ещё раз.")
    upd = event.update
    try:
        cq = getattr(upd, "callback_query", None)
        if cq is not None:
            try:
                await cq.answer(text, show_alert=True)
            except Exception:
                if cq.message is not None:
                    await cq.message.answer(text)
        elif getattr(upd, "message", None) is not None:
            await upd.message.answer(text)
    except Exception:
        logging.exception("error handler: не удалось уведомить пользователя")
    return True

dp.message.outer_middleware(GenderGateMiddleware())
dp.callback_query.outer_middleware(GenderGateMiddleware())


# ── Подключение роутеров ──────────────────────────────────────────────────────
# ПОРЯДОК ЗДЕСЬ — ЧАСТЬ ПОВЕДЕНИЯ, не стиль. aiogram резолвит апдейт по порядку
# регистрации: первый подошедший хендлер забирает апдейт, остальные не видят.
# Значимые пересечения, ради которых выбран именно такой порядок:
#
# 1. payments_router — первым: successful_payment/pre_checkout не должны
#    перехватываться состояниями ввода имени/кода (это реальные деньги).
# 2. referral — до остальных: состояние ввода реферального кода
#    (ReferralRedeem.waiting_for_code) ловит ЛЮБОЕ сообщение, включая команды
#    и нажатия кнопок меню, — как и было в исходном main.py.
# 3. main_menu / admin / onboarding — до reply_flow: нажатие кнопки меню
#    выходит из режима «Ответ с CueMe», фото перехватывает handle_photo, а
#    документ — handle_document (JSON-импорт), даже если юзер в состоянии
#    ожидания сообщения. Это исходное поведение.
# 4. reply_flow — после них, но до account/support/subscription: команды
#    /rebuild, /progress, /help, /premium, /delete в исходном коде шли ПОСЛЕ
#    состояний ввода имени и потому ими перехватывались.
#
# Расхождения с исходным порядком (осознанные, см. отчёт по рефакторингу):
# /contacts, /inspect, /wipe, /broadcast_invite поменяли сторону относительно
# catch-all состояний ввода имени/кода — то есть отличаются ТОЛЬКО если юзер
# наберёт одну из этих команд, пока бот ждёт имя диалога или реферальный код.
dp.include_router(subscription.payments_router)
dp.include_router(referral.router)
dp.include_router(main_menu.router)
dp.include_router(analysis.router)
dp.include_router(date_ideas.router)
dp.include_router(admin.router)
dp.include_router(onboarding.router)
dp.include_router(business.router)
dp.include_router(reply_flow.router)
dp.include_router(account.router)
dp.include_router(support.router)
dp.include_router(subscription.router)


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


async def main() -> None:
    _validate_startup_config()
    init_db()
    bot = Bot(token=BOT_TOKEN)
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
    asyncio.create_task(subscription._reconcile_promo_channel_premium(bot))
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
