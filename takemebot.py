import asyncio
import logging

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message

from config import TELEGRAM_BOT_TOKEN

dp = Dispatcher()


@dp.message(F.text)
async def on_text(message: Message) -> None:
    await message.answer("бот работает")


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    bot = Bot(TELEGRAM_BOT_TOKEN)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
