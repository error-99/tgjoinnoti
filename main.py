
import asyncio
import logging
import os
import random
import time

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

# Configuration
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = int(os.getenv("CHAT_ID", "-1004384102181"))
INTERVAL_SECONDS = 60

NAMES = [
    "Rahim",
    "Karim",
    "Shabbir",
    "Sakib",
    "Nusrat",
    "Mim",
    "Ayesha",
    "Sumaiya",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

# In-memory state; resets whenever the app restarts.
previous_message_id = None


async def rotate_message(bot: Bot):
    global previous_message_id

    # Delete the previous message sent by this bot.
    if previous_message_id is not None:
        try:
            await bot.delete_message(
                chat_id=CHAT_ID,
                message_id=previous_message_id,
            )
            logging.info("Deleted previous message %s", previous_message_id)

        except TelegramAPIError as error:
            logging.warning("Could not delete previous message: %s", error)

    # Choose a random name and send a new message.
    name = random.choice(NAMES)

    message = await bot.send_message(
        chat_id=CHAT_ID,
        text=f"🔔 Random name: {name}",
    )

    previous_message_id = message.message_id

    logging.info(
        "Sent random name '%s' (message ID %s)",
        name,
        previous_message_id,
    )


async def main():
    bot = Bot(token=BOT_TOKEN)

    try:
        while True:
            started = time.monotonic()

            try:
                await rotate_message(bot)
            except TelegramAPIError:
                logging.exception("Telegram API request failed")
            except Exception:
                logging.exception("Unexpected error")

            # Wait 60 seconds between the start of each cycle.
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(0, INTERVAL_SECONDS - elapsed))

    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
