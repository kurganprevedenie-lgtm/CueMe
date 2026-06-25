import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import Message

from config import APP_NAME, BOT_TOKEN

logging.basicConfig(level=logging.INFO)

dp = Dispatcher()


@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        f"Привет! Это {APP_NAME} — бот, который разбирается, "
        f"как ты общаешься, и помогает писать так, чтобы тебя поняли правильно.\n\n"
        f"Пока я только учусь. Скоро смогу разобрать твой стиль переписки."
    )


async def main() -> None:
    bot = Bot(token=BOT_TOKEN)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
